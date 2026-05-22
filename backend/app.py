import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel

import db
from models import (ProjectCreate, ProjectUpdate, MissionCreate, MissionUpdate,
                    DispatchOptions, TOOL_PRESETS, MODEL_CHOICES,
                    ServiceCreate, ServiceUpdate, IncidentCreate, IncidentUpdate,
                    McpServerCreate, CeilingUpdate)
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
    # Sweep for sessions left running by a previous SIGKILL — mark interrupted so UI is accurate
    # Sessions with claude_session_id can be resumed via POST /api/missions/{id}/resume
    conn = await db.get_db()
    try:
        # Collect orphaned mission IDs BEFORE updating sessions (subquery must run first)
        orphaned = await conn.execute_fetchall(
            "SELECT DISTINCT mission_id FROM agent_sessions WHERE status='running'"
        )
        orphan_mission_ids = [r["mission_id"] for r in orphaned]
        if orphan_mission_ids:
            await conn.execute(
                "UPDATE agent_sessions SET status='interrupted', ended_at=datetime('now'), "
                "error_log='Process interrupted (restart/SIGKILL) — resume via POST /api/missions/{id}/resume' "
                "WHERE status='running'"
            )
            placeholders = ",".join("?" * len(orphan_mission_ids))
            await conn.execute(
                f"UPDATE missions SET status='interrupted', updated_at=datetime('now') "
                f"WHERE status='running' AND id IN ({placeholders})",
                orphan_mission_ids,
            )
            await conn.commit()
            log.warning("Startup sweep: marked %d orphaned session(s) as interrupted (resumable)", len(orphan_mission_ids))
    finally:
        await conn.close()
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

_ALLOWED_ORIGINS = [
    o.strip() for o in
    os.environ.get("DEVFLEET_ALLOWED_ORIGINS",
                   "http://localhost:3100,http://localhost:3101").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as _StarletteRequest

# MCP API key — required for external /mcp and /messages access
_MCP_API_KEY = os.environ.get("DEVFLEET_MCP_KEY", "")

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

app.add_middleware(SecurityHeadersMiddleware)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: _StarletteRequest, call_next):
        request.state.user = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            try:
                from auth import decode_token
                request.state.user = decode_token(token)
            except Exception:
                pass  # Invalid/expired token → user stays None; endpoint decides if it cares
        return await call_next(request)


app.add_middleware(AuthMiddleware)

# ── Rate limiting (C3 fix) ───────────────────────────────────────────────────
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

_limiter = Limiter(key_func=get_remote_address)
app.state.limiter = _limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

MAX_CONCURRENT_AGENTS = int(os.environ.get("DEVFLEET_MAX_AGENTS", "0"))  # 0 = defer to lane system


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
# Auth Routes
# ──────────────────────────────────────────────
from models import UserCreate, UserLogin

@app.post("/api/auth/login")
@_limiter.limit("5/minute")
async def auth_login(request: Request, body: UserLogin):
    from auth import get_user_by_email, verify_password, create_access_token
    user = await get_user_by_email(body.email)
    # Always run bcrypt verify (against a dummy hash when user is None) so login
    # timing does not leak whether the email is registered.
    password_ok = verify_password(body.password, user["password_hash"] if user else None)
    if not user or not password_ok:
        raise HTTPException(401, "Invalid email or password")
    conn = await db.get_db()
    try:
        await conn.execute("UPDATE users SET last_login_at=datetime('now') WHERE id=?", (user["id"],))
        await conn.commit()
    finally:
        await conn.close()
    token = create_access_token(user["id"], user["email"], user["role"])
    return {"access_token": token, "token_type": "bearer",
            "user": {"id": user["id"], "email": user["email"], "role": user["role"]}}


