"""
SDK Engine — Agent dispatch via claude-code-sdk (replaces CLI subprocess spawning).

Uses the claude-code-sdk Python API to run agents with:
- Native streaming via async iterators
- Stdio MCP servers for structured tools (context + self-service)
- Per-project MCP server configs from DB
- Report pickup from MCP submit_report tool (JSON file)
- Session resume via SDK
- Proper cost/token tracking from ResultMessage
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any  # kept for type annotations

from claude_code_sdk import (
    query,
    ClaudeCodeOptions,
    AssistantMessage,
    UserMessage,
    SystemMessage,
    ResultMessage,
)
from claude_code_sdk.types import TextBlock, ToolUseBlock, ToolResultBlock, ThinkingBlock
from claude_code_sdk._errors import MessageParseError

# Monkey-patch the SDK's parse_message to skip rate_limit_event instead of throwing.
# The SDK raises MessageParseError on unknown types, which kills the subprocess
# transport (the error propagates through the generator's try/finally → query.close()).
import claude_code_sdk._internal.message_parser as _mp
import claude_code_sdk._internal.client as _cl
_original_parse = _mp.parse_message

_SKIP_EVENT_TYPES = {"rate_limit_event", "tool_use_summary"}

def _patched_parse(data):
    if isinstance(data, dict) and data.get("type") in _SKIP_EVENT_TYPES:
        return None  # Signal to skip
    try:
        return _original_parse(data)
    except MessageParseError:
        # Skip any other unknown event types gracefully
        return None

_mp.parse_message = _patched_parse
# Also patch in the client module where it's imported directly
_cl.parse_message = _patched_parse

import db
from prompt_template import build_prompt
from worktree import create_worktree, cleanup_worktree
from models import TOOL_PRESETS, DispatchOptions

import httpx
import random

log = logging.getLogger("devfleet.sdk_engine")


def _stderr_log_path(session_id: str) -> str:
    """Per-session file the spawned Claude CLI writes its stderr/debug log into.

    The SDK swallows the CLI's stderr by default and surfaces a useless
    "Check stderr output for details" placeholder when the CLI exits non-zero.
    Tee'ing it to a file gives us real diagnostic info on failure.
    """
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(backend_dir, "..", "data", "logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"session-{session_id}.stderr.log")


def _read_stderr_tail(session_id: str, max_chars: int = 4000) -> str:
    """Read the last `max_chars` from the per-session stderr log, if any."""
    path = _stderr_log_path(session_id)
    if not os.path.exists(path):
        return ""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_chars:
                f.seek(size - max_chars)
                # Drop the partial first line for readability
                _ = f.readline()
            return f.read().decode("utf-8", errors="replace").strip()
    except Exception as e:
        log.warning("Failed to read stderr tail for %s: %s", session_id, e)
        return ""


# ── Retry configuration ─────────────────────────────────────────
DEFAULT_MAX_RETRIES = int(os.environ.get("DEVFLEET_MAX_RETRIES", "3"))
DEFAULT_RETRY_INITIAL_DELAY = float(os.environ.get("DEVFLEET_RETRY_INITIAL_DELAY", "2.0"))
DEFAULT_RETRY_MAX_DELAY = float(os.environ.get("DEVFLEET_RETRY_MAX_DELAY", "60.0"))

# Error patterns that indicate transient failures (should retry)
_TRANSIENT_PATTERNS = [
    "500", "502", "503", "504", "429",  # HTTP status codes
    "internal server error", "service unavailable", "bad gateway",
    "gateway timeout", "rate limit", "overloaded",
    "timeout", "timed out", "connection reset", "connection refused",
    "broken pipe", "eof", "ssl", "network",
]


def _is_transient_error(exc: Exception, stderr: str = "", exit_code: int | None = None) -> bool:
    """Classify an exception as transient (retry) or permanent (fail).

    Looks at both the SDK exception text and (when provided) the captured CLI
    stderr. A generic `exit code 1` with the SDK placeholder text and no real
    stderr is treated as transient — empirically these are MCP/API startup
    blips, not genuine permanent failures.
    """
    err = str(exc).lower()
    if any(p in err for p in _TRANSIENT_PATTERNS):
        return True
    stderr_low = (stderr or "").lower()
    if stderr_low and any(p in stderr_low for p in _TRANSIENT_PATTERNS):
        return True
    if exit_code == 1 and (
        "check stderr output for details" in err
        or not stderr.strip()
    ):
        return True
    return False


def _classify_error(exc_or_msg, stderr: str = "", exit_code: int | None = None) -> str:
    """Map an error string to a stable taxonomy clients can branch on.

    Now also inspects the real CLI stderr (captured per-session) and the
    CLI exit code. The SDK historically reports the placeholder "Check stderr
    output for details" with exit code 1; on its own that string carries no
    signal, but the real stderr usually reveals whether the cause was transient
    (rate-limit, MCP-init, network) or a genuine permanent error.
    """
    msg = str(exc_or_msg).lower()
    stderr_low = (stderr or "").lower()
    if any(p in msg for p in ("rate limit", "rate_limit", "429", "overloaded", "quota")):
        return "rate_limit_exhausted"
    if stderr_low and any(p in stderr_low for p in ("rate limit", "rate_limit", "429", "overloaded", "quota")):
        return "rate_limit_exhausted"
    if any(p in msg for p in _TRANSIENT_PATTERNS):
        return "transient_exhausted"
    if stderr_low and any(p in stderr_low for p in _TRANSIENT_PATTERNS):
        return "transient_exhausted"

    # Generic "exit code 1" with the SDK placeholder stderr and no real captured
    # stderr → almost certainly an early-startup failure (MCP server init, env,
    # transient API). Mark retryable so the dispatcher escalates next time
    # instead of permanently burying the mission.
    if exit_code == 1 and (
        "check stderr output for details" in msg
        or not stderr.strip()
    ):
        return "unknown_early_exit"

    return "permanent_error"


def _validate_completion(
    total_tokens: int,
    total_cost: float,
    output_chunks: list,
    report_data: dict | None,
    worktree_had_commits: bool,
) -> tuple[bool, str]:
    """Decide whether a session that exited the SDK loop without raising
    actually did real work. Returns (ok, reason). ok=True → mark completed.
    """
    if total_tokens == 0 and total_cost == 0.0:
        return False, (
            "Agent ran but produced 0 tokens and $0 cost — likely rate-limited "
            "and gave up silently. Re-dispatch when API quota resets."
        )
    if not output_chunks and report_data is None and not worktree_had_commits:
        return False, (
            "Agent finished without text output, without calling submit_report, "
            "and without any worktree commits — no evidence of real work."
        )
    if total_cost < 0.05 and not worktree_had_commits and report_data is None:
        return False, (
            f"Agent finished too cheaply (${total_cost:.4f}) with no commits and "
            f"no submit_report — looks like a stub completion."
        )
    return True, ""


async def _worktree_had_commits(project_path: str, session_id: str) -> bool:
    """Check whether the worktree branch has commits ahead of master."""
    short_id = session_id[:8]
    branch_name = f"devfleet/{short_id}"
    proc = await asyncio.create_subprocess_exec(
        "git", "log", f"HEAD..{branch_name}", "--oneline",
        cwd=project_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return bool(stdout.strip())


async def _master_status_lines(project_path: str) -> set[str]:
    """Snapshot of `git status --porcelain` lines on master. Used to detect
    cwd-escape: agent writes that landed in master's working tree instead of
    the isolated worktree (a real bug observed with some models/configs).
    Lines look like '?? agents/foo.py' or ' M agents/bar.py'.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "status", "--porcelain",
        cwd=project_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return set(line for line in stdout.decode().splitlines() if line.strip())


