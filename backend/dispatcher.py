import asyncio
import json
import logging
import os
import signal
import uuid
from datetime import datetime, timezone

import db
from prompt_template import build_prompt
from worktree import create_worktree, cleanup_worktree, is_git_repo
from models import TOOL_PRESETS, DispatchOptions

log = logging.getLogger("devfleet.dispatcher")

# Track running agent tasks and processes
running_tasks: dict[str, asyncio.Task] = {}
_processes: dict[str, asyncio.subprocess.Process] = {}
_subscribers: dict[str, list[asyncio.Queue]] = {}
_event_buffers: dict[str, list[dict]] = {}  # in-memory event history per session


def _build_cli_args(mission: dict, opts: DispatchOptions | None = None) -> list[str]:
    """Build Claude CLI arguments from mission config + dispatch overrides."""
    args = [
        "claude",
        "--print",
        "--dangerously-skip-permissions",
        "--verbose",
        "--output-format", "stream-json",
    ]

    # Model: override > mission > default
    model = "claude-opus-4-6"
    if opts and opts.model:
        model = opts.model
    elif mission.get("model"):
        model = mission["model"]
    args += ["--model", model]

    # Max turns
    max_turns = None
    if opts and opts.max_turns:
        max_turns = opts.max_turns
    elif mission.get("max_turns"):
        max_turns = mission["max_turns"]
    if max_turns:
        args += ["--max-turns", str(max_turns)]

    # Max budget
    max_budget = None
    if opts and opts.max_budget_usd:
        max_budget = opts.max_budget_usd
    elif mission.get("max_budget_usd"):
        max_budget = mission["max_budget_usd"]
    if max_budget:
        args += ["--max-budget-usd", str(max_budget)]

    # Allowed tools: override > preset > mission config
    tools = None
    if opts and opts.allowed_tools:
        tools = opts.allowed_tools
    elif opts and opts.tool_preset and opts.tool_preset in TOOL_PRESETS:
        tools = TOOL_PRESETS[opts.tool_preset]
    elif mission.get("allowed_tools"):
        raw = mission["allowed_tools"]
        # Check if it's a preset name
        if raw in TOOL_PRESETS:
            tools = TOOL_PRESETS[raw]
        else:
            try:
                tools = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
    elif mission.get("mission_type") and mission["mission_type"] in TOOL_PRESETS:
        tools = TOOL_PRESETS[mission["mission_type"]]

    if tools:
        for tool in tools:
            args += ["--allowedTools", tool]

    # Append system prompt
    if opts and opts.append_system_prompt:
        args += ["--append-system-prompt", opts.append_system_prompt]

    # Fork session (for resume branching)
    if opts and opts.fork_session:
        args.append("--fork-session")

    return args


async def subscribe_session(session_id: str):
    """Async generator that yields SSE events for a session."""
    queue = asyncio.Queue()
    _subscribers.setdefault(session_id, []).append(queue)
    try:
        # If session is still running, replay buffered events first
        if session_id in _event_buffers:
            for evt in _event_buffers[session_id]:
                yield evt
        else:
            # Session not in memory — check DB for completed sessions
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


def _broadcast(session_id: str, event: dict):
    """Push event to all subscribers and buffer for late joiners."""
    if session_id in _event_buffers:
        _event_buffers[session_id].append(event)
    for queue in _subscribers.get(session_id, []):
        queue.put_nowait(event)


def _broadcast_tool_use(session_id: str, block: dict):
    """Format and broadcast a tool_use block."""
    tool_name = block.get("name", "unknown")
    tool_input = block.get("input", {})
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


def _broadcast_tool_result(session_id: str, block: dict):
    """Format and broadcast a tool_result block."""
    content = block.get("content", "")
    is_error = block.get("is_error", False)
    result_text = ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item["text"])
        result_text = "\n".join(parts)
    elif isinstance(content, str):
        result_text = content
    if not result_text:
        return
    # Truncate very long results
    if len(result_text) > 1500:
        result_text = result_text[:1500] + "\n... (truncated)"
    prefix = "ERROR: " if is_error else ""
    _broadcast(session_id, {"type": "tool_result", "text": prefix + result_text + "\n"})


