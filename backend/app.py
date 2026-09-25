import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import db
from models import (ProjectCreate, ProjectUpdate, MissionCreate, MissionUpdate,
                    DispatchOptions, TOOL_PRESETS, MODEL_CHOICES,
                    ServiceCreate, ServiceUpdate, IncidentCreate, IncidentUpdate,
                    McpServerCreate)
import health_checker
import mission_watcher
import scheduler
from autoloop import start_auto_loop, stop_auto_loop, get_auto_loop_status
from remote_control import (start_remote_control, stop_remote_control,
                            get_remote_status, list_remote_sessions, cleanup_all as cleanup_remote,
                            subscribe_remote_session,
                            RemoteControlNotEnabled, WorkspaceNotTrusted)

# Feature flags
ENABLE_REMOTE_CONTROL = os.environ.get("DEVFLEET_ENABLE_REMOTE_CONTROL", "false").lower() == "true"

# SDK engine is the new default; fall back to CLI dispatcher if SDK unavailable
USE_SDK_ENGINE = os.environ.get("DEVFLEET_ENGINE", "sdk").lower() == "sdk"
if USE_SDK_ENGINE:
    try:
        from sdk_engine import dispatch_mission, resume_mission, cancel_session, takeover_session, running_tasks
        log_engine = "sdk"
    except ImportError:
        from dispatcher import dispatch_mission, resume_mission, cancel_session, running_tasks
        log_engine = "cli (sdk_engine import failed)"
        USE_SDK_ENGINE = False
else:
    from dispatcher import dispatch_mission, resume_mission, cancel_session, running_tasks
    log_engine = "cli"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("devfleet")

# Path mapping: host paths ↔ container paths
# e.g. /home/user/my-project → /workspace/my-project (inside Docker)
_PATH_MAPS = []
for env_key, env_val in os.environ.items():
    if env_key.startswith("DEVFLEET_PATH_MAP_"):
        # Format: HOST_PATH:CONTAINER_PATH
        parts = env_val.split(":", 1)
        if len(parts) == 2:
            _PATH_MAPS.append((parts[0], parts[1]))


def resolve_path(path: str) -> str:
    """Translate a host path to a container path if running in Docker."""
    for host_prefix, container_prefix in _PATH_MAPS:
        if path.startswith(host_prefix):
            return path.replace(host_prefix, container_prefix, 1)
    return path


def reverse_path(path: str) -> str:
    """Translate a container path back to a host path for display."""
    for host_prefix, container_prefix in _PATH_MAPS:
        if path.startswith(container_prefix):
            return path.replace(container_prefix, host_prefix, 1)
    return path


@asynccontextmanager
async def lifespan(app):
    await db.init_db()
    await health_checker.start_checker()
    await mission_watcher.start_watcher()
    await scheduler.start_scheduler()
    # Load plugins (custom tools, hooks, extensions)
    from plugins import load_plugins
    load_plugins()
    log.info("Claude DevFleet API started — DB initialized at %s (engine: %s)", db.DB_PATH, log_engine)
    yield
    await scheduler.stop_scheduler()
    await mission_watcher.stop_watcher()
    await health_checker.stop_checker()
    for sid, task in list(running_tasks.items()):
        task.cancel()
    await cleanup_remote()
    log.info("Claude DevFleet API shutting down")


app = FastAPI(title="Claude DevFleet API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_CONCURRENT_AGENTS = int(os.environ.get("DEVFLEET_MAX_AGENTS", "3"))


# ──────────────────────────────────────────────
# MCP Server — External integration endpoint
# ──────────────────────────────────────────────
# Any MCP-compatible client can connect to DevFleet via:
#   Streamable HTTP: { "type": "http",  "url": "http://localhost:18801/mcp" }
#   SSE (legacy):    { "type": "sse",   "url": "http://localhost:18801/mcp/sse" }

from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp_external import server as mcp_server
from starlette.routing import Route, Mount

# ── SSE transport (legacy, backward-compatible) ──
_mcp_sse = SseServerTransport("/messages/")


# Starlette treats classes as raw ASGI apps (scope, receive, send)
# while functions get wrapped with Request objects (which hide `send`).
# Both transports need raw ASGI access, so we use classes.

class _McpSseEndpoint:
    async def __call__(self, scope, receive, send):
        try:
            async with _mcp_sse.connect_sse(scope, receive, send) as streams:
                await mcp_server.run(
                    streams[0], streams[1],
                    mcp_server.create_initialization_options(),
                )
        except Exception:
            log.exception("MCP SSE session error")


class _McpPostEndpoint:
    async def __call__(self, scope, receive, send):
        try:
            await _mcp_sse.handle_post_message(scope, receive, send)
        except Exception:
            log.exception("MCP POST handler error")


# ── Streamable HTTP transport (preferred) ──
# Handles GET (SSE stream) and POST (JSON-RPC) on a single endpoint.
# Each session gets its own transport instance, keyed by mcp-session-id header.

_http_transports: dict[str, StreamableHTTPServerTransport] = {}
_http_ready: dict[str, asyncio.Event] = {}


async def _ensure_http_transport(session_id: str) -> StreamableHTTPServerTransport:
    """Get or create a Streamable HTTP transport, ensuring connect() is ready."""
    if session_id in _http_transports:
        # Wait for existing transport to be ready
        await _http_ready[session_id].wait()
        return _http_transports[session_id]

    transport = StreamableHTTPServerTransport(mcp_session_id=session_id)
    _http_transports[session_id] = transport
    _http_ready[session_id] = asyncio.Event()

    async def _run_server():
        try:
            async with transport.connect() as streams:
                _http_ready[session_id].set()
                await mcp_server.run(
                    streams[0], streams[1],
                    mcp_server.create_initialization_options(),
                )
        except Exception:
            log.exception("MCP HTTP session error")
        finally:
            _http_transports.pop(session_id, None)
            _http_ready.pop(session_id, None)

    asyncio.create_task(_run_server())
    await _http_ready[session_id].wait()
    return transport


class _McpHttpEndpoint:
    """Streamable HTTP MCP endpoint — handles GET, POST, DELETE on /mcp."""

    async def __call__(self, scope, receive, send):
        import uuid as _uuid
        from starlette.requests import Request

        request = Request(scope, receive, send)
        session_id = request.headers.get("mcp-session-id")

        if request.method == "DELETE":
            if session_id and session_id in _http_transports:
                transport = _http_transports.pop(session_id)
                _http_ready.pop(session_id, None)
                await transport.terminate()
            return

        # For GET and POST, ensure transport exists and is ready
        if not session_id:
            session_id = str(_uuid.uuid4())
        transport = await _ensure_http_transport(session_id)
        await transport.handle_request(scope, receive, send)


app.mount("/mcp", Mount(path="", routes=[
    # Streamable HTTP — single endpoint for GET/POST/DELETE
    Route("/", endpoint=_McpHttpEndpoint(), methods=["GET", "POST", "DELETE"]),
    # SSE (legacy) — backward-compatible endpoints
    Route("/sse", endpoint=_McpSseEndpoint()),
    Route("/messages/", endpoint=_McpPostEndpoint(), methods=["POST"]),
]))


# ──────────────────────────────────────────────
# Projects
# ──────────────────────────────────────────────

@app.get("/api/projects")
async def list_projects():
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT p.*,
                      COUNT(m.id) AS mission_count,
                      SUM(CASE WHEN m.status='running' THEN 1 ELSE 0 END) AS running_count,
                      SUM(CASE WHEN m.status='completed' THEN 1 ELSE 0 END) AS completed_count
               FROM projects p
               LEFT JOIN missions m ON m.project_id = p.id
               GROUP BY p.id
               ORDER BY p.created_at DESC"""
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


@app.post("/api/projects", status_code=201)
async def create_project(body: ProjectCreate):
    # Store the original host path, but validate the resolved (container) path
    resolved = resolve_path(body.path)
    if not os.path.isdir(resolved):
        raise HTTPException(400, f"Path does not exist: {body.path}")
    pid = str(uuid.uuid4())
    conn = await db.get_db()
    try:
        await conn.execute(
            """INSERT INTO projects
               (id, name, path, description, system_prompt,
                state, owner, start_date, target_end_date,
                parent_team, teams_channel_id, teams_channel_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pid, body.name, body.path, body.description, body.system_prompt,
             body.state or "active", body.owner, body.start_date, body.target_end_date,
             body.parent_team, body.teams_channel_id, body.teams_channel_name),
        )
        await conn.commit()
        row = await conn.execute_fetchall("SELECT * FROM projects WHERE id=?", (pid,))
        return dict(row[0])
    finally:
        await conn.close()


