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
import time
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
from claude_code_sdk._errors import MessageParseError, ProcessError

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
from models import TOOL_PRESETS, LANE_DEFAULTS, MISSION_TYPE_TO_LANE, MODEL_CHOICES, DispatchOptions

# Max wallclock time per session before watchdog cancels it (default 90 min)
_MAX_SESSION_SECONDS = int(os.getenv("DEVFLEET_MAX_SESSION_MINUTES", "90")) * 60
# How often to flush last_activity_at to DB (seconds) — avoids per-message writes
_ACTIVITY_FLUSH_INTERVAL = 60

log = logging.getLogger("devfleet.sdk_engine")

# ── In-memory state (same pattern as old dispatcher) ──
running_tasks: dict[str, asyncio.Task] = {}
_subscribers: dict[str, list[asyncio.Queue]] = {}
_event_buffers: dict[str, list[dict]] = {}
# Sessions being taken over — worktree is preserved on cancel
_takeover_sessions: set[str] = {}

# ── Lane helpers (lightweight; full accounting lives in lanes.py) ──
# TTL-cached async DB lookup — DB is source of truth for lane prompts after Prompt Studio edits.
# Falls back to LANE_DEFAULTS if the DB is unavailable.
_LANE_PROMPT_CACHE: dict[str, tuple[str, float]] = {}  # name → (text, expires_at)
_LANE_PROMPT_TTL = 60  # seconds


async def _get_lane_prompt_text(lane_name: str) -> str:
    """Fetch the assembled lane prompt text from DB (TTL-cached 60s). Falls back to LANE_DEFAULTS."""
    import time as _t
    cached = _LANE_PROMPT_CACHE.get(lane_name)
    if cached and cached[1] > _t.monotonic():
        return cached[0]
    try:
        conn = await db.get_db()
        try:
            row = await (await conn.execute(
                "SELECT append_prompt FROM lanes WHERE name = ?", (lane_name,)
            )).fetchone()
        finally:
            await conn.close()
        if row and row[0]:
            from lanes import parse_prompt_json, assemble_prompt_text
            text = assemble_prompt_text(parse_prompt_json(row[0]))
        else:
            text = LANE_DEFAULTS.get(lane_name, {}).get("append_prompt", "")
    except Exception:
        text = LANE_DEFAULTS.get(lane_name, {}).get("append_prompt", "")
    _LANE_PROMPT_CACHE[lane_name] = (text, _t.monotonic() + _LANE_PROMPT_TTL)
    return text


def _derive_lane(mission: dict) -> str:
    """Resolve the effective lane for a mission (mirrors lanes.derive_lane)."""
    lane = (mission.get("lane") or "").strip()
    if lane:
        return lane
    return MISSION_TYPE_TO_LANE.get(mission.get("mission_type", "implement"), "coder")


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