async def dispatch_mission(session_id: str, mission: dict, last_report: dict | None,
                           opts: DispatchOptions | None = None):
    """Spawn claude CLI and manage the agent session lifecycle."""
    from app import resolve_path

    full_prompt = build_prompt(mission, last_report)
    project_path = resolve_path(mission["project_path"])
    output_chunks = []
    worktree_path = None
    total_cost = 0.0
    total_tokens = 0

    # Initialize event buffer for this session
    _event_buffers[session_id] = []

    try:
        # Create isolated worktree if project is a git repo
        worktree_path = await create_worktree(project_path, session_id)
        work_dir = worktree_path or project_path

        # Build CLI args from mission config + dispatch overrides
        cli_args = _build_cli_args(mission, opts)
        cli_args += ["-p", full_prompt]

        model_used = "claude-opus-4-6"
        if opts and opts.model:
            model_used = opts.model
        elif mission.get("model"):
            model_used = mission["model"]

        log.info("Dispatching session %s for mission '%s' in %s (model: %s, worktree: %s)",
                 session_id, mission["title"], work_dir, model_used, worktree_path is not None)
        log.info("CLI args: %s", " ".join(cli_args))

        # Broadcast dispatch config to frontend
        _broadcast(session_id, {
            "type": "config",
            "model": model_used,
            "max_turns": (opts and opts.max_turns) or mission.get("max_turns"),
            "max_budget_usd": (opts and opts.max_budget_usd) or mission.get("max_budget_usd"),
            "mission_type": mission.get("mission_type", "implement"),
        })

        process = await asyncio.create_subprocess_exec(
            *cli_args,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
            limit=10 * 1024 * 1024,
        )
        _processes[session_id] = process

        claude_session_id = ""

        # Read stdout line by line (Claude CLI stream-json format)
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")

            try:
                event = json.loads(text)
                etype = event.get("type", "")

                # Capture Claude session ID from system init event
                if etype == "system":
                    claude_session_id = event.get("session_id", "")
                    if claude_session_id:
                        log.info("Session %s → Claude session ID: %s", session_id, claude_session_id)
                        # Persist immediately so resume works even if process dies
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
                    continue

                # Skip rate limit and empty events
                if etype in ("rate_limit_event", ""):
                    continue

                # Assistant message — contains text, tool_use, and thinking blocks
                if etype == "assistant":
                    content = event.get("message", {}).get("content", [])
                    for block in content:
                        btype = block.get("type")

                        if btype == "text":
                            chunk = block["text"]
                            output_chunks.append(chunk)
                            _broadcast(session_id, {"type": "text", "text": "\n" + chunk + "\n"})

                        elif btype == "tool_use":
                            _broadcast_tool_use(session_id, block)

                        # Skip thinking blocks silently

                # User message — contains tool_result blocks
                elif etype == "user":
                    content = event.get("message", {}).get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if block.get("type") == "tool_result":
                                _broadcast_tool_result(session_id, block)

                # Final result with usage stats
                elif etype == "result":
                    chunk = event.get("result", "")
                    if chunk:
                        combined = "".join(output_chunks)
                        if chunk not in combined:
                            output_chunks.append(chunk)
                            _broadcast(session_id, {"type": "text", "text": "\n" + chunk})
                    cost = event.get("total_cost_usd")
                    usage = event.get("usage", {})
                    if cost:
                        total_cost = cost
                    if usage:
                        total_tokens = (usage.get("input_tokens", 0) or 0) + (usage.get("output_tokens", 0) or 0)
                    if cost or usage:
                        _broadcast(session_id, {"type": "usage", "usage": usage, "cost": cost})

                # Other events — skip silently

            except json.JSONDecodeError:
                stripped = text.strip()
                if stripped:
                    output_chunks.append(text)
                    _broadcast(session_id, {"type": "text", "text": text})

        # Wait for process to finish
        await process.wait()
        exit_code = process.returncode

        # Capture stderr
        stderr_data = await process.stderr.read()
        error_log = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""

        full_output = "".join(output_chunks)
        ended_at = datetime.now(timezone.utc).isoformat()
        status = "completed" if exit_code == 0 else "failed"
        mission_status = "completed" if exit_code == 0 else "failed"

        # Parse report from output
        report_data = parse_report(full_output)

        # Update DB with cost tracking
        conn = await db.get_db()
        try:
            await conn.execute(
                """UPDATE agent_sessions
                   SET status=?, ended_at=?, exit_code=?, output_log=?, error_log=?,
                       total_cost_usd=?, total_tokens=?
                   WHERE id=?""",
                (status, ended_at, exit_code, full_output, error_log,
                 total_cost, total_tokens, session_id),
            )
            await conn.execute(
                "UPDATE missions SET status=?, updated_at=? WHERE id=?",
                (mission_status, ended_at, mission["id"]),
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
                     report_data["errors_encountered"], report_data["preview_url"]),
                )
            await conn.commit()
        finally:
            await conn.close()

        # Cleanup worktree — auto-merge if successful
        if worktree_path:
            merge = (exit_code == 0)
            await cleanup_worktree(project_path, session_id, merge=merge)

        _broadcast(session_id, {"type": "done", "status": status, "exit_code": exit_code,
                                "cost": total_cost, "tokens": total_tokens})
        log.info("Session %s finished: %s (exit %d, cost $%.4f, tokens %d)",
                 session_id, status, exit_code, total_cost, total_tokens)

    except asyncio.CancelledError:
        log.info("Session %s cancelled", session_id)
        if session_id in _processes:
            try:
                _processes[session_id].terminate()
                await asyncio.sleep(2)
                if _processes[session_id].returncode is None:
                    _processes[session_id].kill()
            except ProcessLookupError:
                pass

        conn = await db.get_db()
        try:
            ended_at = datetime.now(timezone.utc).isoformat()
            full_output = "".join(output_chunks)
            await conn.execute(
                """UPDATE agent_sessions SET status='cancelled', ended_at=?, output_log=?,
                       total_cost_usd=?, total_tokens=?
                   WHERE id=?""",
                (ended_at, full_output, total_cost, total_tokens, session_id),
            )
            await conn.execute(
                "UPDATE missions SET status='failed', updated_at=? WHERE id=?",
                (ended_at, mission["id"]),
            )
            await conn.commit()
        finally:
            await conn.close()

        # Discard worktree on cancel (don't merge)
        if worktree_path:
            await cleanup_worktree(project_path, session_id, merge=False)

        _broadcast(session_id, {"type": "done", "status": "cancelled"})

    except Exception as e:
        log.exception("Session %s error: %s", session_id, e)
        conn = await db.get_db()
        try:
            ended_at = datetime.now(timezone.utc).isoformat()
            full_output = "".join(output_chunks)
            await conn.execute(
                """UPDATE agent_sessions SET status='failed', ended_at=?, output_log=?, error_log=?,
                       total_cost_usd=?, total_tokens=?
                   WHERE id=?""",
                (ended_at, full_output, str(e), total_cost, total_tokens, session_id),
            )
            await conn.execute(
                "UPDATE missions SET status='failed', updated_at=? WHERE id=?",
                (ended_at, mission["id"]),
            )
            await conn.commit()
        finally:
            await conn.close()

        # Discard worktree on error
        if worktree_path:
            await cleanup_worktree(project_path, session_id, merge=False)

        _broadcast(session_id, {"type": "done", "status": "failed", "error": str(e)})

    finally:
        _processes.pop(session_id, None)
        running_tasks.pop(session_id, None)
        _event_buffers.pop(session_id, None)