@app.get("/api/projects/{pid}")
async def get_project(pid: str):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM projects WHERE id=?", (pid,))
        if not rows:
            raise HTTPException(404, "Project not found")
        project = dict(rows[0])
        missions = await conn.execute_fetchall(
            "SELECT * FROM missions WHERE project_id=? ORDER BY priority DESC, created_at DESC",
            (pid,),
        )
        project["missions"] = [dict(m) for m in missions]
        return project
    finally:
        await conn.close()


@app.put("/api/projects/{pid}")
async def update_project(pid: str, body: ProjectUpdate):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM projects WHERE id=?", (pid,))
        if not rows:
            raise HTTPException(404, "Project not found")
        updates = body.model_dump(exclude_none=True)
        if not updates:
            return dict(rows[0])
        if "path" in updates and not os.path.isdir(resolve_path(updates["path"])):
            raise HTTPException(400, f"Path does not exist: {updates['path']}")
        sets = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [pid]
        await conn.execute(f"UPDATE projects SET {sets} WHERE id=?", vals)
        await conn.commit()
        row = await conn.execute_fetchall("SELECT * FROM projects WHERE id=?", (pid,))
        return dict(row[0])
    finally:
        await conn.close()


@app.delete("/api/projects/{pid}")
async def delete_project(pid: str):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM projects WHERE id=?", (pid,))
        if not rows:
            raise HTTPException(404, "Project not found")
        await conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        await conn.commit()
        return {"ok": True}
    finally:
        await conn.close()


# ──────────────────────────────────────────────
# Missions
# ──────────────────────────────────────────────

@app.get("/api/missions")
async def list_missions(
    project_id: str = Query(None),
    status: str = Query(None),
    tag: str = Query(None),
    parent_mission_id: str = Query(None),
):
    conn = await db.get_db()
    try:
        query = """SELECT m.*, p.name AS project_name
                   FROM missions m
                   JOIN projects p ON p.id = m.project_id
                   WHERE 1=1"""
        params = []
        if project_id:
            query += " AND m.project_id=?"
            params.append(project_id)
        if status:
            query += " AND m.status=?"
            params.append(status)
        if parent_mission_id:
            query += " AND m.parent_mission_id=?"
            params.append(parent_mission_id)
        query += " ORDER BY m.priority DESC, m.created_at DESC"
        rows = await conn.execute_fetchall(query, params)
        results = []
        for r in rows:
            d = dict(r)
            if tag:
                tags = json.loads(d.get("tags", "[]"))
                if tag not in tags:
                    continue
            results.append(d)
        return results
    finally:
        await conn.close()