def _extract_path_from_status_line(line: str) -> str:
    """`?? agents/foo.py` → `agents/foo.py`. ` M file -> renamed` → `renamed`."""
    if not line or len(line) < 4:
        return ""
    rest = line[3:]
    # rename lines look like ' R old -> new' — take the new path
    if " -> " in rest:
        rest = rest.split(" -> ", 1)[1]
    # quoted paths (when name has spaces / unicode) — strip quotes
    if rest.startswith('"') and rest.endswith('"'):
        rest = rest[1:-1]
    return rest


async def _recover_cwd_escape(
    project_path: str,
    session_id: str,
    escaped_status_lines: set[str],
) -> tuple[bool, str, list[str]]:
    """Agent wrote files into master's working tree instead of the worktree.
    Stage+commit those files directly on master so the work isn't lost.
    Returns (ok, message, file_paths_committed).
    """
    short_id = session_id[:8]
    files = []
    for line in escaped_status_lines:
        p = _extract_path_from_status_line(line)
        if p and not p.startswith(".devfleet-worktrees/"):
            files.append(p)
    if not files:
        return True, "", []

    # Stage just those paths (don't `git add -A` — keep blast radius tight)
    add_proc = await asyncio.create_subprocess_exec(
        "git", "add", "--", *files,
        cwd=project_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, add_err = await add_proc.communicate()
    if add_proc.returncode != 0:
        return False, f"git add failed: {add_err.decode()[:200]}", files

    msg = (
        f"DevFleet: cwd-escape recovery for session {short_id}\n\n"
        f"Agent wrote {len(files)} file(s) into master's working tree instead of "
        f"the isolated worktree (.devfleet-worktrees/session-{short_id}). "
        f"Auto-staging+committing here so the work isn't lost on next worktree creation."
    )
    commit_proc = await asyncio.create_subprocess_exec(
        "git", "commit", "-m", msg,
        cwd=project_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, commit_err = await commit_proc.communicate()
    if commit_proc.returncode != 0:
        return False, f"git commit failed: {commit_err.decode()[:200]}", files

    log.warning(
        "Session %s cwd-escape: recovered %d file(s) on master via direct commit",
        session_id, len(files),
    )
    return True, f"Recovered {len(files)} file(s) on master", files


def _retry_delay(attempt: int, initial: float = 2.0, maximum: float = 60.0) -> float:
    """Exponential backoff with jitter. attempt is 0-indexed."""
    delay = min(initial * (2 ** attempt), maximum)
    jitter = random.uniform(0, delay * 0.25)  # 25% jitter
    return delay + jitter


async def _fire_callback(mission: dict, status: str, payload: dict | None = None):
    """POST to mission's callback_url if set. Fire-and-forget, never raises."""
    url = (mission.get("callback_url") or "").strip()
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={
                "mission_id": mission["id"],
                "mission_title": mission.get("title", ""),
                "project_id": mission.get("project_id", ""),
                "status": status,
                "payload": payload or {},
            })
        log.info("Callback fired: %s → %s", status, url)
    except Exception as e:
        log.warning("Callback failed for mission %s: %s", mission.get("id"), e)

# ── In-memory state (same pattern as old dispatcher) ──
running_tasks: dict[str, asyncio.Task] = {}
_subscribers: dict[str, list[asyncio.Queue]] = {}
_event_buffers: dict[str, list[dict]] = {}
# Sessions being taken over — worktree is preserved on cancel
_takeover_sessions: set[str] = {}


# ── MCP Server Integration (Phase 2) ──
# Agents get DevFleet tools via stdio MCP servers (mcp_context.py, mcp_devfleet.py).
# The submit_report tool writes a JSON file that we pick up after agent completion.


async def _load_project_mcp_configs(project_id: str) -> dict:
    """Load per-project MCP server configs from the mcp_configs DB table."""
    if not project_id:
        return {}
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT server_name, server_type, config_json FROM mcp_configs WHERE project_id=? AND enabled=1",
            (project_id,),
        )
        configs = {}
        for row in rows:
            r = dict(row)
            try:
                config = json.loads(r["config_json"])
                config["type"] = r["server_type"]
                configs[r["server_name"]] = config
            except (json.JSONDecodeError, TypeError):
                log.warning("Invalid MCP config for server %s", r["server_name"])
        return configs
    finally:
        await conn.close()