async def _build_sdk_options(
    mission: dict,
    opts: DispatchOptions | None,
    work_dir: str,
    project_path: str = "",
    session_id: str = "",
    resume_session_id: str | None = None,
    extra_mcp_servers: dict | None = None,
) -> ClaudeCodeOptions:
    """Build ClaudeCodeOptions from mission config + dispatch overrides."""

    # Model selection: override > mission > lane default > hardcoded default
    # Validate against MODEL_CHOICES to reject stale/retired model names
    model = "claude-sonnet-4-6"
    if opts and opts.model and opts.model in MODEL_CHOICES:
        model = opts.model
    elif opts and opts.model:
        log.warning("Dispatch override model '%s' not in MODEL_CHOICES — using lane default", opts.model)

    if model == "claude-sonnet-4-6":  # not yet overridden
        stored = mission.get("model", "")
        if stored and stored in MODEL_CHOICES:
            model = stored
        elif stored:
            # Stale model name (e.g. claude-sonnet-4-20250514, claude-opus-4-6)
            # Fall through to lane default below
            log.warning("Mission model '%s' is stale/unknown — resolving from lane default", stored)

        if model == "claude-sonnet-4-6":  # still not resolved — try lane default
            lane = _derive_lane(mission)
            lane_model = LANE_DEFAULTS.get(lane, {}).get("default_model", "")
            if lane_model and lane_model in MODEL_CHOICES:
                model = lane_model

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

    # Allow DevFleet MCP tools (full default set; filtered below by lane_mcp_tools)
    lane_name = _derive_lane(mission)
    _all_devfleet_tools = [
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
    ]
    allowed_tools.extend(_all_devfleet_tools)

    # Filter MCP tools based on per-lane lane_mcp_tools DB settings
    _tool_hints: list[str] = []
    try:
        from lanes import get_lane_mcp_tools as _get_mcp_tools
        mcp_tool_rows = await _get_mcp_tools(lane_name)
        if mcp_tool_rows:
            disabled = {
                f"mcp__{r['server_name']}__{r['tool_name']}"
                for r in mcp_tool_rows if not r["enabled"]
            }
            if disabled:
                allowed_tools = [t for t in allowed_tools if t not in disabled]
            _tool_hints = [
                f"- {r['server_name']}/{r['tool_name']}: {r['trigger_hint']}"
                for r in mcp_tool_rows
                if r.get("trigger_hint") and r["trigger_hint"] != "always" and r["enabled"]
            ]
    except Exception as _mcp_err:
        log.debug("MCP tool filtering skipped: %s", _mcp_err)

    # System prompt: lane policy (DB) → dispatch override → compact instruction
    lane_prompt = await _get_lane_prompt_text(lane_name)
    compact_instruction = (
        "\n\nCONTEXT MANAGEMENT: When your context window approaches 199,000 tokens, "
        "immediately run /compact before continuing. Do not wait for it to fill completely."
    )
    if opts and opts.append_system_prompt:
        append_prompt = opts.append_system_prompt + compact_instruction
    elif lane_prompt:
        append_prompt = lane_prompt + compact_instruction
    else:
        append_prompt = compact_instruction.strip()

    # Inject MCP tool usage hints into the system prompt
    if _tool_hints:
        append_prompt += "\n\nMCP Tool Usage Guidelines:\n" + "\n".join(_tool_hints)

    # Max turns: worktree missions default to 200 (high complexity tolerance)
    max_turns = None
    if opts and opts.max_turns:
        max_turns = opts.max_turns
    elif mission.get("max_turns"):
        max_turns = mission["max_turns"]
    # High worktree complexity tolerance — no turn limit cap when isolated
    if max_turns is None and work_dir != project_path:
        max_turns = 200

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

    # ECC global access — agents can read ~/.claude skills, memory, and CLAUDE.md
    # regardless of which repo or worktree they run in
    global_ecc = os.path.expanduser("~/.claude")
    add_dirs: list[str] = []
    if os.path.isdir(global_ecc):
        add_dirs.append(global_ecc)

    # Learn-eval Stop hook — every agent session extracts reusable patterns
    learn_eval_cmd = f"claude -p '/learn-eval' --output-format=text 2>/dev/null || true"
    stop_hooks = {
        "Stop": [
            {
                "matcher": "*",
                "hooks": [{"type": "command", "command": learn_eval_cmd}],
            }
        ]
    }

    kwargs = dict(
        model=model,
        max_turns=max_turns,
        allowed_tools=allowed_tools,
        append_system_prompt=append_prompt,
        permission_mode="bypassPermissions",
        cwd=work_dir,
        resume=resume_session_id,
        include_partial_messages=False,
        hooks=stop_hooks,
    )
    if mcp_servers:
        kwargs["mcp_servers"] = mcp_servers
    if add_dirs:
        kwargs["add_dirs"] = add_dirs
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
    worktree_branch: str | None = None,
):
    """Unified agent runner for both dispatch and resume."""
    output_chunks = []
    total_cost = existing_cost
    total_tokens = existing_tokens
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_read_tokens = 0
    total_cache_creation_tokens = 0
    last_cost_flush = time.time()

    # Initialize event buffer
    _event_buffers[session_id] = []

    # Tracks whether the SDK actually started streaming (distinguishes dispatch-layer from agent-layer failures)
    agent_started = False
    session_timed_out = False

    # Watchdog: cancel this task if agent exceeds the max wallclock limit
    async def _watchdog():
        nonlocal session_timed_out
        await asyncio.sleep(_MAX_SESSION_SECONDS)
        session_timed_out = True
        log.warning(
            "Session %s exceeded %d-minute limit — cancelling (stuck agent or hung subprocess)",
            session_id, _MAX_SESSION_SECONDS // 60,
        )
        current = asyncio.current_task()
        parent = running_tasks.get(session_id)
        if parent and parent is not current:
            parent.cancel()

    watchdog_task = asyncio.create_task(_watchdog())

    try:
        # Load per-project MCP configs from DB
        extra_mcp = await _load_project_mcp_configs(mission.get("project_id", ""))

        sdk_options = await _build_sdk_options(
            mission, opts, work_dir,
            project_path=project_path,
            session_id=session_id,
            resume_session_id=resume_session_id,
            extra_mcp_servers=extra_mcp or None,
        )
        model_used = sdk_options.model or "claude-sonnet-4-6"

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
        last_activity_flush = time.time()

        # Stream messages from the SDK
        # Wrap in safe iterator to skip unparseable events (e.g. rate_limit_event)
        async for message in _safe_query(prompt=prompt, options=sdk_options):
            agent_started = True  # SDK yielded at least one message — agent actually spawned

            # Throttled heartbeat — flushes activity timestamp + running cost to DB once per minute
            now = time.time()
            if now - last_activity_flush >= _ACTIVITY_FLUSH_INTERVAL:
                last_activity_flush = now
                try:
                    _conn = await db.get_db()
                    await _conn.execute(
                        """UPDATE agent_sessions
                           SET last_activity_at=?, total_cost_usd=?, total_tokens=?
                           WHERE id=?""",
                        (datetime.now(timezone.utc).isoformat(),
                         total_cost, total_tokens, session_id),
                    )
                    await _conn.commit()
                    await _conn.close()
                    # Broadcast live cost update to frontend
                    if total_cost > 0:
                        _broadcast(session_id, {"type": "cost_update", "cost": total_cost, "tokens": total_tokens})
                except Exception:
                    pass
            if isinstance(message, SystemMessage):
                # Capture session ID for resume capability
                claude_session_id = message.data.get("session_id", "") or ""
                if claude_session_id:
                    log.info("Session %s → Claude session ID: %s", session_id, claude_session_id)
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

                # Extract usage — SDK returns Usage object or dict; handle both
                usage = message.usage
                cost = message.total_cost_usd or 0.0

                def _get(obj, key, default=0):
                    if obj is None:
                        return default
                    if isinstance(obj, dict):
                        return obj.get(key, default) or default
                    return getattr(obj, key, default) or default

                input_t = _get(usage, "input_tokens")
                output_t = _get(usage, "output_tokens")
                cache_read_t = _get(usage, "cache_read_input_tokens")
                cache_create_t = _get(usage, "cache_creation_input_tokens")

                # SDK input_tokens is already non-cached input only.
                # cache_read_input_tokens and cache_creation_input_tokens are tracked separately.
                # total_tokens = input + output (display); cache cols hold full cache breakdown.
                total_input_tokens += input_t
                total_output_tokens += output_t
                total_cache_read_tokens += cache_read_t
                total_cache_creation_tokens += cache_create_t
                total_tokens = existing_tokens + total_input_tokens + total_output_tokens
                total_cost = existing_cost + cost

                _broadcast(session_id, {
                    "type": "usage",
                    "usage": {
                        "input_tokens": billable_input,
                        "output_tokens": output_t,
                        "cache_read_tokens": cache_read_t,
                        "cache_creation_tokens": cache_create_t,
                    },
                    "cost": total_cost,
                })

        # Agent finished successfully
        full_output = "".join(output_chunks)
        if existing_output:
            full_output = existing_output + "\n--- RESUMED ---\n" + full_output
        ended_at = datetime.now(timezone.utc).isoformat()

        # Check for report from MCP submit_report tool (writes JSON file)
        report_data = _read_report_file(session_id)
        # Fallback: try parsing from text markers (backward compat)
        if not report_data:
            report_data = _parse_report_from_text(full_output)

        conn = await db.get_db()
        try:
            await conn.execute(
                """UPDATE agent_sessions
                   SET status='completed', ended_at=?, exit_code=0, output_log=?,
                       total_cost_usd=?, total_tokens=?,
                       cache_read_tokens=?, cache_creation_tokens=?
                   WHERE id=?""",
                (ended_at, full_output, total_cost, total_tokens,
                 total_cache_read_tokens, total_cache_creation_tokens, session_id),
            )
            await conn.execute(
                "UPDATE missions SET status='completed', updated_at=? WHERE id=?",
                (ended_at, mission["id"]),
            )

            if report_data:
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

        # Cleanup worktree — auto-merge if successful
        if worktree_path:
            await cleanup_worktree(project_path, session_id, merge=True, branch_name=worktree_branch)

        watchdog_task.cancel()
        _broadcast(session_id, {
            "type": "done", "status": "completed", "exit_code": 0,
            "cost": total_cost, "tokens": total_tokens,
        })
        log.info("Session %s completed (cost $%.4f, tokens %d)", session_id, total_cost, total_tokens)

    except asyncio.CancelledError:
        watchdog_task.cancel()
        is_takeover = session_id in _takeover_sessions
        _takeover_sessions.discard(session_id)
        cancel_reason = "timed out" if session_timed_out else ("taken over" if is_takeover else "cancelled")
        log.info("Session %s %s", session_id, cancel_reason)
        ended_at = datetime.now(timezone.utc).isoformat()
        full_output = "".join(output_chunks)
        if existing_output:
            full_output = existing_output + "\n--- RESUMED ---\n" + full_output

        new_status = "timed_out" if session_timed_out else ("takeover" if is_takeover else "cancelled")
        mission_status = "running" if is_takeover else "failed"
        conn = await db.get_db()
        try:
            await conn.execute(
                """UPDATE agent_sessions SET status=?, ended_at=?, output_log=?,
                       total_cost_usd=?, total_tokens=?,
                       cache_read_tokens=?, cache_creation_tokens=?
                   WHERE id=?""",
                (new_status, ended_at, full_output, total_cost, total_tokens,
                 total_cache_read_tokens, total_cache_creation_tokens, session_id),
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
            await cleanup_worktree(project_path, session_id, merge=False, branch_name=worktree_branch)
        _broadcast(session_id, {"type": "done", "status": new_status})

    except Exception as e:
        watchdog_task.cancel()
        failure_layer = "agent" if agent_started else "dispatch"
        # Detect OOM kills: SIGKILL from the OS surfaces as ProcessError with exit_code=-9
        oom_killed = isinstance(e, ProcessError) and getattr(e, "exit_code", None) == -9
        if oom_killed:
            log.error(
                "Session %s OOM-killed (SIGKILL, exit -9) — agent process killed by OS memory pressure",
                session_id,
            )
        else:
            log.exception("Session %s %s-layer error: %s", session_id, failure_layer, e)
        ended_at = datetime.now(timezone.utc).isoformat()
        full_output = "".join(output_chunks)
        if existing_output:
            full_output = existing_output + "\n--- RESUMED ---\n" + full_output

        conn = await db.get_db()
        try:
            await conn.execute(
                """UPDATE agent_sessions SET status='failed', ended_at=?, output_log=?, error_log=?,
                       total_cost_usd=?, total_tokens=?,
                       cache_read_tokens=?, cache_creation_tokens=?
                   WHERE id=?""",
                (ended_at, full_output, str(e), total_cost, total_tokens,
                 total_cache_read_tokens, total_cache_creation_tokens, session_id),
            )
            await conn.execute(
                "UPDATE missions SET status='failed', updated_at=? WHERE id=?",
                (ended_at, mission["id"]),
            )
            # Record failure layer for observability and downstream classification
            await conn.execute(
                "INSERT INTO mission_events (mission_id, event_type, data, failure_layer) VALUES (?, ?, ?, ?)",
                (mission["id"], "session_failed",
                 json.dumps({"session_id": session_id, "error": str(e)[:500],
                             "oom_killed": oom_killed}),
                 failure_layer),
            )
            await conn.commit()

            # Dispatch-layer failures are orchestrator bugs, not agent bugs.
            # Auto-retry once with a short backoff so transient errors (e.g. hot-reload races)
            # self-heal. Agent-layer failures need code changes — never auto-retry.
            if failure_layer == "dispatch":
                prior_failures = await conn.execute(
                    """SELECT COUNT(*) FROM mission_events
                       WHERE mission_id=? AND event_type='session_failed'
                         AND failure_layer='dispatch'
                         AND created_at >= datetime('now', '-10 minutes')""",
                    (mission["id"],),
                )
                row = await prior_failures.fetchone()
                prior_count = row[0] if row else 0
                if prior_count <= 1:
                    log.warning(
                        "Mission %s dispatch failure #%d — scheduling auto-retry in 10s",
                        mission["id"], prior_count,
                    )
                    asyncio.create_task(_dispatch_retry(mission["id"], delay=10))
        finally:
            await conn.close()

        if worktree_path:
            await cleanup_worktree(project_path, session_id, merge=False, branch_name=worktree_branch)
        _broadcast(session_id, {"type": "done", "status": "failed", "error": str(e),
                                  "failure_layer": failure_layer})

    finally:
        running_tasks.pop(session_id, None)
        _event_buffers.pop(session_id, None)


async def _dispatch_retry(mission_id: str, delay: int = 10):
    """Reset a dispatch-failed mission to 'draft' after a short delay so mission_watcher retries it."""
    await asyncio.sleep(delay)
    try:
        conn = await db.get_db()
        try:
            await conn.execute(
                "UPDATE missions SET status='draft', updated_at=datetime('now') WHERE id=? AND status='failed'",
                (mission_id,),
            )
            await conn.commit()
            log.info("Mission %s reset to draft for dispatch retry", mission_id)
        finally:
            await conn.close()
    except Exception as err:
        log.warning("Failed to reset mission %s for retry: %s", mission_id, err)


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

    worktree_path, worktree_branch = await create_worktree(project_path, session_id, mission=mission)
    work_dir = worktree_path or project_path

    if worktree_branch:
        _conn = await db.get_db()
        try:
            await _conn.execute(
                "UPDATE agent_sessions SET branch_name=? WHERE id=?",
                (worktree_branch, session_id),
            )
            await _conn.commit()
        finally:
            await _conn.close()

    await _run_agent(
        session_id=session_id,
        mission=mission,
        prompt=full_prompt,
        work_dir=work_dir,
        worktree_path=worktree_path,
        project_path=project_path,
        opts=opts,
        worktree_branch=worktree_branch,
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

    worktree_path, worktree_branch = await create_worktree(project_path, session_id, mission=mission)
    work_dir = worktree_path or project_path

    if worktree_branch:
        _conn = await db.get_db()
        try:
            await _conn.execute(
                "UPDATE agent_sessions SET branch_name=? WHERE id=?",
                (worktree_branch, session_id),
            )
            await _conn.commit()
        finally:
            await _conn.close()

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
        worktree_branch=worktree_branch,
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