@app.post("/api/missions", status_code=201)
async def create_mission(body: MissionCreate):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT id FROM projects WHERE id=?", (body.project_id,))
        if not rows:
            raise HTTPException(400, "Project not found")
        mid = str(uuid.uuid4())
        schedule_enabled = 1 if body.schedule_cron else 0
        # Get next mission number for this project
        num_rows = await conn.execute_fetchall(
            "SELECT COALESCE(MAX(mission_number), 0) + 1 AS next_num FROM missions WHERE project_id=?",
            (body.project_id,),
        )
        next_num = num_rows[0][0] if num_rows else 1
        await conn.execute(
            """INSERT INTO missions (id, project_id, title, detailed_prompt, acceptance_criteria,
               priority, tags, model, max_turns, max_budget_usd, allowed_tools, mission_type,
               parent_mission_id, depends_on, auto_dispatch, schedule_cron, schedule_enabled, mission_number)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (mid, body.project_id, body.title, body.detailed_prompt,
             body.acceptance_criteria, body.priority, json.dumps(body.tags),
             body.model, body.max_turns, body.max_budget_usd,
             body.allowed_tools or "", body.mission_type,
             body.parent_mission_id, json.dumps(body.depends_on),
             1 if body.auto_dispatch else 0, body.schedule_cron, schedule_enabled, next_num),
        )
        await conn.commit()
        row = await conn.execute_fetchall(
            "SELECT m.*, p.name AS project_name FROM missions m JOIN projects p ON p.id=m.project_id WHERE m.id=?",
            (mid,),
        )
        return dict(row[0])
    finally:
        await conn.close()


@app.get("/api/missions/{mid}")
async def get_mission(mid: str):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT m.*, p.name AS project_name, p.path AS project_path FROM missions m JOIN projects p ON p.id=m.project_id WHERE m.id=?",
            (mid,),
        )
        if not rows:
            raise HTTPException(404, "Mission not found")
        mission = dict(rows[0])

        sessions = await conn.execute_fetchall(
            "SELECT * FROM agent_sessions WHERE mission_id=? ORDER BY started_at DESC",
            (mid,),
        )
        mission["sessions"] = [dict(s) for s in sessions]

        reports = await conn.execute_fetchall(
            "SELECT * FROM reports WHERE mission_id=? ORDER BY created_at DESC LIMIT 1",
            (mid,),
        )
        mission["latest_report"] = dict(reports[0]) if reports else None

        # Phase 3: child missions
        children = await conn.execute_fetchall(
            "SELECT id, title, status, mission_type FROM missions WHERE parent_mission_id=? ORDER BY created_at",
            (mid,),
        )
        mission["children"] = [dict(c) for c in children]

        return mission
    finally:
        await conn.close()


@app.get("/api/missions/{mid}/children")
async def list_children(mid: str):
    """List all child/sub-missions of a mission."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT m.*, p.name AS project_name
               FROM missions m
               JOIN projects p ON p.id = m.project_id
               WHERE m.parent_mission_id=?
               ORDER BY m.priority DESC, m.created_at""",
            (mid,),
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


@app.get("/api/missions/{mid}/report")
async def get_mission_report(mid: str, log_tail_bytes: int = 4096):
    """Flattened single-call mission retrieval — status + structured report
    + cost + exit code + tail of agent output_log. Designed so an MCP wrapper
    can surface a mission result with one HTTP call instead of three.

    log_tail_bytes caps how much of agent_sessions.output_log is returned
    (default 4KB) — enough to surface the agent's final chat reply when no
    structured report was written, without blowing up the response."""
    conn = await db.get_db()
    try:
        m_rows = await conn.execute_fetchall(
            "SELECT id, title, status, model, created_at, updated_at "
            "FROM missions WHERE id=?",
            (mid,),
        )
        if not m_rows:
            raise HTTPException(404, "Mission not found")
        m = dict(m_rows[0])

        s_rows = await conn.execute_fetchall(
            "SELECT id, status, started_at, ended_at, exit_code, output_log, "
            "error_log, total_cost_usd, total_tokens "
            "FROM agent_sessions WHERE mission_id=? "
            "ORDER BY started_at DESC LIMIT 1",
            (mid,),
        )
        s = dict(s_rows[0]) if s_rows else {}

        r_rows = await conn.execute_fetchall(
            "SELECT files_changed, what_done, what_open, what_tested, "
            "what_untested, next_steps, errors_encountered, preview_url, "
            "created_at FROM reports WHERE mission_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (mid,),
        )
        rep = dict(r_rows[0]) if r_rows else None

        output_log = s.get("output_log") or ""
        error_log = s.get("error_log") or ""

        return {
            "mission_id": mid,
            "title": m.get("title"),
            "status": m.get("status"),
            "model": m.get("model"),
            "created_at": m.get("created_at"),
            "updated_at": m.get("updated_at"),
            "session": {
                "session_id": s.get("id"),
                "started_at": s.get("started_at"),
                "ended_at": s.get("ended_at"),
                "exit_code": s.get("exit_code"),
                "total_cost_usd": s.get("total_cost_usd"),
                "total_tokens": s.get("total_tokens"),
                "error_log_tail": error_log[-log_tail_bytes:] if error_log else None,
                "output_log_tail": output_log[-log_tail_bytes:] if output_log else None,
                "output_log_truncated": len(output_log) > log_tail_bytes,
            } if s else None,
            "report": rep,
        }
    finally:
        await conn.close()


@app.put("/api/missions/{mid}")
async def update_mission(mid: str, body: MissionUpdate):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM missions WHERE id=?", (mid,))
        if not rows:
            raise HTTPException(404, "Mission not found")
        updates = body.model_dump(exclude_none=True)
        if not updates:
            return dict(rows[0])
        if "tags" in updates:
            updates["tags"] = json.dumps(updates["tags"])
        if "depends_on" in updates:
            updates["depends_on"] = json.dumps(updates["depends_on"])
        if "auto_dispatch" in updates:
            updates["auto_dispatch"] = 1 if updates["auto_dispatch"] else 0
        if "schedule_enabled" in updates:
            updates["schedule_enabled"] = 1 if updates["schedule_enabled"] else 0
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        sets = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [mid]
        await conn.execute(f"UPDATE missions SET {sets} WHERE id=?", vals)
        await conn.commit()
        row = await conn.execute_fetchall(
            "SELECT m.*, p.name AS project_name FROM missions m JOIN projects p ON p.id=m.project_id WHERE m.id=?",
            (mid,),
        )
        return dict(row[0])
    finally:
        await conn.close()


@app.delete("/api/missions/{mid}")
async def delete_mission(mid: str):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT status FROM missions WHERE id=?", (mid,))
        if not rows:
            raise HTTPException(404, "Mission not found")
        if dict(rows[0])["status"] == "running":
            raise HTTPException(400, "Cannot delete a running mission — cancel it first")
        await conn.execute("DELETE FROM missions WHERE id=?", (mid,))
        await conn.commit()
        return {"ok": True}
    finally:
        await conn.close()


# ──────────────────────────────────────────────
# Generate Next Mission from Report
# ──────────────────────────────────────────────

@app.post("/api/missions/{mid}/generate-next")
async def generate_next_mission(mid: str):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT m.*, p.name AS project_name FROM missions m JOIN projects p ON p.id=m.project_id WHERE m.id=?",
            (mid,),
        )
        if not rows:
            raise HTTPException(404, "Mission not found")
        mission = dict(rows[0])

        reports = await conn.execute_fetchall(
            "SELECT * FROM reports WHERE mission_id=? ORDER BY created_at DESC LIMIT 1",
            (mid,),
        )
        if not reports:
            raise HTTPException(400, "No report found for this mission — dispatch it first")
        report = dict(reports[0])

        # Build the next mission from report data
        what_open = report.get("what_open", "").strip()
        what_untested = report.get("what_untested", "").strip()
        next_steps = report.get("next_steps", "").strip()
        what_done = report.get("what_done", "").strip()
        errors = report.get("errors_encountered", "").strip()

        # Derive title from next_steps first line, or fallback
        title_line = ""
        for line in (next_steps or what_open or "").split("\n"):
            cleaned = line.strip().lstrip("-•* ")
            if cleaned:
                title_line = cleaned
                break
        new_title = title_line[:80] if title_line else f"Continue: {mission['title']}"

        # Build detailed prompt with full context
        prompt_parts = [f"## Context from Previous Mission: {mission['title']}\n"]

        if what_done:
            prompt_parts.append(f"### Already Completed\n{what_done}\n")
        if errors and errors.lower() not in ("none", "- none", "n/a", ""):
            prompt_parts.append(f"### Errors from Previous Session (fix these first)\n{errors}\n")
        if what_open and what_open.lower() not in ("none", "- none", "n/a", ""):
            prompt_parts.append(f"### Open Items to Complete\n{what_open}\n")
        if what_untested and what_untested.lower() not in ("none", "- none", "n/a", ""):
            prompt_parts.append(f"### Needs Testing\n{what_untested}\n")

        prompt_parts.append("## Your Task\n")
        if next_steps:
            prompt_parts.append(next_steps)
        elif what_open:
            prompt_parts.append(f"Complete the remaining open items:\n{what_open}")
        else:
            prompt_parts.append("Review the completed work and add tests/improvements as needed.")

        # Build acceptance criteria from untested + open items
        criteria_parts = []
        if what_untested and what_untested.lower() not in ("none", "- none", "n/a", ""):
            criteria_parts.append(f"Test coverage for:\n{what_untested}")
        if what_open and what_open.lower() not in ("none", "- none", "n/a", ""):
            criteria_parts.append(f"Complete:\n{what_open}")

        # Create the new mission as draft
        new_id = str(uuid.uuid4())
        tags = mission.get("tags", "[]")
        num_rows = await conn.execute_fetchall(
            "SELECT COALESCE(MAX(mission_number), 0) + 1 AS next_num FROM missions WHERE project_id=?",
            (mission["project_id"],),
        )
        next_num = num_rows[0][0] if num_rows else 1
        await conn.execute(
            """INSERT INTO missions (id, project_id, title, detailed_prompt, acceptance_criteria, priority, tags, mission_number)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (new_id, mission["project_id"], new_title,
             "\n".join(prompt_parts),
             "\n".join(criteria_parts) if criteria_parts else "",
             mission.get("priority", 0), tags, next_num),
        )
        await conn.commit()

        row = await conn.execute_fetchall(
            "SELECT m.*, p.name AS project_name FROM missions m JOIN projects p ON p.id=m.project_id WHERE m.id=?",
            (new_id,),
        )
        return dict(row[0])
    finally:
        await conn.close()


# ──────────────────────────────────────────────
# Dispatch
# ──────────────────────────────────────────────

@app.post("/api/missions/{mid}/dispatch")
async def dispatch(mid: str, body: DispatchOptions | None = None):
    running_count = sum(1 for t in running_tasks.values() if not t.done())
    if running_count >= MAX_CONCURRENT_AGENTS:
        raise HTTPException(429, f"Max {MAX_CONCURRENT_AGENTS} concurrent agents — wait for one to finish")

    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT m.*, p.path AS project_path FROM missions m JOIN projects p ON p.id=m.project_id WHERE m.id=?",
            (mid,),
        )
        if not rows:
            raise HTTPException(404, "Mission not found")
        mission = dict(rows[0])
        if mission["status"] == "running":
            raise HTTPException(400, "Mission already running")

        # Get last report for context
        reports = await conn.execute_fetchall(
            "SELECT * FROM reports WHERE mission_id=? ORDER BY created_at DESC LIMIT 1",
            (mid,),
        )
        last_report = dict(reports[0]) if reports else None

        session_id = str(uuid.uuid4())
        model_used = (body and body.model) or mission.get("model") or "claude-opus-4-6"
        await conn.execute(
            "INSERT INTO agent_sessions (id, mission_id, model) VALUES (?, ?, ?)",
            (session_id, mid, model_used),
        )
        await conn.execute(
            "UPDATE missions SET status='running', updated_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), mid),
        )
        await conn.commit()
    finally:
        await conn.close()

    task = asyncio.create_task(
        dispatch_mission(session_id, mission, last_report, opts=body)
    )
    running_tasks[session_id] = task

    return {"session_id": session_id, "status": "running", "model": model_used}