def _read_report_file(session_id: str) -> dict | None:
    """Read report JSON written by the stdio MCP submit_report tool."""
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    report_dir = os.path.join(backend_dir, "..", "data", "reports")
    report_path = os.path.join(report_dir, f"{session_id}.json")
    try:
        if os.path.exists(report_path):
            with open(report_path, "r") as f:
                data = json.load(f)
            # Clean up the file after reading
            os.remove(report_path)
            log.info("Picked up MCP report file for session %s", session_id)
            return data
    except Exception as e:
        log.warning("Failed to read report file for session %s: %s", session_id, e)
    return None


def _build_sdk_options(
    mission: dict,
    opts: DispatchOptions | None,
    work_dir: str,
    session_id: str = "",
    resume_session_id: str | None = None,
    extra_mcp_servers: dict | None = None,
    stderr_file=None,
) -> ClaudeCodeOptions:
    """Build ClaudeCodeOptions from mission config + dispatch overrides."""

    # Model selection: override > mission > default
    model = "claude-opus-4-6"
    if opts and opts.model:
        model = opts.model
    elif mission.get("model"):
        model = mission["model"]

    # Allowed tools: override > preset > mission config > full
    allowed_tools = []
    if opts and opts.allowed_tools:
        allowed_tools = opts.allowed_tools
    elif opts and opts.tool_preset and opts.tool_preset in TOOL_PRESETS:
        allowed_tools = TOOL_PRESETS[opts.tool_preset]
    elif mission.get("allowed_tools"):
        raw = mission["allowed_tools"]
        if raw in TOOL_PRESETS:
            allowed_tools = TOOL_PRESETS[raw]
        else:
            try:
                allowed_tools = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
    elif mission.get("mission_type") and mission["mission_type"] in TOOL_PRESETS:
        allowed_tools = TOOL_PRESETS[mission["mission_type"]]

    # Allow DevFleet MCP tools
    allowed_tools.extend([
        "mcp__devfleet-context__get_mission_context",
        "mcp__devfleet-context__get_project_context",
        "mcp__devfleet-context__get_session_history",
        "mcp__devfleet-context__get_team_context",
        "mcp__devfleet-context__read_past_reports",
        "mcp__devfleet-tools__submit_report",
        "mcp__devfleet-tools__create_sub_mission",
        "mcp__devfleet-tools__request_review",
        "mcp__devfleet-tools__get_sub_mission_status",
        "mcp__devfleet-tools__list_project_missions",
    ])

    # System prompt
    append_prompt = opts.append_system_prompt if opts and opts.append_system_prompt else None

    # Max turns
    max_turns = None
    if opts and opts.max_turns:
        max_turns = opts.max_turns
    elif mission.get("max_turns"):
        max_turns = mission["max_turns"]

    # MCP servers — auto-attach DevFleet context + tools as stdio servers
    python_path = sys.executable
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    mcp_env = {
        "DEVFLEET_API_URL": os.environ.get("DEVFLEET_API_URL", "http://localhost:18801"),
        "DEVFLEET_MISSION_ID": mission.get("id", ""),
        "DEVFLEET_PROJECT_ID": mission.get("project_id", ""),
        "DEVFLEET_SESSION_ID": session_id,
        "DEVFLEET_REPORT_DIR": os.path.join(backend_dir, "..", "data", "reports"),
    }

    mcp_servers = {
        "devfleet-context": {
            "type": "stdio",
            "command": python_path,
            "args": [os.path.join(backend_dir, "mcp_context.py")],
            "env": mcp_env,
        },
        "devfleet-tools": {
            "type": "stdio",
            "command": python_path,
            "args": [os.path.join(backend_dir, "mcp_devfleet.py")],
            "env": mcp_env,
        },
    }

    # Context Mode — attach context-mode MCP server for context savings + session continuity
    # See: https://github.com/mksglu/context-mode
    use_context_mode = opts and opts.context_mode
    if use_context_mode:
        context_mode_cmd = os.environ.get("DEVFLEET_CONTEXT_MODE_CMD", "context-mode")
        mcp_servers["context-mode"] = {
            "type": "stdio",
            "command": context_mode_cmd,
            "args": [],
            "env": {"PROJECT_DIR": work_dir},
        }
        # Allow context-mode tools
        allowed_tools.extend([
            "mcp__context-mode__ctx_execute",
            "mcp__context-mode__ctx_batch_execute",
            "mcp__context-mode__ctx_execute_file",
            "mcp__context-mode__ctx_index",
            "mcp__context-mode__ctx_search",
            "mcp__context-mode__ctx_fetch_and_index",
        ])
        log.info("Context Mode enabled for session %s", session_id)

    if extra_mcp_servers:
        mcp_servers.update(extra_mcp_servers)

    kwargs = dict(
        model=model,
        max_turns=max_turns,
        allowed_tools=allowed_tools,
        append_system_prompt=append_prompt,
        permission_mode="bypassPermissions",
        cwd=work_dir,
        resume=resume_session_id,
        include_partial_messages=False,
    )
    if mcp_servers:
        kwargs["mcp_servers"] = mcp_servers
    if stderr_file is not None:
        # Capture the spawned CLI's stderr so failures expose real diagnostics
        # instead of the SDK's "Check stderr output for details" placeholder.
        kwargs["debug_stderr"] = stderr_file
        kwargs["extra_args"] = {"debug-to-stderr": None}
    return ClaudeCodeOptions(**kwargs)


# ── Broadcasting (SSE to frontend) ──

def _broadcast(session_id: str, event: dict):
    """Push event to all subscribers and buffer for late joiners."""
    if session_id in _event_buffers:
        _event_buffers[session_id].append(event)
    for queue in _subscribers.get(session_id, []):
        queue.put_nowait(event)