async def resume_mission(session_id: str, mission: dict, claude_session_id: str,
                         opts: DispatchOptions | None = None):
    """Resume a failed Claude session using --resume flag."""
    from app import resolve_path

    project_path = resolve_path(mission["project_path"])
    output_chunks = []
    worktree_path = None
    total_cost = 0.0
    total_tokens = 0

    # Initialize event buffer for this session
    _event_buffers[session_id] = []

    try:
        # Use same worktree if project is git repo
        worktree_path = await create_worktree(project_path, session_id)
        work_dir = worktree_path or project_path

        # Build CLI args with resume flag
        cli_args = _build_cli_args(mission, opts)
        cli_args += ["--resume", claude_session_id]

        log.info("Resuming session %s for mission '%s' (claude session: %s)",
                 session_id, mission["title"], claude_session_id)

        process = await asyncio.create_subprocess_exec(
            *cli_args,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
            limit=10 * 1024 * 1024,
        )
        _processes[session_id] = process

        new_claude_session_id = ""

        # Read stdout — same parsing as dispatch_mission
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")

            try:
                event = json.loads(text)
                etype = event.get("type", "")

                if etype == "system":
                    new_claude_session_id = event.get("session_id", "")
                    if new_claude_session_id:
                        try:
                            conn = await db.get_db()
                            await conn.execute(
                                "UPDATE agent_sessions SET claude_session_id=? WHERE id=?",
                                (new_claude_session_id, session_id),
                            )
                            await conn.commit()
                            await conn.close()
                        except Exception:
                            pass
                    continue

                if etype in ("rate_limit_event", ""):
                    continue

                if etype == "assistant":
                    content = event.get("message", {}).get("content", [])
                    for block in content:
                        btype = block.get("type")
                        if btype == "text":
                            chunk = block["text"]
                            output_chunks.append(chunk)
                            _broadcast(session_id, {"type": "text", "text": "\n" + chunk + "\n"})
                        elif btype == "tool_use":
                            _broadcast_tool_use(session_id, block)

                elif etype == "user":
                    content = event.get("message", {}).get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if block.get("type") == "tool_result":
                                _broadcast_tool_result(session_id, block)

                elif etype == "result":
                    chunk = event.get("result", "")
                    if chunk:
                        combined = "".join(output_chunks)
                        if chunk not in combined:
                            output_chunks.append(chunk)
                            _broadcast(session_id, {"type": "text", "text": "\n" + chunk})
                    cost = event.get("total_cost_usd")
                    usage = event.get("usage", {})
                    if cost:
                        total_cost = cost
                    if usage:
                        total_tokens = (usage.get("input_tokens", 0) or 0) + (usage.get("output_tokens", 0) or 0)
                    if cost or usage:
                        _broadcast(session_id, {"type": "usage", "usage": usage, "cost": cost})

            except json.JSONDecodeError:
                stripped = text.strip()
                if stripped:
                    output_chunks.append(text)
                    _broadcast(session_id, {"type": "text", "text": text})

        await process.wait()
        exit_code = process.returncode

        stderr_data = await process.stderr.read()
        error_log = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""

        full_output = "".join(output_chunks)
        ended_at = datetime.now(timezone.utc).isoformat()
        status = "completed" if exit_code == 0 else "failed"
        mission_status = "completed" if exit_code == 0 else "failed"

        report_data = parse_report(full_output)

        conn = await db.get_db()
        try:
            # Append to existing output_log
            rows = await conn.execute_fetchall(
                "SELECT output_log, total_cost_usd, total_tokens FROM agent_sessions WHERE id=?", (session_id,)
            )
            existing = dict(rows[0]) if rows else {}
            existing_output = existing.get("output_log", "")
            combined_output = existing_output + "\n--- RESUMED ---\n" + full_output if existing_output else full_output
            # Accumulate costs across resumes
            accumulated_cost = (existing.get("total_cost_usd") or 0) + total_cost
            accumulated_tokens = (existing.get("total_tokens") or 0) + total_tokens

            await conn.execute(
                """UPDATE agent_sessions
                   SET status=?, ended_at=?, exit_code=?, output_log=?, error_log=?,
                       total_cost_usd=?, total_tokens=?
                   WHERE id=?""",
                (status, ended_at, exit_code, combined_output, error_log,
                 accumulated_cost, accumulated_tokens, session_id),
            )
            await conn.execute(
                "UPDATE missions SET status=?, updated_at=? WHERE id=?",
                (mission_status, ended_at, mission["id"]),
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
                     report_data["errors_encountered"], report_data["preview_url"]),
                )
            await conn.commit()
        finally:
            await conn.close()

        if worktree_path:
            merge = (exit_code == 0)
            await cleanup_worktree(project_path, session_id, merge=merge)

        _broadcast(session_id, {"type": "done", "status": status, "exit_code": exit_code,
                                "cost": total_cost, "tokens": total_tokens})
        log.info("Resumed session %s finished: %s (exit %d, cost $%.4f)",
                 session_id, status, exit_code, total_cost)

    except asyncio.CancelledError:
        log.info("Resumed session %s cancelled", session_id)
        if session_id in _processes:
            try:
                _processes[session_id].terminate()
                await asyncio.sleep(2)
                if _processes[session_id].returncode is None:
                    _processes[session_id].kill()
            except ProcessLookupError:
                pass

        conn = await db.get_db()
        try:
            ended_at = datetime.now(timezone.utc).isoformat()
            rows = await conn.execute_fetchall(
                "SELECT output_log FROM agent_sessions WHERE id=?", (session_id,)
            )
            existing_output = dict(rows[0])["output_log"] if rows else ""
            full_output = "".join(output_chunks)
            combined_output = existing_output + "\n--- RESUMED ---\n" + full_output if existing_output else full_output
            await conn.execute(
                "UPDATE agent_sessions SET status='cancelled', ended_at=?, output_log=? WHERE id=?",
                (ended_at, combined_output, session_id),
            )
            await conn.execute(
                "UPDATE missions SET status='failed', updated_at=? WHERE id=?",
                (ended_at, mission["id"]),
            )
            await conn.commit()
        finally:
            await conn.close()

        if worktree_path:
            await cleanup_worktree(project_path, session_id, merge=False)

        _broadcast(session_id, {"type": "done", "status": "cancelled"})

    except Exception as e:
        log.exception("Resumed session %s error: %s", session_id, e)
        conn = await db.get_db()
        try:
            ended_at = datetime.now(timezone.utc).isoformat()
            rows = await conn.execute_fetchall(
                "SELECT output_log FROM agent_sessions WHERE id=?", (session_id,)
            )
            existing_output = dict(rows[0])["output_log"] if rows else ""
            full_output = "".join(output_chunks)
            combined_output = existing_output + "\n--- RESUMED ---\n" + full_output if existing_output else full_output
            await conn.execute(
                "UPDATE agent_sessions SET status='failed', ended_at=?, output_log=?, error_log=? WHERE id=?",
                (ended_at, combined_output, str(e), session_id),
            )
            await conn.execute(
                "UPDATE missions SET status='failed', updated_at=? WHERE id=?",
                (ended_at, mission["id"]),
            )
            await conn.commit()
        finally:
            await conn.close()

        if worktree_path:
            await cleanup_worktree(project_path, session_id, merge=False)

        _broadcast(session_id, {"type": "done", "status": "failed", "error": str(e)})

    finally:
        _processes.pop(session_id, None)
        running_tasks.pop(session_id, None)
        _event_buffers.pop(session_id, None)


async def cancel_session(session_id: str) -> bool:
    """Cancel a running session."""
    task = running_tasks.get(session_id)
    if not task or task.done():
        return False
    task.cancel()
    return True


def parse_report(output: str) -> dict | None:
    """Parse structured report from agent output using markers."""
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
            header = line[3:].strip().lower()
            # Normalize section names
            header = header.replace("'", "").replace("\u2019", "")
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