@app.post("/api/missions/{mid}/resume")
async def resume(mid: str, body: DispatchOptions | None = None):
    """Resume a failed mission from its last Claude session."""
    running_count = sum(1 for t in running_tasks.values() if not t.done())
    if running_count >= MAX_CONCURRENT_AGENTS:
        raise HTTPException(429, f"Max {MAX_CONCURRENT_AGENTS} concurrent agents — wait for one to finish")

    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT m.*, p.path AS project_path FROM missions m JOIN projects p ON p.id=m.project_id WHERE m.id=?",
            (mid,),
        )
        if not rows:
            raise HTTPException(404, "Mission not found")
        mission = dict(rows[0])
        if mission["status"] == "running":
            raise HTTPException(400, "Mission already running")

        # Find the last session with a Claude session ID
        sessions = await conn.execute_fetchall(
            """SELECT id, claude_session_id, status FROM agent_sessions
               WHERE mission_id=? AND claude_session_id != '' AND claude_session_id IS NOT NULL
               ORDER BY started_at DESC LIMIT 1""",
            (mid,),
        )
        if not sessions:
            raise HTTPException(400, "No resumable session found — dispatch a new one instead")
        last_session = dict(sessions[0])
        claude_sid = last_session["claude_session_id"]
        session_id = last_session["id"]

        # Reset session status for the resume
        await conn.execute(
            "UPDATE agent_sessions SET status='running', ended_at=NULL, exit_code=NULL, error_log='' WHERE id=?",
            (session_id,),
        )
        await conn.execute(
            "UPDATE missions SET status='running', updated_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), mid),
        )
        await conn.commit()
    finally:
        await conn.close()

    task = asyncio.create_task(
        resume_mission(session_id, mission, claude_sid, opts=body)
    )
    running_tasks[session_id] = task

    return {"session_id": session_id, "status": "running", "resumed": True}


@app.post("/api/missions/{mid}/recover")
async def recover_mission_endpoint(mid: str):
    """Recover a merge_blocked or cwd_escape_unrecovered mission: stash uncommitted
    master changes, merge devfleet/<short-id>, restore stash. Idempotent —
    safe to call twice."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT m.*, p.path AS project_path FROM missions m JOIN projects p ON p.id=m.project_id WHERE m.id=?",
            (mid,),
        )
        if not rows:
            raise HTTPException(404, "Mission not found")
        mission = dict(rows[0])

        sess_rows = await conn.execute_fetchall(
            """SELECT id, error_type, last_error FROM agent_sessions
               WHERE mission_id=? ORDER BY started_at DESC LIMIT 1""",
            (mid,),
        )
        if not sess_rows:
            raise HTTPException(400, "No session to recover")
        sess = dict(sess_rows[0])
        if sess.get("error_type") not in ("merge_blocked", "cwd_escape_unrecovered"):
            raise HTTPException(
                400,
                f"Mission error_type is {sess.get('error_type') or 'none'}; "
                f"recover only works on merge_blocked or cwd_escape_unrecovered",
            )
        session_id = sess["id"]
    finally:
        await conn.close()

    project_path = resolve_path(mission["project_path"])
    short_id = session_id[:8]
    branch_name = f"devfleet/{short_id}"
    worktree_path = os.path.join(project_path, ".devfleet-worktrees", f"session-{short_id}")

    async def _git(*args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=project_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode, out.decode().strip(), err.decode().strip()

    # Verify the branch still exists
    code, _, _ = await _git("rev-parse", "--verify", branch_name)
    if code != 0:
        raise HTTPException(
            410,
            f"Recovery branch {branch_name} no longer exists. "
            f"Re-dispatch the mission as a new one.",
        )

    # Stash master's working tree if dirty (untracked + tracked)
    code, status_out, _ = await _git("status", "--porcelain")
    stashed = False
    if status_out.strip():
        code, _, err = await _git("stash", "push", "-u", "-m", f"devfleet recover {short_id}")
        if code != 0:
            raise HTTPException(500, f"git stash failed: {err[:300]}")
        stashed = True

    # Try to merge
    code, _, merge_err = await _git(
        "merge", "--no-ff", "-m", f"Claude DevFleet: recover-merge session {short_id}", branch_name,
    )
    if code != 0:
        await _git("merge", "--abort")
        if stashed:
            await _git("stash", "pop")
        raise HTTPException(
            409,
            f"Merge still failed: {merge_err[:400]}. "
            f"There's a real conflict between master and the agent's branch — "
            f"manual resolution needed on the DGX.",
        )

    # Merge succeeded. Clean up worktree + branch.
    await _git("worktree", "remove", "--force", worktree_path)
    await _git("branch", "-D", branch_name)

    # Restore stash if we stashed
    pop_warning = ""
    if stashed:
        code, _, pop_err = await _git("stash", "pop")
        if code != 0:
            pop_warning = (
                f"Merge succeeded but git stash pop had conflicts: {pop_err[:200]}. "
                f"Your previously-uncommitted master changes are still in `git stash list` — "
                f"resolve them manually."
            )

    # Update DB to reflect successful recovery
    conn = await db.get_db()
    try:
        await conn.execute(
            """UPDATE agent_sessions SET status='completed', exit_code=0,
                   last_error=?, error_type=''
               WHERE id=?""",
            (
                f"Recovered via /api/missions/{mid}/recover" + (f" — {pop_warning}" if pop_warning else ""),
                session_id,
            ),
        )
        await conn.execute(
            "UPDATE missions SET status='completed', updated_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), mid),
        )
        await conn.commit()
    finally:
        await conn.close()

    return {
        "ok": True,
        "mission_id": mid,
        "branch_merged": branch_name,
        "stash_warning": pop_warning,
    }


# ──────────────────────────────────────────────
# Sessions
# ──────────────────────────────────────────────

@app.get("/api/sessions")
async def list_sessions(mission_id: str = Query(None), status: str = Query(None)):
    conn = await db.get_db()
    try:
        query = """SELECT s.*, m.title AS mission_title, p.name AS project_name
                   FROM agent_sessions s
                   JOIN missions m ON m.id = s.mission_id
                   JOIN projects p ON p.id = m.project_id
                   WHERE 1=1"""
        params = []
        if mission_id:
            query += " AND s.mission_id=?"
            params.append(mission_id)
        if status:
            query += " AND s.status=?"
            params.append(status)
        query += " ORDER BY s.started_at DESC"
        rows = await conn.execute_fetchall(query, params)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


@app.get("/api/sessions/{sid}")
async def get_session(sid: str):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT s.*, m.title AS mission_title, m.id AS mission_id, m.mission_number
               FROM agent_sessions s
               JOIN missions m ON m.id = s.mission_id
               WHERE s.id=?""",
            (sid,),
        )
        if not rows:
            raise HTTPException(404, "Session not found")
        session = dict(rows[0])
        reports = await conn.execute_fetchall(
            "SELECT * FROM reports WHERE session_id=?", (sid,)
        )
        session["report"] = dict(reports[0]) if reports else None
        return session
    finally:
        await conn.close()


@app.get("/api/sessions/{sid}/stream")
async def stream_session(sid: str):
    if USE_SDK_ENGINE:
        from sdk_engine import subscribe_session
    else:
        from dispatcher import subscribe_session

    async def event_stream():
        async for event in subscribe_session(sid):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/sessions/{sid}/cancel")
async def cancel(sid: str):
    result = await cancel_session(sid)
    if not result:
        raise HTTPException(404, "Session not running")
    return {"ok": True}


# ──────────────────────────────────────────────
# Reports
# ──────────────────────────────────────────────

@app.get("/api/reports")
async def list_reports(project_id: str = Query(None), mission_id: str = Query(None)):
    conn = await db.get_db()
    try:
        query = """SELECT r.*, m.title AS mission_title, p.name AS project_name
                   FROM reports r
                   JOIN missions m ON m.id = r.mission_id
                   JOIN projects p ON p.id = m.project_id
                   WHERE 1=1"""
        params = []
        if mission_id:
            query += " AND r.mission_id=?"
            params.append(mission_id)
        if project_id:
            query += " AND m.project_id=?"
            params.append(project_id)
        query += " ORDER BY r.created_at DESC"
        rows = await conn.execute_fetchall(query, params)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