def _broadcast_content_block(session_id: str, block):
    """Broadcast a content block from an AssistantMessage."""
    if isinstance(block, TextBlock):
        _broadcast(session_id, {"type": "text", "text": "\n" + block.text + "\n"})
    elif isinstance(block, ToolUseBlock):
        tool_name = block.name
        tool_input = block.input if isinstance(block.input, dict) else {}
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            _broadcast(session_id, {"type": "tool", "text": f"$ {cmd}\n"})
        elif tool_name in ("Edit", "Write"):
            fpath = tool_input.get("file_path", "")
            _broadcast(session_id, {"type": "tool", "text": f"[{tool_name}] {fpath}\n"})
        elif tool_name == "Read":
            fpath = tool_input.get("file_path", "")
            _broadcast(session_id, {"type": "tool", "text": f"[Read] {fpath}\n"})
        elif tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            _broadcast(session_id, {"type": "tool", "text": f"[{tool_name}] {pattern}\n"})
        else:
            _broadcast(session_id, {"type": "tool", "text": f"[{tool_name}]\n"})
    elif isinstance(block, ToolResultBlock):
        content = block.content
        if isinstance(content, str) and content:
            text = content[:1500] + "\n... (truncated)" if len(content) > 1500 else content
            _broadcast(session_id, {"type": "tool_result", "text": text + "\n"})
        elif isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item["text"])
            result_text = "\n".join(parts)
            if result_text:
                if len(result_text) > 1500:
                    result_text = result_text[:1500] + "\n... (truncated)"
                _broadcast(session_id, {"type": "tool_result", "text": result_text + "\n"})


async def subscribe_session(session_id: str):
    """Async generator that yields SSE events for a session."""
    queue = asyncio.Queue()
    _subscribers.setdefault(session_id, []).append(queue)
    try:
        # Replay buffered events for late joiners
        if session_id in _event_buffers:
            for evt in _event_buffers[session_id]:
                yield evt
        else:
            # Check DB for completed sessions
            conn = await db.get_db()
            try:
                rows = await conn.execute_fetchall(
                    "SELECT output_log, status FROM agent_sessions WHERE id=?", (session_id,)
                )
                if rows:
                    existing = dict(rows[0])
                    if existing["output_log"]:
                        yield {"type": "backfill", "text": existing["output_log"]}
                    if existing["status"] != "running":
                        yield {"type": "done", "status": existing["status"]}
                        return
            finally:
                await conn.close()

        while True:
            event = await queue.get()
            yield event
            if event.get("type") == "done":
                break
    finally:
        if session_id in _subscribers:
            _subscribers[session_id].remove(queue)
            if not _subscribers[session_id]:
                del _subscribers[session_id]


# ── Core dispatch/resume via SDK ──

async def _safe_query(prompt: str, options: ClaudeCodeOptions):
    """Wrapper around SDK query() that filters None results from patched parser."""
    async for message in query(prompt=prompt, options=options):
        if message is not None:
            yield message