@app.post("/api/auth/register")
@_limiter.limit("5/minute")
async def auth_register(request: Request, body: UserCreate):
    from auth import get_user_by_email, create_user, consume_invite_token, create_access_token
    # Generic 400 for any registration failure (existing email, invalid token,
    # bad password) so we don't leak which inputs are valid.
    if await get_user_by_email(body.email):
        log.info("Registration rejected: email already in use (%s)", body.email)
        raise HTTPException(400, "Registration failed — check your invite token and try again")
    try:
        user = await create_user(body.email, body.password)
    except ValueError:
        log.exception("Registration rejected: invalid password")
        raise HTTPException(400, "Registration failed — check your invite token and try again")
    valid = await consume_invite_token(body.invite_token, user["id"])
    if not valid:
        conn = await db.get_db()
        try:
            await conn.execute("DELETE FROM users WHERE id=?", (user["id"],))
            await conn.commit()
        finally:
            await conn.close()
        raise HTTPException(400, "Registration failed — check your invite token and try again")
    token = create_access_token(user["id"], user["email"], user["role"])
    return {"access_token": token, "token_type": "bearer",
            "user": {"id": user["id"], "email": user["email"], "role": user["role"]}}


@app.get("/api/auth/me")
async def auth_me(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return {"id": user["sub"], "email": user["email"], "role": user["role"]}


@app.post("/api/auth/invite")
async def auth_invite(request: Request):
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        raise HTTPException(403, "Admin access required")
    from auth import create_invite_token
    token = await create_invite_token(user["sub"])
    return {"invite_token": token, "expires_in": "7 days"}


@app.get("/api/auth/users")
async def auth_list_users(request: Request):
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        raise HTTPException(403, "Admin access required")
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT id, email, role, created_at, last_login_at FROM users ORDER BY created_at"
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


# ──────────────────────────────────────────────
# Fleet Events SSE
# ──────────────────────────────────────────────
_fleet_subscribers: list[asyncio.Queue] = []


async def broadcast_fleet_event(event: dict):
    dead = []
    for q in _fleet_subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        if q in _fleet_subscribers:
            _fleet_subscribers.remove(q)


@app.get("/api/events")
async def fleet_events_stream(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "Authentication required")
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _fleet_subscribers.append(q)

    async def event_stream():
        try:
            yield f"data: {json.dumps({'type': 'connected', 'user': user['email']})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'ping'})}\n\n"
        finally:
            if q in _fleet_subscribers:
                _fleet_subscribers.remove(q)

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
            "INSERT INTO projects (id, name, path, description) VALUES (?, ?, ?, ?)",
            (pid, body.name, body.path, body.description),
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


@app.delete("/api/projects/{pid}", status_code=204)
async def delete_project(pid: str):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM projects WHERE id=?", (pid,))
        if not rows:
            raise HTTPException(404, "Project not found")
        await conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        await conn.commit()
    finally:
        await conn.close()


# ──────────────────────────────────────────────
# Missions
# ──────────────────────────────────────────────

@app.get("/api/missions")
async def list_missions(
    response: Response,
    project_id: str = Query(None),
    status: str = Query(None),
    tag: str = Query(None),
    parent_mission_id: str = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
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
        # Tag filter is applied in Python because tags are stored as JSON text;
        # LIMIT/OFFSET therefore slice the filtered list rather than the raw SQL result.
        results = []
        for r in rows:
            d = dict(r)
            if tag:
                tags = json.loads(d.get("tags", "[]"))
                if tag not in tags:
                    continue
            results.append(d)
        response.headers["X-Total-Count"] = str(len(results))
        return results[offset:offset + limit]
    finally:
        await conn.close()


@app.post("/api/missions", status_code=201)
async def create_mission(request: Request, body: MissionCreate):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT id FROM projects WHERE id=?", (body.project_id,))
        if not rows:
            raise HTTPException(404, "Project not found")
        mid = str(uuid.uuid4())
        schedule_enabled = 1 if body.schedule_cron else 0
        # Get next mission number for this project
        num_rows = await conn.execute_fetchall(
            "SELECT COALESCE(MAX(mission_number), 0) + 1 AS next_num FROM missions WHERE project_id=?",
            (body.project_id,),
        )
        next_num = num_rows[0][0] if num_rows else 1
        # Derive lane from mission_type if not explicitly provided
        from models import MISSION_TYPE_TO_LANE
        lane = body.lane or MISSION_TYPE_TO_LANE.get(body.mission_type, "coder")
        # Capture authenticated user for attribution
        _user = getattr(request.state, "user", None)
        _by_email = _user.get("email", "") if _user else ""
        _by_name = _by_email.split("@")[0] if _by_email else ""
        await conn.execute(
            """INSERT INTO missions (id, project_id, title, detailed_prompt, acceptance_criteria,
               priority, tags, model, max_turns, max_budget_usd, allowed_tools, mission_type,
               lane, parent_mission_id, depends_on, auto_dispatch, schedule_cron, schedule_enabled,
               mission_number, created_by_email, created_by_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (mid, body.project_id, body.title, body.detailed_prompt,
             body.acceptance_criteria, body.priority, json.dumps(body.tags),
             body.model, body.max_turns, body.max_budget_usd,
             body.allowed_tools or "", body.mission_type, lane,
             body.parent_mission_id, json.dumps(body.depends_on),
             1 if body.auto_dispatch else 0, body.schedule_cron, schedule_enabled,
             next_num, _by_email, _by_name),
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


@app.delete("/api/missions/{mid}", status_code=204)
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
# Lanes
# ──────────────────────────────────────────────

@app.get("/api/lanes")
async def get_lanes():
    """Return live lane topology — all lanes with running/free slot counts."""
    from lanes import snapshot as lane_snapshot
    return await lane_snapshot()


@app.get("/api/lanes/studio-summary")
async def lanes_studio_summary():
    """Fleet-wide Prompt Studio stats for the Dashboard card."""
    conn = await db.get_db()
    try:
        total = (await (await conn.execute(
            "SELECT COUNT(*) FROM lanes WHERE enabled=1"
        )).fetchone())[0]
        customized = (await (await conn.execute(
            "SELECT COUNT(*) FROM lanes WHERE json_valid(append_prompt) AND enabled=1"
        )).fetchone())[0]
        disabled_tools = (await (await conn.execute(
            "SELECT COUNT(*) FROM lane_mcp_tools WHERE enabled=0"
        )).fetchone())[0]
        critiques = (await (await conn.execute(
            "SELECT COUNT(*) FROM lane_prompt_critiques"
        )).fetchone())[0]
    finally:
        await conn.close()
    return {
        "total_lanes": total,
        "customized_count": customized,
        "disabled_tools_count": disabled_tools,
        "critiques_available": critiques,
    }


@app.post("/api/lanes/run-critique")
async def run_lane_critique_batch():
    """Trigger one-time Opus 4.7 critique for all lanes. Returns immediately; runs in background."""
    import asyncio as _asyncio
    from lane_critique import run_critique_batch
    _asyncio.create_task(run_critique_batch())
    return {"status": "started", "message": "Opus critique batch running in background (~30s)"}


@app.get("/api/lanes/{name}")
async def get_lane(name: str):
    from lanes import get_one_lane
    lane = await get_one_lane(name)
    if not lane:
        raise HTTPException(404, f"Lane '{name}' not found")
    return lane


@app.put("/api/lanes/{name}")
async def update_lane_endpoint(name: str, body: dict):
    from lanes import update_lane, get_one_lane
    existing = await get_one_lane(name)
    if not existing:
        raise HTTPException(404, f"Lane '{name}' not found")
    allowed = {"max_agents", "default_model", "tool_preset", "append_prompt", "color", "icon", "enabled"}
    patch = {k: v for k, v in body.items() if k in allowed}
    updated = await update_lane(name, patch)
    return updated


@app.get("/api/lanes/{name}/prompt")
async def get_lane_prompt(name: str):
    from lanes import get_one_lane, parse_prompt_json
    lane = await get_one_lane(name)
    if not lane:
        raise HTTPException(404, f"Lane '{name}' not found")
    return parse_prompt_json(lane.get("append_prompt", ""))


@app.put("/api/lanes/{name}/prompt")
async def update_lane_prompt(name: str, body: dict):
    from lanes import get_one_lane, update_lane
    if not await get_one_lane(name):
        raise HTTPException(404, f"Lane '{name}' not found")
    allowed_keys = {"role", "rules", "quality_gates", "context_hints"}
    prompt_json = {k: v for k, v in body.items() if k in allowed_keys}
    await update_lane(name, {"append_prompt": json.dumps(prompt_json)})
    return prompt_json


@app.get("/api/lanes/{name}/mcp-tools")
async def get_lane_mcp_tools_endpoint(name: str):
    from lanes import get_lane_mcp_tools
    return await get_lane_mcp_tools(name)


@app.put("/api/lanes/{name}/mcp-tools/{server}/{tool}")
async def update_lane_mcp_tool(name: str, server: str, tool: str, body: dict):
    from lanes import upsert_lane_mcp_tool
    enabled = body.get("enabled", True)
    trigger_hint = body.get("trigger_hint", "always")
    return await upsert_lane_mcp_tool(name, server, tool, bool(enabled), trigger_hint)


@app.get("/api/lanes/{name}/prompt-critique")
async def get_lane_critique(name: str):
    conn = await db.get_db()
    try:
        row = await (await conn.execute(
            "SELECT * FROM lane_prompt_critiques WHERE lane_name = ?", (name,)
        )).fetchone()
    finally:
        await conn.close()
    if not row:
        return {"lane_name": name, "critique_json": None, "created_at": None}
    row_dict = dict(row)
    try:
        row_dict["critique_json"] = json.loads(row_dict["critique_json"])
    except Exception:
        pass
    return row_dict


@app.get("/api/fleet/summary")
async def fleet_summary():
    """Fleet health snapshot — total slots, running agents, free slots, and today's cost."""
    from lanes import total_capacity as lane_total_capacity
    total_slots = lane_total_capacity()
    running_agents = sum(1 for t in running_tasks.values() if not t.done())
    free_slots = max(0, total_slots - running_agents)
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT COALESCE(SUM(total_cost_usd), 0) AS cost_today
               FROM agent_sessions
               WHERE DATE(started_at) = DATE('now')"""
        )
        cost_today_usd = dict(rows[0])["cost_today"] if rows else 0.0
    finally:
        await conn.close()
    return {
        "total_slots": total_slots,
        "running_agents": running_agents,
        "free_slots": free_slots,
        "cost_today_usd": round(cost_today_usd, 6),
    }


# ──────────────────────────────────────────────
# Dispatch
# ──────────────────────────────────────────────

@app.post("/api/missions/{mid}/dispatch")
async def dispatch(request: Request, mid: str, body: DispatchOptions | None = None):
    running_count = sum(1 for t in running_tasks.values() if not t.done())
    if MAX_CONCURRENT_AGENTS > 0 and running_count >= MAX_CONCURRENT_AGENTS:
        raise HTTPException(429, f"Global agent ceiling reached ({running_count}/{MAX_CONCURRENT_AGENTS}) — wait for a slot")

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

        # Per-lane capacity check
        from lanes import check_slot
        ok, reason = await check_slot(mission)
        if not ok:
            raise HTTPException(429, f"Lane full: {reason}")

        # Get last report for context
        reports = await conn.execute_fetchall(
            "SELECT * FROM reports WHERE mission_id=? ORDER BY created_at DESC LIMIT 1",
            (mid,),
        )
        last_report = dict(reports[0]) if reports else None

        session_id = str(uuid.uuid4())
        model_used = (body and body.model) or mission.get("model") or "claude-opus-4-7"
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

    # Look up calling user's GitHub token (if they have one stored)
    _dispatch_user = getattr(request.state, "user", None)
    _github_token = None
    if _dispatch_user:
        _gh_rows = await (await (await db.get_db()).execute(
            "SELECT github_token FROM users WHERE id=?", (_dispatch_user.get("sub"),)
        )).fetchone()
        if _gh_rows and _gh_rows[0]:
            _github_token = _gh_rows[0]

    task = asyncio.create_task(
        dispatch_mission(session_id, mission, last_report, opts=body, github_token=_github_token)
    )
    running_tasks[session_id] = task

    return {"session_id": session_id, "status": "running", "model": model_used}


@app.post("/api/missions/{mid}/resume")
async def resume(mid: str, body: DispatchOptions | None = None):
    """Resume a failed mission from its last Claude session."""
    running_count = sum(1 for t in running_tasks.values() if not t.done())
    if MAX_CONCURRENT_AGENTS > 0 and running_count >= MAX_CONCURRENT_AGENTS:
        raise HTTPException(429, f"Global agent ceiling reached ({running_count}/{MAX_CONCURRENT_AGENTS}) — wait for a slot")

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


# ──────────────────────────────────────────────
# Sessions
# ──────────────────────────────────────────────

@app.get("/api/sessions")
async def list_sessions(
    response: Response,
    mission_id: str = Query(None),
    status: str = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conn = await db.get_db()
    try:
        base_where = "WHERE 1=1"
        params = []
        if mission_id:
            base_where += " AND s.mission_id=?"
            params.append(mission_id)
        if status:
            base_where += " AND s.status=?"
            params.append(status)
        # Total count uses the same JOINs as the data query so orphan rows
        # (sessions whose mission/project was deleted) aren't counted as matches.
        count_rows = await conn.execute_fetchall(
            f"""SELECT COUNT(*) AS n FROM agent_sessions s
                JOIN missions m ON m.id = s.mission_id
                JOIN projects p ON p.id = m.project_id
                {base_where}""",
            params,
        )
        total = count_rows[0]["n"] if count_rows else 0
        query = f"""SELECT s.*, m.title AS mission_title, p.name AS project_name
                    FROM agent_sessions s
                    JOIN missions m ON m.id = s.mission_id
                    JOIN projects p ON p.id = m.project_id
                    {base_where}
                    ORDER BY s.started_at DESC
                    LIMIT ? OFFSET ?"""
        rows = await conn.execute_fetchall(query, params + [limit, offset])
        response.headers["X-Total-Count"] = str(total)
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
async def list_reports(
    response: Response,
    project_id: str = Query(None),
    mission_id: str = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conn = await db.get_db()
    try:
        base_where = "WHERE 1=1"
        params = []
        if mission_id:
            base_where += " AND r.mission_id=?"
            params.append(mission_id)
        if project_id:
            base_where += " AND m.project_id=?"
            params.append(project_id)
        count_rows = await conn.execute_fetchall(
            f"""SELECT COUNT(*) AS n FROM reports r
                JOIN missions m ON m.id = r.mission_id
                {base_where}""",
            params,
        )
        total = count_rows[0]["n"] if count_rows else 0
        query = f"""SELECT r.*, m.title AS mission_title, p.name AS project_name
                    FROM reports r
                    JOIN missions m ON m.id = r.mission_id
                    JOIN projects p ON p.id = m.project_id
                    {base_where}
                    ORDER BY r.created_at DESC
                    LIMIT ? OFFSET ?"""
        rows = await conn.execute_fetchall(query, params + [limit, offset])
        response.headers["X-Total-Count"] = str(total)
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
        # Use DB as source of truth — in-memory running_tasks loses sessions on restart
        db_running = await conn.execute_fetchall(
            "SELECT COUNT(*) AS c FROM agent_sessions WHERE status = 'running'"
        )
        running_agents = max(
            sum(1 for t in running_tasks.values() if not t.done()),
            dict(db_running[0])["c"] if db_running else 0
        )
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
        raise HTTPException(500, "Planning failed")


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
        raise HTTPException(500, "Planning failed")


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
        raise HTTPException(500, "Analysis failed")


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
        raise HTTPException(500, "Health check failed")


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
        raise HTTPException(500, "Graph generation failed")


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
        raise HTTPException(500, "Diagram generation failed")


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
        raise HTTPException(500, "Cost analysis failed")


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


@app.delete("/api/sessions/{sid}/remote-control", status_code=204)
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
            raise HTTPException(404, "Project not found")
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


@app.delete("/api/services/{sid}", status_code=204)
async def delete_service(sid: str):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM monitored_services WHERE id=?", (sid,))
        if not rows:
            raise HTTPException(404, "Service not found")
        await conn.execute("DELETE FROM monitored_services WHERE id=?", (sid,))
        await conn.commit()
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


@app.delete("/api/incidents/{iid}", status_code=204)
async def delete_incident(iid: str):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM incidents WHERE id=?", (iid,))
        if not rows:
            raise HTTPException(404, "Incident not found")
        await conn.execute("DELETE FROM incidents WHERE id=?", (iid,))
        await conn.commit()
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


@app.delete("/api/mcp-servers/{mid}", status_code=204)
async def delete_mcp_server(mid: str):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM mcp_configs WHERE id=?", (mid,))
        if not rows:
            raise HTTPException(404, "MCP server config not found")
        await conn.execute("DELETE FROM mcp_configs WHERE id=?", (mid,))
        await conn.commit()
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


@app.delete("/api/missions/{mid}/schedule", status_code=204)
async def remove_schedule(mid: str):
    """Disable scheduling on a mission."""
    conn = await db.get_db()
    try:
        await conn.execute(
            "UPDATE missions SET schedule_enabled=0, updated_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), mid),
        )
        await conn.commit()
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


_tunnel_status_cache: dict = {"value": None, "expires_at": 0.0}
_TUNNEL_STATUS_TTL_SEC = 15.0


def _tunnel_status() -> dict:
    """Check cloudflared tunnel health. Prefers the local /ready endpoint
    (sub-millisecond, Sydney-local) over `cloudflared tunnel info` (which
    round-trips to Cloudflare's US control plane — adds ~640ms per call).

    Result is cached for _TUNNEL_STATUS_TTL_SEC so the dashboard's 5s poll
    doesn't pay the cost on every tick even if the local endpoint is down.
    """
    import time as _time
    now = _time.monotonic()
    cached = _tunnel_status_cache["value"]
    if cached is not None and now < _tunnel_status_cache["expires_at"]:
        return cached

    result = _tunnel_status_uncached()
    _tunnel_status_cache["value"] = result
    _tunnel_status_cache["expires_at"] = now + _TUNNEL_STATUS_TTL_SEC
    return result


def _tunnel_status_uncached() -> dict:
    # Fast path — local cloudflared metrics endpoint (auto-discovered via lsof)
    import subprocess, json as _json
    fallback = {"connected": False, "url": None, "connections": 0}
    try:
        port = _discover_cloudflared_metrics_port()
        if port:
            import urllib.request, urllib.error
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ready", timeout=1) as r:
                data = _json.loads(r.read())
            conns = int(data.get("readyConnections", 0))
            return {
                "connected": conns > 0,
                "url": "https://farhanfleet.nexis365.com.au" if conns > 0 else None,
                "connections": conns,
            }
    except Exception:
        pass

    # Slow fallback — only used when the local metrics endpoint is unreachable
    try:
        proc = subprocess.run(
            ["cloudflared", "tunnel", "info", "--output", "json", "farhanfleet"],
            capture_output=True, text=True, timeout=4,
        )
        if proc.returncode == 0:
            data = _json.loads(proc.stdout)
            conns = data.get("conns") or []
            connected = len(conns) > 0
            return {
                "connected": connected,
                "url": "https://farhanfleet.nexis365.com.au" if connected else None,
                "connections": len(conns),
            }
    except Exception:
        pass
    return fallback


_cloudflared_port_cache: dict = {"port": None, "checked_at": 0.0}
_PORT_DISCOVERY_TTL_SEC = 60.0


def _discover_cloudflared_metrics_port() -> int | None:
    """Find the metrics port cloudflared is listening on. Cached for 60s
    because the port doesn't change unless cloudflared restarts."""
    import time as _time, subprocess
    now = _time.monotonic()
    if _cloudflared_port_cache["port"] is not None and \
            now - _cloudflared_port_cache["checked_at"] < _PORT_DISCOVERY_TTL_SEC:
        return _cloudflared_port_cache["port"]

    try:
        # `lsof -an -iTCP -sTCP:LISTEN -c cloudflared` lists listening sockets;
        # parse the first 127.0.0.1:<port>. Use absolute path because launchd's
        # PATH doesn't include /usr/sbin where macOS keeps lsof.
        lsof_bin = "/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else "lsof"
        out = subprocess.run(
            [lsof_bin, "-an", "-iTCP", "-sTCP:LISTEN", "-c", "cloudflared", "-Fn"],
            capture_output=True, text=True, timeout=2,
        )
        port: int | None = None
        for line in out.stdout.splitlines():
            if line.startswith("n127.0.0.1:"):
                port = int(line.split(":", 1)[1])
                break
        _cloudflared_port_cache["port"] = port
        _cloudflared_port_cache["checked_at"] = now
        return port
    except Exception:
        return None


@app.get("/api/system/status")
async def system_status():
    """Get system-wide status: watcher, scheduler, running agents, tunnel."""
    from lanes import total_capacity as lane_total_capacity
    running_count = sum(1 for t in running_tasks.values() if not t.done())
    total_slots = lane_total_capacity()
    return {
        "running_agents": running_count,
        "max_agents": MAX_CONCURRENT_AGENTS,
        "total_slots": total_slots,
        "free_slots": max(0, total_slots - running_count),
        "engine": "sdk" if USE_SDK_ENGINE else "cli",
        "mission_watcher": mission_watcher.get_watcher_status(),
        "scheduler": scheduler.get_scheduler_status(),
        "tunnel": _tunnel_status(),
    }


@app.patch("/api/system/ceiling")
async def set_ceiling(request: Request, body: CeilingUpdate):
    """Set the global agent ceiling at runtime. Admin only."""
    _u = getattr(request.state, "user", None)
    if not _u or _u.get("role") != "admin":
        raise HTTPException(403, "Admin access required")
    if body.max_agents < 0:
        raise HTTPException(400, "max_agents must be a non-negative integer")
    global MAX_CONCURRENT_AGENTS
    MAX_CONCURRENT_AGENTS = body.max_agents
    running_count = sum(1 for t in running_tasks.values() if not t.done())
    return {"max_agents": MAX_CONCURRENT_AGENTS, "running_agents": running_count}


@app.get("/api/system/features")
async def system_features():
    """Get enabled features."""
    return {
        "remote_control": ENABLE_REMOTE_CONTROL,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Frontend SPA serving — must be registered LAST so /api/* routes take precedence
# ──────────────────────────────────────────────────────────────────────────────
_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

if _FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        """Serve index.html for all non-API client-side routes (React Router fallback)."""
        if full_path.startswith("api/") or full_path.startswith("mcp"):
            raise HTTPException(404, "Not Found")
        candidate = _FRONTEND_DIST / full_path
        if candidate.is_file() and not candidate.is_symlink():
            try:
                candidate.resolve().relative_to(_FRONTEND_DIST.resolve())
                return FileResponse(candidate)
            except ValueError:
                pass
        return FileResponse(_FRONTEND_DIST / "index.html")
else:
    log.warning("Frontend dist not found at %s — skipping SPA mount (run `npm run build` in frontend/)", _FRONTEND_DIST)