@app.get("/api/reports/{rid}")
async def get_report(rid: str):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT r.*, m.title AS mission_title, p.name AS project_name
               FROM reports r
               JOIN missions m ON m.id = r.mission_id
               JOIN projects p ON p.id = m.project_id
               WHERE r.id=?""",
            (rid,),
        )
        if not rows:
            raise HTTPException(404, "Report not found")
        return dict(rows[0])
    finally:
        await conn.close()


# ──────────────────────────────────────────────
# Dashboard
# ──────────────────────────────────────────────

@app.get("/api/dashboard/stats")
async def dashboard_stats():
    conn = await db.get_db()
    try:
        projects = await conn.execute_fetchall("SELECT COUNT(*) AS c FROM projects")
        missions_by_status = await conn.execute_fetchall(
            "SELECT status, COUNT(*) AS c FROM missions GROUP BY status"
        )
        running_agents = sum(1 for t in running_tasks.values() if not t.done())
        recent_reports = await conn.execute_fetchall(
            """SELECT r.id, r.created_at, r.what_done, r.what_open,
                      m.title AS mission_title, p.name AS project_name
               FROM reports r
               JOIN missions m ON m.id = r.mission_id
               JOIN projects p ON p.id = m.project_id
               ORDER BY r.created_at DESC LIMIT 10"""
        )
        recent_sessions = await conn.execute_fetchall(
            """SELECT s.id, s.status, s.started_at, s.ended_at,
                      m.title AS mission_title, p.name AS project_name
               FROM agent_sessions s
               JOIN missions m ON m.id = s.mission_id
               JOIN projects p ON p.id = m.project_id
               ORDER BY s.started_at DESC LIMIT 10"""
        )
        return {
            "total_projects": dict(projects[0])["c"],
            "missions_by_status": {dict(r)["status"]: dict(r)["c"] for r in missions_by_status},
            "running_agents": running_agents,
            "max_agents": MAX_CONCURRENT_AGENTS,
            "recent_reports": [dict(r) for r in recent_reports],
            "recent_sessions": [dict(r) for r in recent_sessions],
        }
    finally:
        await conn.close()


# ──────────────────────────────────────────────
# Plugins — List loaded plugins and their tools
# ──────────────────────────────────────────────

@app.get("/api/plugins")
async def api_list_plugins():
    from plugins import registry
    return {
        "loaded": registry.loaded_plugins,
        "custom_tools": [{"name": t.name, "description": t.description} for t in registry.tools],
        "hooks": {k: len(v) for k, v in registry._hooks.items() if v},
    }


# ──────────────────────────────────────────────
# Auto-Loop
# ──────────────────────────────────────────────

class AutoLoopRequest(BaseModel):
    project_id: str
    goal: str


@app.post("/api/autoloop/start")
async def api_start_autoloop(body: AutoLoopRequest):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT id FROM projects WHERE id=?", (body.project_id,))
        if not rows:
            raise HTTPException(404, "Project not found")
    finally:
        await conn.close()
    started = start_auto_loop(body.project_id, body.goal)
    if not started:
        raise HTTPException(409, "Auto-loop already running for this project")
    return {"ok": True, "message": "Auto-loop started"}


@app.post("/api/autoloop/stop/{project_id}")
async def api_stop_autoloop(project_id: str):
    stopped = stop_auto_loop(project_id)
    if not stopped:
        raise HTTPException(404, "No active auto-loop for this project")
    return {"ok": True}


@app.get("/api/autoloop/status/{project_id}")
async def api_autoloop_status(project_id: str):
    return get_auto_loop_status(project_id)


# ──────────────────────────────────────────────
# Project Planner — One-prompt project creation
# ──────────────────────────────────────────────

from planner import plan_project

class PlanRequest(BaseModel):
    prompt: str
    project_path: str | None = None  # auto-generate if not provided

@app.post("/api/plan", status_code=201)
async def api_plan_project(body: PlanRequest):
    """Take a natural language prompt, plan a project with chained missions."""
    # Auto-generate project path if not provided
    project_path = body.project_path
    if not project_path:
        # Derive from prompt — take first few words, slugify
        # Store in projects/ dir inside DevFleet install (works for any user)
        import re
        slug = re.sub(r'[^a-z0-9]+', '-', body.prompt.lower().strip())[:40].strip('-')
        # Use DEVFLEET_PROJECTS_DIR if set (useful in Docker), otherwise derive from install root
        projects_base = os.environ.get("DEVFLEET_PROJECTS_DIR")
        if not projects_base:
            devfleet_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            projects_base = os.path.join(devfleet_root, "projects")
        project_path = os.path.join(projects_base, slug)

    # Resolve for local filesystem (Docker container path)
    resolved_path = resolve_path(project_path)

    try:
        result = await plan_project(body.prompt, resolved_path)
        # Store and return the host-facing path (for UI display)
        host_path = reverse_path(resolved_path)
        result["project"]["path"] = host_path
        # Also update the DB record to store the host path
        conn = await db.get_db()
        try:
            await conn.execute("UPDATE projects SET path = ? WHERE id = ?",
                               (host_path, result["project"]["id"]))
            await conn.commit()
        finally:
            await conn.close()
        return result
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        log.exception("Plan failed")
        raise HTTPException(500, f"Planning failed: {e}")


# ──────────────────────────────────────────────
# Project Intelligence — Vision, Analysis, Health, Visualization, Optimization
# ──────────────────────────────────────────────

from planner_v2 import plan_project_intelligent
from project_analyzer import analyze_project_files
from health_metrics import get_project_health
from visualizer import generate_mission_graph, generate_project_summary_diagram
from cost_optimizer import analyze_costs_and_optimize


class PlanIntelligentRequest(BaseModel):
    prompt: str
    project_path: str | None = None


@app.post("/api/plan-intelligent", status_code=201)
async def api_plan_intelligent(body: PlanIntelligentRequest):
    """Enhanced project planner using extended thinking for better mission breaking."""
    project_path = body.project_path
    if not project_path:
        import re
        slug = re.sub(r'[^a-z0-9]+', '-', body.prompt.lower().strip())[:40].strip('-')
        projects_base = os.environ.get("DEVFLEET_PROJECTS_DIR")
        if not projects_base:
            devfleet_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            projects_base = os.path.join(devfleet_root, "projects")
        project_path = os.path.join(projects_base, slug)

    resolved_path = resolve_path(project_path)

    try:
        result = await plan_project_intelligent(body.prompt, resolved_path)
        host_path = reverse_path(resolved_path)
        result["project"]["path"] = host_path
        conn = await db.get_db()
        try:
            await conn.execute("UPDATE projects SET path = ? WHERE id = ?",
                               (host_path, result["project"]["id"]))
            await conn.commit()
        finally:
            await conn.close()
        return result
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        log.exception("Intelligent planning failed")
        raise HTTPException(500, f"Planning failed: {e}")


class AnalyzeProjectRequest(BaseModel):
    files: list[str] | None = None
    custom_prompt: str = ""


@app.post("/api/projects/{pid}/analyze")
async def api_analyze_project(pid: str, body: AnalyzeProjectRequest):
    """Analyze a project structure and suggest missions using vision + reasoning."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM projects WHERE id=?", (pid,))
        if not rows:
            raise HTTPException(404, "Project not found")
        project = dict(rows[0])
    finally:
        await conn.close()

    try:
        analysis = await analyze_project_files(
            resolve_path(project["path"]),
            files_to_analyze=body.files,
            custom_prompt=body.custom_prompt
        )
        return {
            "project_id": pid,
            "analysis": analysis,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        log.exception("Project analysis failed")
        raise HTTPException(500, f"Analysis failed: {e}")


@app.get("/api/projects/{pid}/health")
async def api_project_health(pid: str):
    """Get comprehensive project health metrics and recommendations."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM projects WHERE id=?", (pid,))
        if not rows:
            raise HTTPException(404, "Project not found")
    finally:
        await conn.close()

    try:
        health = await get_project_health(pid)
        return health
    except Exception as e:
        log.exception("Health check failed")
        raise HTTPException(500, f"Health check failed: {e}")


@app.get("/api/projects/{pid}/missions/graph")
async def api_mission_graph(
    pid: str,
    diagram_type: str = Query("dag", regex="^(dag|timeline|critical_path)$")
):
    """Get Mermaid diagram of mission dependencies and execution flow."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM projects WHERE id=?", (pid,))
        if not rows:
            raise HTTPException(404, "Project not found")
    finally:
        await conn.close()

    try:
        graph = await generate_mission_graph(pid, diagram_type)
        return {
            "mermaid_diagram": graph,
            "diagram_type": diagram_type,
            "project_id": pid
        }
    except Exception as e:
        log.exception("Mission graph generation failed")
        raise HTTPException(500, f"Graph generation failed: {e}")