async def _run_agent(
    session_id: str,
    mission: dict,
    prompt: str,
    work_dir: str,
    worktree_path: str | None,
    project_path: str,
    opts: DispatchOptions | None,
    resume_session_id: str | None = None,
    existing_output: str = "",
    existing_cost: float = 0.0,
    existing_tokens: int = 0,
):
    """Unified agent runner for both dispatch and resume."""
    output_chunks = []
    total_cost = existing_cost
    total_tokens = existing_tokens

    # Initialize event buffer
    _event_buffers[session_id] = []

    # cwd-escape detection: snapshot master's status so we can detect any agent
    # writes that bypass the worktree. Empty set if not a git project.
    pre_master_status: set[str] = set()
    if worktree_path:
        try:
            pre_master_status = await _master_status_lines(project_path)
        except Exception as e:
            log.warning("Failed pre-status snapshot for %s: %s", session_id, e)

    # Capture the CLI's stderr to a per-session file so on failure we get real
    # diagnostics instead of "Check stderr output for details".
    try:
        stderr_file = open(_stderr_log_path(session_id), "ab", buffering=0)
    except Exception as e:
        log.warning("Failed to open stderr log for %s: %s", session_id, e)
        stderr_file = None

    try:
        # Load per-project MCP configs from DB
        extra_mcp = await _load_project_mcp_configs(mission.get("project_id", ""))

        # Inject project-level system_prompt — prepend to any per-dispatch override
        project_system_prompt = ""
        proj_id = mission.get("project_id", "")
        if proj_id:
            try:
                conn = await db.get_db()
                try:
                    rows = await conn.execute_fetchall(
                        "SELECT system_prompt FROM projects WHERE id=?", (proj_id,)
                    )
                    if rows and rows[0]["system_prompt"]:
                        project_system_prompt = rows[0]["system_prompt"]
                finally:
                    await conn.close()
            except Exception:
                pass

        if project_system_prompt:
            if opts is None:
                opts = DispatchOptions()
            existing_sp = opts.append_system_prompt or ""
            opts = opts.model_copy(update={
                "append_system_prompt": (project_system_prompt + "\n\n" + existing_sp).strip()
            })

        # Build initial options (rebuilt inside retry loop if claude_session_id is captured)
        sdk_options = _build_sdk_options(
            mission, opts, work_dir,
            session_id=session_id,
            resume_session_id=resume_session_id,
            extra_mcp_servers=extra_mcp or None,
            stderr_file=stderr_file,
        )
        model_used = sdk_options.model or "claude-opus-4-6"

        log.info(
            "SDK dispatch session %s for mission '%s' in %s (model: %s, worktree: %s, resume: %s)",
            session_id, mission["title"], work_dir, model_used,
            worktree_path is not None, resume_session_id or "none",
        )

        # Broadcast config to frontend
        _broadcast(session_id, {
            "type": "config",
            "model": model_used,
            "max_turns": sdk_options.max_turns,
            "max_budget_usd": (opts and opts.max_budget_usd) or mission.get("max_budget_usd"),
            "mission_type": mission.get("mission_type", "implement"),
        })

        claude_session_id = ""
        current_resume_id = resume_session_id  # rolls forward when SystemMessage gives us one
        attempt_prompt = prompt  # swapped to "continue" prompt after first attempt that captured a session

        # ── Retry configuration ──────────────────────────────
        max_retries = (opts.max_retries if opts else DEFAULT_MAX_RETRIES) or DEFAULT_MAX_RETRIES
        retry_initial = (opts.retry_initial_delay if opts else DEFAULT_RETRY_INITIAL_DELAY) or DEFAULT_RETRY_INITIAL_DELAY
        retry_max = (opts.retry_max_delay if opts else DEFAULT_RETRY_MAX_DELAY) or DEFAULT_RETRY_MAX_DELAY
        retry_count = 0
        last_transient_error = ""

        # ── Stream messages from the SDK (with retry on transient errors) ──
        while True:
            # Rebuild options if we have a resume id from a prior attempt
            if current_resume_id and current_resume_id != resume_session_id:
                sdk_options = _build_sdk_options(
                    mission, opts, work_dir,
                    session_id=session_id,
                    resume_session_id=current_resume_id,
                    extra_mcp_servers=extra_mcp or None,
                    stderr_file=stderr_file,
                )
            try:
                async for message in _safe_query(prompt=attempt_prompt, options=sdk_options):
                    if isinstance(message, SystemMessage):
                        # Capture session ID for resume capability
                        new_id = message.data.get("session_id", "") or ""
                        if new_id:
                            if not claude_session_id:
                                claude_session_id = new_id
                            current_resume_id = new_id  # use this on any future retry
                            log.info("Session %s → Claude session ID: %s", session_id, new_id)
                            try:
                                conn = await db.get_db()
                                await conn.execute(
                                    "UPDATE agent_sessions SET claude_session_id=?, model=? WHERE id=?",
                                    (claude_session_id, model_used, session_id),
                                )
                                await conn.commit()
                                await conn.close()
                            except Exception:
                                pass

                    elif isinstance(message, AssistantMessage):
                        for block in message.content:
                            _broadcast_content_block(session_id, block)
                            if isinstance(block, TextBlock):
                                output_chunks.append(block.text)

                    elif isinstance(message, UserMessage):
                        # Tool results come back as UserMessages
                        content = message.content
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, ToolResultBlock):
                                    _broadcast_content_block(session_id, block)

                    elif isinstance(message, ResultMessage):
                        # Final result with usage/cost
                        result_text = getattr(message, "result", "") or ""
                        if result_text:
                            combined = "".join(output_chunks)
                            if result_text not in combined:
                                output_chunks.append(result_text)
                                _broadcast(session_id, {"type": "text", "text": "\n" + result_text})

                        # Extract cost and usage — ResultMessage has direct attrs
                        usage = message.usage  # dict or None
                        cost = message.total_cost_usd  # float or None
                        input_t = 0
                        output_t = 0
                        if usage and isinstance(usage, dict):
                            input_t = usage.get("input_tokens", 0) or 0
                            output_t = usage.get("output_tokens", 0) or 0
                            total_tokens = existing_tokens + input_t + output_t
                        if cost:
                            total_cost = existing_cost + cost
                        if cost or usage:
                            _broadcast(session_id, {
                                "type": "usage",
                                "usage": {"input_tokens": input_t, "output_tokens": output_t},
                                "cost": total_cost,
                            })

                break  # Success — exit retry loop

            except asyncio.CancelledError:
                raise  # Don't retry cancellations

            except Exception as query_exc:
                live_stderr = _read_stderr_tail(session_id)
                exit_code = getattr(query_exc, "exit_code", None)
                if _is_transient_error(query_exc, stderr=live_stderr, exit_code=exit_code) and retry_count < max_retries:
                    retry_count += 1
                    delay = _retry_delay(retry_count - 1, retry_initial, retry_max)
                    last_transient_error = str(query_exc)

                    # If the SDK gave us a Claude session id during this attempt,
                    # switch to "continue" prompting so the agent doesn't restart from scratch.
                    if current_resume_id:
                        attempt_prompt = (
                            "Continue from where you left off — finish the remaining "
                            "work and call submit_report when done."
                        )

                    log.warning(
                        "Session %s: transient error (attempt %d/%d, resume=%s), retrying in %.1fs: %s",
                        session_id, retry_count, max_retries,
                        current_resume_id or "none", delay, query_exc,
                    )
                    _broadcast(session_id, {
                        "type": "retry",
                        "attempt": retry_count,
                        "max_retries": max_retries,
                        "delay": round(delay, 1),
                        "error": str(query_exc)[:200],
                        "resume": bool(current_resume_id),
                    })

                    # Update session with retry state
                    try:
                        conn = await db.get_db()
                        await conn.execute(
                            "UPDATE agent_sessions SET retry_count=?, last_error=?, error_type='transient' WHERE id=?",
                            (retry_count, str(query_exc)[:500], session_id),
                        )
                        await conn.commit()
                        await conn.close()
                    except Exception:
                        pass

                    await asyncio.sleep(delay)
                    continue  # Retry — sdk_options gets rebuilt at top of loop

                else:
                    # Permanent error or retries exhausted — classify and re-raise.
                    # Pass the real CLI stderr + exit code so we don't bury a
                    # transient as 'permanent_error'.
                    error_type = _classify_error(
                        query_exc, stderr=live_stderr, exit_code=exit_code,
                    )
                    try:
                        conn = await db.get_db()
                        await conn.execute(
                            "UPDATE agent_sessions SET retry_count=?, last_error=?, error_type=? WHERE id=?",
                            (retry_count, str(query_exc)[:500], error_type, session_id),
                        )
                        await conn.commit()
                        await conn.close()
                    except Exception:
                        pass

                    if retry_count > 0:
                        log.error(
                            "Session %s: exhausted %d retries (error_type=%s). Last error: %s",
                            session_id, retry_count, error_type, query_exc,
                        )
                    raise

        # ── Agent stream ended without raising. NOT yet 'completed' — must validate. ──
        full_output = "".join(output_chunks)
        if existing_output:
            full_output = existing_output + "\n--- RESUMED ---\n" + full_output
        ended_at = datetime.now(timezone.utc).isoformat()

        # Pick up structured report (MCP submit_report tool, then text-marker fallback)
        report_data = _read_report_file(session_id)
        if not report_data:
            report_data = _parse_report_from_text(full_output)

        # cwd-escape detection: did the agent write files into master's working
        # tree instead of the isolated worktree? Files there get lost on next
        # worktree creation since worktrees are spawned from HEAD, not the WD.
        escape_recovered_files: list[str] = []
        escape_error_msg = ""
        if worktree_path:
            try:
                post_master_status = await _master_status_lines(project_path)
                escaped = post_master_status - pre_master_status
                if escaped:
                    ok_rec, rec_msg, rec_files = await _recover_cwd_escape(
                        project_path, session_id, escaped,
                    )
                    if ok_rec:
                        escape_recovered_files = rec_files
                    else:
                        escape_error_msg = rec_msg
            except Exception as e:
                log.warning("Failed cwd-escape check/recovery for %s: %s", session_id, e)

        # Did the worktree branch accumulate real commits?
        worktree_had_commits = False
        if worktree_path:
            try:
                worktree_had_commits = await _worktree_had_commits(project_path, session_id)
            except Exception as e:
                log.warning("Failed to check worktree commits for %s: %s", session_id, e)

        # Validate real work was done. Recovered cwd-escape files count as "work
        # on master", so treat as worktree_had_commits=True for validation purposes.
        validation_ok, validation_reason = _validate_completion(
            total_tokens, total_cost, output_chunks, report_data,
            worktree_had_commits or bool(escape_recovered_files),
        )

        # Try to merge if validation passed; on merge failure → mission fails as merge_blocked
        merge_failed = False
        merge_error_msg = ""
        if validation_ok and worktree_path:
            merge_result = await cleanup_worktree(project_path, session_id, merge=True)
            if merge_result is False:
                merge_failed = True
                merge_error_msg = (
                    f"Auto-merge of devfleet/{session_id[:8]} into master failed "
                    f"(uncommitted changes or conflict). Branch and worktree preserved "
                    f"at {worktree_path}. Use recover_mission to retry from chat."
                )
        elif not validation_ok and worktree_path:
            log.warning(
                "Session %s validation failed; preserving worktree at %s for inspection",
                session_id, worktree_path,
            )

        # Determine final status
        if not validation_ok:
            final_status = "failed"
            error_type = (
                _classify_error(last_transient_error) if last_transient_error else "no_work_done"
            )
            last_error = validation_reason
        elif merge_failed:
            final_status = "failed"
            error_type = "merge_blocked"
            last_error = merge_error_msg
        elif escape_error_msg:
            # Recovery itself failed — flag clearly
            final_status = "failed"
            error_type = "cwd_escape_unrecovered"
            last_error = (
                f"Agent wrote outside the worktree but auto-recovery failed: {escape_error_msg}. "
                f"Manual recovery needed on the DGX."
            )
        else:
            final_status = "completed"
            error_type = "cwd_escape_recovered" if escape_recovered_files else ""
            last_error = (
                f"Agent's writes bypassed the worktree (likely a model-specific tool-cwd "
                f"quirk). Auto-recovered {len(escape_recovered_files)} file(s) by direct "
                f"commit on master: {', '.join(escape_recovered_files[:5])}"
                f"{'...' if len(escape_recovered_files) > 5 else ''}"
            ) if escape_recovered_files else ""

        conn = await db.get_db()
        try:
            await conn.execute(
                """UPDATE agent_sessions
                   SET status=?, ended_at=?, exit_code=?, output_log=?,
                       total_cost_usd=?, total_tokens=?, last_error=?, error_type=?
                   WHERE id=?""",
                (final_status, ended_at, 0 if final_status == "completed" else 1,
                 full_output, total_cost, total_tokens,
                 last_error, error_type, session_id),
            )
            await conn.execute(
                "UPDATE missions SET status=?, updated_at=? WHERE id=?",
                (final_status, ended_at, mission["id"]),
            )

            # Only store report on a clean success
            if report_data and final_status == "completed":
                report_id = str(uuid.uuid4())
                await conn.execute(
                    """INSERT INTO reports
                       (id, session_id, mission_id, files_changed, what_done, what_open,
                        what_tested, what_untested, next_steps, errors_encountered, preview_url)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (report_id, session_id, mission["id"],
                     report_data["files_changed"], report_data["what_done"],
                     report_data["what_open"], report_data["what_tested"],
                     report_data["what_untested"], report_data["next_steps"],
                     report_data["errors_encountered"], report_data.get("preview_url", "")),
                )

            # Store conversation messages for future resume context
            await conn.execute(
                """INSERT OR REPLACE INTO conversations
                   (session_id, messages_json, updated_at)
                   VALUES (?, ?, ?)""",
                (session_id, json.dumps([{"role": "user", "content": prompt}] +
                    [{"role": "assistant", "content": c} for c in output_chunks]),
                 ended_at),
            )

            await conn.commit()
        finally:
            await conn.close()

        broadcast_payload = {
            "type": "done", "status": final_status,
            "exit_code": 0 if final_status == "completed" else 1,
            "cost": total_cost, "tokens": total_tokens,
        }
        if final_status == "failed":
            broadcast_payload["error"] = last_error
            broadcast_payload["error_type"] = error_type
        _broadcast(session_id, broadcast_payload)

        log.info(
            "Session %s %s (cost $%.4f, tokens %d, error_type=%s)",
            session_id, final_status, total_cost, total_tokens, error_type or "none",
        )

        # Fire post_complete or post_fail hooks + webhook callback
        from plugins import run_hooks
        if final_status == "completed":
            await run_hooks("post_complete", mission, report_data or {})
            await _fire_callback(mission, "completed", report_data)
        else:
            fail_payload = {
                "error": last_error,
                "error_type": error_type,
                "tokens_used": total_tokens,
                "cost_usd": total_cost,
                "retry_count": retry_count,
            }
            await run_hooks("post_fail", mission, fail_payload)
            await _fire_callback(mission, "failed", fail_payload)

    except asyncio.CancelledError:
        is_takeover = session_id in _takeover_sessions
        _takeover_sessions.discard(session_id)
        log.info("Session %s %s", session_id, "taken over" if is_takeover else "cancelled")
        ended_at = datetime.now(timezone.utc).isoformat()
        full_output = "".join(output_chunks)
        if existing_output:
            full_output = existing_output + "\n--- RESUMED ---\n" + full_output

        new_status = "takeover" if is_takeover else "cancelled"
        mission_status = "running" if is_takeover else "failed"
        cancel_reason = "Session taken over for human review" if is_takeover else "Session cancelled"
        cancel_type = "takeover" if is_takeover else "cancelled"
        conn = await db.get_db()
        try:
            await conn.execute(
                """UPDATE agent_sessions SET status=?, ended_at=?, output_log=?,
                       total_cost_usd=?, total_tokens=?, last_error=?, error_type=?
                   WHERE id=?""",
                (new_status, ended_at, full_output, total_cost, total_tokens,
                 cancel_reason, cancel_type, session_id),
            )
            await conn.execute(
                "UPDATE missions SET status=?, updated_at=? WHERE id=?",
                (mission_status, ended_at, mission["id"]),
            )
            await conn.commit()
        finally:
            await conn.close()

        if worktree_path and not is_takeover:
            # Only delete worktree on regular cancel, NOT on takeover
            await cleanup_worktree(project_path, session_id, merge=False)
        _broadcast(session_id, {
            "type": "done", "status": new_status,
            "error": cancel_reason, "error_type": cancel_type,
        })

    except Exception as e:
        # Pull the real CLI stderr we tee'd to a per-session file; this is what
        # the SDK hides behind "Check stderr output for details".
        stderr_tail = _read_stderr_tail(session_id)
        error_type = _classify_error(e, stderr=stderr_tail, exit_code=getattr(e, "exit_code", None))
        log.exception("Session %s error (type=%s): %s", session_id, error_type, e)
        ended_at = datetime.now(timezone.utc).isoformat()
        full_output = "".join(output_chunks)
        if existing_output:
            full_output = existing_output + "\n--- RESUMED ---\n" + full_output

        # Build a human-readable last_error — prefer real stderr over the placeholder
        if error_type == "rate_limit_exhausted":
            last_error = (
                f"Anthropic rate limit hit and {DEFAULT_MAX_RETRIES} retries exhausted. "
                f"Re-dispatch when quota resets. (Last error: {str(e)[:200]})"
            )
        elif error_type == "transient_exhausted":
            last_error = (
                f"Transient error retried {DEFAULT_MAX_RETRIES} times without success. "
                f"(Last error: {str(e)[:200]})"
            )
        elif error_type == "unknown_early_exit":
            last_error = (
                "Claude CLI exited 1 within seconds of launch (likely transient — "
                "API/MCP startup, env, or quota). Retries exhausted. "
                f"Stderr tail: {stderr_tail[-800:] if stderr_tail else '(empty)'}"
            )
        else:
            tail_suffix = f" — stderr: {stderr_tail[-600:]}" if stderr_tail else ""
            last_error = f"Permanent error: {str(e)[:400]}{tail_suffix}"

        # error_log gets the FULL stderr tail (not the placeholder) for postmortem
        error_log_value = stderr_tail or str(e)

        conn = await db.get_db()
        try:
            await conn.execute(
                """UPDATE agent_sessions SET status='failed', ended_at=?, output_log=?, error_log=?,
                       total_cost_usd=?, total_tokens=?, last_error=?, error_type=?
                   WHERE id=?""",
                (ended_at, full_output, error_log_value, total_cost, total_tokens,
                 last_error, error_type, session_id),
            )
            await conn.execute(
                "UPDATE missions SET status='failed', updated_at=? WHERE id=?",
                (ended_at, mission["id"]),
            )
            await conn.commit()
        finally:
            await conn.close()

        # Preserve worktree on rate-limit / transient / early-exit-1 errors so
        # resume_mission can pick up later. Cleanup only on truly permanent
        # errors. Note `unknown_early_exit` is included: these usually self-heal
        # on a re-dispatch, and the SDK occasionally produces them even after
        # the agent did real work in an MCP tool call.
        if worktree_path:
            preserve = error_type in (
                "rate_limit_exhausted", "transient_exhausted", "unknown_early_exit",
            )
            if not preserve:
                await cleanup_worktree(project_path, session_id, merge=False)
            else:
                log.warning(
                    "Session %s worktree preserved at %s for resume (error_type=%s)",
                    session_id, worktree_path, error_type,
                )

        _broadcast(session_id, {
            "type": "done", "status": "failed",
            "error": last_error, "error_type": error_type,
        })

        # Fire post_fail hooks (plugins + webhook callback)
        from plugins import run_hooks
        fail_payload = {"error": last_error, "error_type": error_type,
                        "tokens_used": total_tokens, "cost_usd": total_cost}
        await run_hooks("post_fail", mission, fail_payload)
        await _fire_callback(mission, "failed", fail_payload)

    finally:
        running_tasks.pop(session_id, None)
        _event_buffers.pop(session_id, None)
        if stderr_file is not None:
            try:
                stderr_file.close()
            except Exception:
                pass


async def dispatch_mission(
    session_id: str,
    mission: dict,
    last_report: dict | None,
    opts: DispatchOptions | None = None,
):
    """Spawn an agent via SDK to work on a mission."""
    from app import resolve_path

    full_prompt = build_prompt(mission, last_report)
    project_path = resolve_path(mission["project_path"])

    worktree_path = await create_worktree(project_path, session_id)
    work_dir = worktree_path or project_path

    await _run_agent(
        session_id=session_id,
        mission=mission,
        prompt=full_prompt,
        work_dir=work_dir,
        worktree_path=worktree_path,
        project_path=project_path,
        opts=opts,
    )


async def resume_mission(
    session_id: str,
    mission: dict,
    claude_session_id: str,
    opts: DispatchOptions | None = None,
):
    """Resume a failed agent session via SDK --resume."""
    from app import resolve_path

    project_path = resolve_path(mission["project_path"])

    # Get existing output and costs for accumulation
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT output_log, total_cost_usd, total_tokens FROM agent_sessions WHERE id=?",
            (session_id,),
        )
        existing = dict(rows[0]) if rows else {}
    finally:
        await conn.close()

    worktree_path = await create_worktree(project_path, session_id)
    work_dir = worktree_path or project_path

    await _run_agent(
        session_id=session_id,
        mission=mission,
        prompt="Continue where you left off. Complete the remaining work.",
        work_dir=work_dir,
        worktree_path=worktree_path,
        project_path=project_path,
        opts=opts,
        resume_session_id=claude_session_id,
        existing_output=existing.get("output_log", ""),
        existing_cost=existing.get("total_cost_usd") or 0,
        existing_tokens=existing.get("total_tokens") or 0,
    )


async def cancel_session(session_id: str) -> bool:
    """Cancel a running session."""
    task = running_tasks.get(session_id)
    if task and not task.done():
        task.cancel()
        return True

    # Task not in memory (e.g. after backend restart) — update DB directly
    conn = await db.get_db()
    try:
        row = await conn.execute_fetchall(
            "SELECT status, mission_id FROM agent_sessions WHERE id=?", (session_id,),
        )
        if not row:
            return False
        data = dict(row[0])
        if data["status"] not in ("running", "takeover"):
            return False
        # Mark session as cancelled
        await conn.execute(
            "UPDATE agent_sessions SET status='cancelled', ended_at=datetime('now') WHERE id=?",
            (session_id,),
        )
        # Mark mission as failed
        await conn.execute(
            "UPDATE missions SET status='failed', updated_at=datetime('now') WHERE id=? AND status='running'",
            (data["mission_id"],),
        )
        await conn.commit()
        log.info("Force-cancelled orphaned session %s (no task in memory)", session_id)
        return True
    finally:
        await conn.close()


async def takeover_session(session_id: str) -> dict | None:
    """Take over a running session: cancel the agent but preserve the worktree.

    Returns dict with {work_dir, output_log, claude_session_id} or None if not found.
    Handles both live tasks and orphaned sessions (after backend restart).
    """
    task = running_tasks.get(session_id)
    if task and not task.done():
        # Live task — cancel it gracefully, preserving worktree
        _takeover_sessions.add(session_id)
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass

    # Retrieve the saved session data
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT s.status, s.output_log, s.claude_session_id, s.total_cost_usd, s.total_tokens,
                      m.project_id, m.id AS mission_id, p.path AS project_path
               FROM agent_sessions s
               JOIN missions m ON m.id = s.mission_id
               JOIN projects p ON p.id = m.project_id
               WHERE s.id=?""",
            (session_id,),
        )
        if not rows:
            return None
        data = dict(rows[0])

        # Only allow takeover of running/takeover sessions
        if data["status"] not in ("running", "takeover"):
            return None

        # Update session status to takeover
        await conn.execute(
            "UPDATE agent_sessions SET status='takeover', ended_at=datetime('now') WHERE id=?",
            (session_id,),
        )
        await conn.commit()
    finally:
        await conn.close()

    from app import resolve_path
    project_path = resolve_path(data["project_path"])
    short_id = session_id[:8]
    worktree_path = os.path.join(project_path, ".devfleet-worktrees", f"session-{short_id}")

    # Check if worktree exists
    work_dir = worktree_path if os.path.isdir(worktree_path) else project_path

    return {
        "work_dir": work_dir,
        "output_log": data.get("output_log", ""),
        "claude_session_id": data.get("claude_session_id", ""),
        "total_cost_usd": data.get("total_cost_usd", 0),
        "total_tokens": data.get("total_tokens", 0),
    }


def _parse_report_from_text(output: str) -> dict | None:
    """Backward-compatible: parse report from text markers."""
    start_marker = "---DEVFLEET-REPORT-START---"
    end_marker = "---DEVFLEET-REPORT-END---"

    start_idx = output.find(start_marker)
    end_idx = output.find(end_marker)
    if start_idx == -1 or end_idx == -1:
        return None

    report_text = output[start_idx + len(start_marker):end_idx].strip()
    sections = {}
    current_section = None
    current_content = []

    for line in report_text.split("\n"):
        if line.startswith("## "):
            if current_section:
                sections[current_section] = "\n".join(current_content).strip()
            header = line[3:].strip().lower().replace("'", "").replace("\u2019", "")
            current_section = header
            current_content = []
        else:
            current_content.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_content).strip()

    return {
        "files_changed": sections.get("files changed", ""),
        "what_done": sections.get("whats done", ""),
        "what_open": sections.get("whats open", ""),
        "what_tested": sections.get("whats tested", ""),
        "what_untested": sections.get("whats not tested", ""),
        "next_steps": sections.get("next steps", ""),
        "errors_encountered": sections.get("errors encountered", ""),
        "preview_url": sections.get("preview", ""),
    }