@app.get("/api/projects/{pid}/missions/summary-diagram")
async def api_project_summary(pid: str):
    """Get high-level project summary diagram."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM projects WHERE id=?", (pid,))
        if not rows:
            raise HTTPException(404, "Project not found")
    finally:
        await conn.close()

    try:
        diagram = await generate_project_summary_diagram(pid)
        return {
            "mermaid_diagram": diagram,
            "project_id": pid
        }
    except Exception as e:
        log.exception("Summary diagram generation failed")
        raise HTTPException(500, f"Diagram generation failed: {e}")


@app.get("/api/projects/{pid}/costs")
async def api_cost_analysis(pid: str):
    """Analyze project costs and suggest optimizations using batch analysis."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM projects WHERE id=?", (pid,))
        if not rows:
            raise HTTPException(404, "Project not found")
    finally:
        await conn.close()

    try:
        analysis = await analyze_costs_and_optimize(pid)
        return analysis
    except Exception as e:
        log.exception("Cost analysis failed")
        raise HTTPException(500, f"Cost analysis failed: {e}")


# ──────────────────────────────────────────────
# Remote Control — Take over sessions from phone/browser
# ──────────────────────────────────────────────

@app.post("/api/sessions/{sid}/remote-control")
async def start_remote(sid: str):
    """Start a remote-control session for a running or completed agent session.
    Returns a claude.ai URL that can be opened on phone/browser."""
    if not ENABLE_REMOTE_CONTROL:
        raise HTTPException(501, "Remote control is not enabled")
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT s.*, m.id AS mid, m.title, m.detailed_prompt,
                      m.acceptance_criteria, m.tags, m.mission_type,
                      m.project_id, p.path AS project_path
               FROM agent_sessions s
               JOIN missions m ON m.id = s.mission_id
               JOIN projects p ON p.id = m.project_id
               WHERE s.id=?""",
            (sid,),
        )
        if not rows:
            raise HTTPException(404, "Session not found")
        row = dict(rows[0])
    finally:
        await conn.close()

    project_path = resolve_path(row["project_path"])
    mission = {
        "title": row["title"],
        "detailed_prompt": row.get("detailed_prompt", ""),
        "acceptance_criteria": row.get("acceptance_criteria"),
        "tags": row.get("tags"),
        "mission_type": row.get("mission_type"),
    }
    try:
        url = await start_remote_control(
            session_id=sid,
            mission_id=row["mid"],
            work_dir=project_path,
            mission=mission,
        )
    except (RemoteControlNotEnabled, WorkspaceNotTrusted) as e:
        raise HTTPException(403, str(e))
    if not url:
        raise HTTPException(500, "Failed to start remote-control — check logs")

    return {"url": url, "session_id": sid}


@app.post("/api/sessions/{sid}/takeover")
async def takeover(sid: str):
    """Take over a running agent: cancel it, preserve its worktree, and start
    remote-control in the same directory with full context of what the agent did."""
    if not USE_SDK_ENGINE:
        raise HTTPException(400, "Takeover only supported with SDK engine")

    # 1. Take over the running session (cancels agent, preserves worktree)
    result = await takeover_session(sid)
    if not result:
        raise HTTPException(404, "Session not running or not found")

    # 2. Get mission details
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT m.*, p.path AS project_path
               FROM agent_sessions s
               JOIN missions m ON m.id = s.mission_id
               JOIN projects p ON p.id = m.project_id
               WHERE s.id=?""",
            (sid,),
        )
        if not rows:
            raise HTTPException(404, "Session not found")
        mission = dict(rows[0])

        # 3. Create a new session for the remote-control
        rc_session_id = str(uuid.uuid4())
        await conn.execute(
            "INSERT INTO agent_sessions (id, mission_id, status) VALUES (?, ?, 'remote')",
            (rc_session_id, mission["id"]),
        )
        await conn.commit()
    finally:
        await conn.close()

    # 4. Build agent progress summary from output log
    output_log = result.get("output_log", "")
    # Summarize: last ~2000 chars of output + cost info
    progress_parts = []
    if result.get("total_cost_usd"):
        progress_parts.append(f"Agent spent ${result['total_cost_usd']:.4f} and used {result.get('total_tokens', 0)} tokens before takeover.")
    if output_log:
        # Get the last meaningful chunk of output
        trimmed = output_log[-2000:] if len(output_log) > 2000 else output_log
        progress_parts.append(f"### Last agent output\n```\n{trimmed}\n```")

    # 5. List files changed in the worktree
    work_dir = result["work_dir"]
    try:
        import subprocess
        diff = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],
            cwd=work_dir, capture_output=True, text=True, timeout=5,
        )
        if diff.returncode == 0 and diff.stdout.strip():
            progress_parts.append(f"### Files changed by agent\n```\n{diff.stdout.strip()}\n```")
        # Also check untracked files
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=work_dir, capture_output=True, text=True, timeout=5,
        )
        if status.returncode == 0 and status.stdout.strip():
            progress_parts.append(f"### New/modified files\n```\n{status.stdout.strip()}\n```")
    except Exception as e:
        log.warning("Failed to get git diff for takeover: %s", e)

    agent_progress = "\n\n".join(progress_parts)

    # 6. Start remote-control in the agent's worktree
    try:
        url = await start_remote_control(
            session_id=rc_session_id,
            mission_id=mission["id"],
            work_dir=work_dir,
            mission=mission,
            agent_progress=agent_progress,
        )
    except (RemoteControlNotEnabled, WorkspaceNotTrusted) as e:
        raise HTTPException(403, str(e))
    if not url:
        raise HTTPException(500, "Failed to start remote-control — check logs")

    return {
        "url": url,
        "session_id": rc_session_id,
        "work_dir": work_dir,
        "agent_progress": agent_progress[:500],  # Preview for frontend
    }


@app.post("/api/missions/{mid}/remote-control")
async def start_remote_for_mission(mid: str):
    """Start a fresh remote-control session for a mission (interactive mode).
    Creates a new session and launches remote-control in the project dir."""
    if not ENABLE_REMOTE_CONTROL:
        raise HTTPException(501, "Remote control is not enabled")
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT m.*, p.path AS project_path FROM missions m JOIN projects p ON p.id=m.project_id WHERE m.id=?",
            (mid,),
        )
        if not rows:
            raise HTTPException(404, "Mission not found")
        mission = dict(rows[0])

        session_id = str(uuid.uuid4())
        await conn.execute(
            "INSERT INTO agent_sessions (id, mission_id, status) VALUES (?, ?, 'remote')",
            (session_id, mid),
        )
        await conn.execute(
            "UPDATE missions SET status='running', updated_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), mid),
        )
        await conn.commit()
    finally:
        await conn.close()

    project_path = resolve_path(mission["project_path"])

    try:
        url = await start_remote_control(
            session_id=session_id,
            mission_id=mid,
            work_dir=project_path,
            mission=mission,
        )
    except (RemoteControlNotEnabled, WorkspaceNotTrusted) as e:
        # Cleanup the session and reset mission status
        conn = await db.get_db()
        try:
            await conn.execute("DELETE FROM agent_sessions WHERE id=?", (session_id,))
            await conn.execute(
                "UPDATE missions SET status='draft', updated_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), mid),
            )
            await conn.commit()
        finally:
            await conn.close()
        raise HTTPException(403, str(e))
    if not url:
        # Cleanup the session on failure
        conn = await db.get_db()
        try:
            await conn.execute("DELETE FROM agent_sessions WHERE id=?", (session_id,))
            await conn.execute(
                "UPDATE missions SET status='draft', updated_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), mid),
            )
            await conn.commit()
        finally:
            await conn.close()
        raise HTTPException(500, "Failed to start remote-control — check logs")

    return {"url": url, "session_id": session_id}


@app.delete("/api/sessions/{sid}/remote-control")
async def stop_remote(sid: str):
    """Stop a remote-control session and reset mission status."""
    if not ENABLE_REMOTE_CONTROL:
        raise HTTPException(501, "Remote control is not enabled")
    # Try to stop the in-memory process (may not exist after server hot-reload)
    await stop_remote_control(sid)

    # Always clean up DB state regardless of in-memory state
    now = datetime.now(timezone.utc).isoformat()
    conn = await db.get_db()
    try:
        row = await conn.execute_fetchall(
            "SELECT mission_id, status FROM agent_sessions WHERE id=?", (sid,)
        )
        if not row:
            raise HTTPException(404, "Session not found")

        await conn.execute(
            "UPDATE agent_sessions SET status='completed', ended_at=? WHERE id=?",
            (now, sid),
        )
        mid = row[0]["mission_id"]
        # Also clean up any other orphaned remote sessions for this mission
        await conn.execute(
            "UPDATE agent_sessions SET status='completed', ended_at=? "
            "WHERE mission_id=? AND status='remote' AND ended_at IS NULL",
            (now, mid),
        )
        # Only reset mission if it's still in 'running' state
        mission_row = await conn.execute_fetchall(
            "SELECT status FROM missions WHERE id=?", (mid,)
        )
        if mission_row and mission_row[0]["status"] == "running":
            await conn.execute(
                "UPDATE missions SET status='draft', updated_at=? WHERE id=?",
                (now, mid),
            )
        await conn.commit()
    finally:
        await conn.close()

    return {"ok": True}


@app.get("/api/sessions/{sid}/remote-control")
async def remote_status(sid: str):
    """Get remote-control status for a session."""
    if not ENABLE_REMOTE_CONTROL:
        raise HTTPException(501, "Remote control is not enabled")
    return get_remote_status(sid)


@app.get("/api/remote-control/sessions")
async def list_remote():
    """List all active remote-control sessions."""
    if not ENABLE_REMOTE_CONTROL:
        raise HTTPException(501, "Remote control is not enabled")
    return list_remote_sessions()


@app.get("/api/sessions/{sid}/remote-stream")
async def stream_remote(sid: str):
    """SSE stream of live remote-control process output."""
    if not ENABLE_REMOTE_CONTROL:
        raise HTTPException(501, "Remote control is not enabled")
    async def event_stream():
        async for event in subscribe_remote_session(sid):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ──────────────────────────────────────────────
# Monitored Services
# ──────────────────────────────────────────────

@app.get("/api/services")
async def list_services(project_id: str = Query(None)):
    conn = await db.get_db()
    try:
        if project_id:
            rows = await conn.execute_fetchall(
                "SELECT * FROM monitored_services WHERE project_id=? ORDER BY group_name, name",
                (project_id,),
            )
        else:
            rows = await conn.execute_fetchall(
                "SELECT * FROM monitored_services ORDER BY group_name, name"
            )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


@app.post("/api/services", status_code=201)
async def create_service(body: ServiceCreate):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT id FROM projects WHERE id=?", (body.project_id,))
        if not rows:
            raise HTTPException(400, "Project not found")
        sid = str(uuid.uuid4())
        await conn.execute(
            """INSERT INTO monitored_services (id, project_id, name, url, group_name, description,
               check_interval, timeout_ms, expected_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sid, body.project_id, body.name, body.url, body.group_name, body.description,
             body.check_interval, body.timeout_ms, body.expected_status),
        )
        await conn.commit()
        row = await conn.execute_fetchall("SELECT * FROM monitored_services WHERE id=?", (sid,))
        return dict(row[0])
    finally:
        await conn.close()


@app.get("/api/services/{sid}")
async def get_service_detail(sid: str):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM monitored_services WHERE id=?", (sid,))
        if not rows:
            raise HTTPException(404, "Service not found")
        service = dict(rows[0])
        status_data = await health_checker.get_service_status(sid)
        service.update(status_data)
        return service
    finally:
        await conn.close()


@app.put("/api/services/{sid}")
async def update_service(sid: str, body: ServiceUpdate):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM monitored_services WHERE id=?", (sid,))
        if not rows:
            raise HTTPException(404, "Service not found")
        updates = body.model_dump(exclude_none=True)
        if not updates:
            return dict(rows[0])
        if "enabled" in updates:
            updates["enabled"] = 1 if updates["enabled"] else 0
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        sets = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [sid]
        await conn.execute(f"UPDATE monitored_services SET {sets} WHERE id=?", vals)
        await conn.commit()
        row = await conn.execute_fetchall("SELECT * FROM monitored_services WHERE id=?", (sid,))
        return dict(row[0])
    finally:
        await conn.close()


@app.delete("/api/services/{sid}")
async def delete_service(sid: str):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM monitored_services WHERE id=?", (sid,))
        if not rows:
            raise HTTPException(404, "Service not found")
        await conn.execute("DELETE FROM monitored_services WHERE id=?", (sid,))
        await conn.commit()
        return {"ok": True}
    finally:
        await conn.close()


@app.get("/api/services/{sid}/checks")
async def get_service_checks(sid: str, hours: int = Query(24)):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT status, response_time_ms, status_code, error_message, checked_at
               FROM health_checks
               WHERE service_id=? AND checked_at >= datetime('now', ? || ' hours')
               ORDER BY checked_at DESC""",
            (sid, f"-{hours}"),
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


# ──────────────────────────────────────────────
# Status Page
# ──────────────────────────────────────────────

@app.get("/api/status")
async def get_status_page(project_id: str = Query(None)):
    conn = await db.get_db()
    try:
        if project_id:
            services = await conn.execute_fetchall(
                "SELECT * FROM monitored_services WHERE project_id=? ORDER BY group_name, name",
                (project_id,),
            )
        else:
            services = await conn.execute_fetchall(
                "SELECT * FROM monitored_services ORDER BY group_name, name"
            )

        groups = {}
        total = 0
        up_count = 0
        degraded_count = 0
        down_count = 0

        for row in services:
            svc = dict(row)
            status_data = await health_checker.get_service_status(svc["id"])
            uptime_bars = await health_checker.get_uptime_bars(svc["id"])
            svc.update(status_data)
            svc["uptime_bars"] = uptime_bars

            group = svc.get("group_name") or "Default"
            if group not in groups:
                groups[group] = []
            groups[group].append(svc)

            total += 1
            s = status_data.get("status", "unknown")
            if s == "up":
                up_count += 1
            elif s == "degraded":
                degraded_count += 1
            else:
                down_count += 1

        if total == 0:
            overall = "no_services"
        elif down_count > 0:
            overall = "major_outage"
        elif degraded_count > 0:
            overall = "degraded"
        else:
            overall = "all_operational"

        # Get incidents
        incident_query = "SELECT * FROM incidents ORDER BY created_at DESC LIMIT 20"
        incident_params = []
        if project_id:
            incident_query = "SELECT * FROM incidents WHERE project_id=? ORDER BY created_at DESC LIMIT 20"
            incident_params = [project_id]
        incidents = await conn.execute_fetchall(incident_query, incident_params)

        active_incidents = [dict(i) for i in incidents if dict(i)["status"] != "resolved"]
        recent_incidents = [dict(i) for i in incidents]

        return {
            "overall_status": overall,
            "total_services": total,
            "operational": up_count,
            "degraded": degraded_count,
            "down": down_count,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "groups": [{"name": name, "services": svcs} for name, svcs in groups.items()],
            "active_incidents": active_incidents,
            "recent_incidents": recent_incidents,
        }
    finally:
        await conn.close()


@app.get("/api/status/summary")
async def get_status_summary():
    conn = await db.get_db()
    try:
        services = await conn.execute_fetchall("SELECT id FROM monitored_services WHERE enabled=1")
        total = len(services)
        up_count = 0
        degraded_count = 0
        down_count = 0

        for row in services:
            status_data = await health_checker.get_service_status(dict(row)["id"])
            s = status_data.get("status", "unknown")
            if s == "up":
                up_count += 1
            elif s == "degraded":
                degraded_count += 1
            else:
                down_count += 1

        if total == 0:
            overall = "no_services"
        elif down_count > 0:
            overall = "major_outage"
        elif degraded_count > 0:
            overall = "degraded"
        else:
            overall = "all_operational"

        return {
            "overall_status": overall,
            "total_services": total,
            "operational": up_count,
            "degraded": degraded_count,
            "down": down_count,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        await conn.close()


# ──────────────────────────────────────────────
# Incidents
# ──────────────────────────────────────────────

@app.get("/api/incidents")
async def list_incidents(project_id: str = Query(None), status: str = Query(None)):
    conn = await db.get_db()
    try:
        query = "SELECT * FROM incidents WHERE 1=1"
        params = []
        if project_id:
            query += " AND project_id=?"
            params.append(project_id)
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        rows = await conn.execute_fetchall(query, params)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


@app.post("/api/incidents", status_code=201)
async def create_incident(body: IncidentCreate):
    conn = await db.get_db()
    try:
        iid = str(uuid.uuid4())
        await conn.execute(
            """INSERT INTO incidents (id, service_id, project_id, title, description, severity)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (iid, body.service_id, body.project_id, body.title, body.description, body.severity),
        )
        await conn.commit()
        row = await conn.execute_fetchall("SELECT * FROM incidents WHERE id=?", (iid,))
        return dict(row[0])
    finally:
        await conn.close()


@app.put("/api/incidents/{iid}")
async def update_incident(iid: str, body: IncidentUpdate):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM incidents WHERE id=?", (iid,))
        if not rows:
            raise HTTPException(404, "Incident not found")
        updates = body.model_dump(exclude_none=True)
        if not updates:
            return dict(rows[0])
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        if updates.get("status") == "resolved" and "resolved_at" not in updates:
            updates["resolved_at"] = updates["updated_at"]
        sets = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [iid]
        await conn.execute(f"UPDATE incidents SET {sets} WHERE id=?", vals)
        await conn.commit()
        row = await conn.execute_fetchall("SELECT * FROM incidents WHERE id=?", (iid,))
        return dict(row[0])
    finally:
        await conn.close()


@app.delete("/api/incidents/{iid}")
async def delete_incident(iid: str):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM incidents WHERE id=?", (iid,))
        if not rows:
            raise HTTPException(404, "Incident not found")
        await conn.execute("DELETE FROM incidents WHERE id=?", (iid,))
        await conn.commit()
        return {"ok": True}
    finally:
        await conn.close()


# ──────────────────────────────────────────────
# Config / Meta — expose available options to frontend
# ──────────────────────────────────────────────

@app.get("/api/config/models")
async def get_models():
    """List available Claude models."""
    return MODEL_CHOICES


@app.get("/api/config/tool-presets")
async def get_tool_presets():
    """List available tool presets for mission types."""
    return TOOL_PRESETS


@app.get("/api/config/mission-types")
async def get_mission_types():
    """List available mission types."""
    return list(TOOL_PRESETS.keys())


@app.get("/api/config/engine")
async def get_engine_config():
    """Show current dispatch engine configuration."""
    return {
        "engine": "sdk" if USE_SDK_ENGINE else "cli",
        "sdk_available": USE_SDK_ENGINE,
    }


# ──────────────────────────────────────────────
# MCP Server Configuration (per-project)
# ──────────────────────────────────────────────

@app.get("/api/projects/{pid}/mcp-servers")
async def list_mcp_servers(pid: str):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT * FROM mcp_configs WHERE project_id=? ORDER BY server_name",
            (pid,),
        )
        results = []
        for r in rows:
            d = dict(r)
            d["config"] = json.loads(d.pop("config_json", "{}"))
            results.append(d)
        return results
    finally:
        await conn.close()


@app.post("/api/projects/{pid}/mcp-servers", status_code=201)
async def add_mcp_server(pid: str, body: McpServerCreate):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT id FROM projects WHERE id=?", (pid,))
        if not rows:
            raise HTTPException(404, "Project not found")
        mid = str(uuid.uuid4())
        await conn.execute(
            """INSERT INTO mcp_configs (id, project_id, server_name, server_type, config_json)
               VALUES (?, ?, ?, ?, ?)""",
            (mid, pid, body.server_name, body.server_type, json.dumps(body.config)),
        )
        await conn.commit()
        row = await conn.execute_fetchall("SELECT * FROM mcp_configs WHERE id=?", (mid,))
        d = dict(row[0])
        d["config"] = json.loads(d.pop("config_json", "{}"))
        return d
    finally:
        await conn.close()


@app.delete("/api/mcp-servers/{mid}")
async def delete_mcp_server(mid: str):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM mcp_configs WHERE id=?", (mid,))
        if not rows:
            raise HTTPException(404, "MCP server config not found")
        await conn.execute("DELETE FROM mcp_configs WHERE id=?", (mid,))
        await conn.commit()
        return {"ok": True}
    finally:
        await conn.close()


# ──────────────────────────────────────────────
# Scheduling (Phase 3)
# ──────────────────────────────────────────────

class ScheduleRequest(BaseModel):
    cron: str
    enabled: bool = True


@app.post("/api/missions/{mid}/schedule")
async def set_schedule(mid: str, body: ScheduleRequest):
    """Set a cron schedule on a mission (turns it into a recurring template)."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT id FROM missions WHERE id=?", (mid,))
        if not rows:
            raise HTTPException(404, "Mission not found")
        await conn.execute(
            "UPDATE missions SET schedule_cron=?, schedule_enabled=?, updated_at=? WHERE id=?",
            (body.cron, 1 if body.enabled else 0, datetime.now(timezone.utc).isoformat(), mid),
        )
        await conn.commit()
        return {"ok": True, "mission_id": mid, "cron": body.cron, "enabled": body.enabled}
    finally:
        await conn.close()


@app.delete("/api/missions/{mid}/schedule")
async def remove_schedule(mid: str):
    """Disable scheduling on a mission."""
    conn = await db.get_db()
    try:
        await conn.execute(
            "UPDATE missions SET schedule_enabled=0, updated_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), mid),
        )
        await conn.commit()
        return {"ok": True}
    finally:
        await conn.close()


@app.get("/api/schedules")
async def list_schedules():
    """List all missions with active schedules."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT m.id, m.title, m.schedule_cron, m.schedule_enabled,
                      m.last_scheduled_at, m.mission_type, m.project_id,
                      p.name AS project_name
               FROM missions m
               JOIN projects p ON p.id = m.project_id
               WHERE m.schedule_cron IS NOT NULL AND m.schedule_cron != ''
               ORDER BY m.schedule_enabled DESC, m.title"""
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


# ──────────────────────────────────────────────
# Mission Events & Watcher Status (Phase 3)
# ──────────────────────────────────────────────

@app.get("/api/missions/{mid}/events")
async def list_mission_events(mid: str, limit: int = Query(20)):
    """Get the event log for a mission."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT * FROM mission_events WHERE mission_id=? ORDER BY created_at DESC LIMIT ?",
            (mid, limit),
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


@app.get("/api/system/status")
async def system_status():
    """Get system-wide status: watcher, scheduler, running agents."""
    running_count = sum(1 for t in running_tasks.values() if not t.done())
    return {
        "running_agents": running_count,
        "max_agents": MAX_CONCURRENT_AGENTS,
        "engine": "sdk" if USE_SDK_ENGINE else "cli",
        "mission_watcher": mission_watcher.get_watcher_status(),
        "scheduler": scheduler.get_scheduler_status(),
    }


@app.get("/api/system/features")
async def system_features():
    """Get enabled features."""
    return {
        "remote_control": ENABLE_REMOTE_CONTROL,
    }
