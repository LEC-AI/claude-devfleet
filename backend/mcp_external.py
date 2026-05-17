"""
Claude DevFleet MCP Server — External integration endpoint.

Exposes Claude DevFleet as an MCP server so any MCP-compatible client
(Claude Code, Cursor, Windsurf, Cline, custom agents) can:
  - Plan projects from natural language
  - Create projects and missions
  - Dispatch agents
  - Check mission status and read reports
  - List and browse projects/missions

Mount via SSE transport at /mcp on the FastAPI app.
"""

import asyncio
import json
import logging
import os
import re
import uuid

from mcp.server import Server
import mcp.types as types

import db

log = logging.getLogger("devfleet.mcp-external")

server = Server("devfleet")


# ── Helper: resolve projects dir ──

def _projects_base() -> str:
    base = os.environ.get("DEVFLEET_PROJECTS_DIR")
    if not base:
        devfleet_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        base = os.path.join(devfleet_root, "projects")
    return base


def _slugify(text: str, max_len: int = 40) -> str:
    return re.sub(r'[^a-z0-9]+', '-', text.lower().strip())[:max_len].strip('-')


# ── Tool Definitions ──

TOOLS = [
    types.Tool(
        name="plan_project",
        description=(
            "Plan a project from a natural language description. "
            "AI breaks the prompt into a project with chained missions, "
            "dependencies, and auto-dispatch. Returns project ID and mission list."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Natural language description of what to build"
                },
                "project_path": {
                    "type": "string",
                    "description": "Optional filesystem path for the project. Auto-generated if not provided."
                },
            },
            "required": ["prompt"],
        },
    ),
    types.Tool(
        name="create_project",
        description="Create a new Claude DevFleet project manually.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name"},
                "path": {"type": "string", "description": "Filesystem path for the project. Auto-generated if not provided."},
                "description": {"type": "string", "description": "Project description"},
            },
            "required": ["name"],
        },
    ),
    types.Tool(
        name="create_mission",
        description=(
            "Create a mission (task) in an existing project. "
            "Supports dependencies, auto-dispatch, and priority."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "ID of the project"},
                "title": {"type": "string", "description": "Mission title"},
                "prompt": {"type": "string", "description": "Detailed prompt / instructions for the agent"},
                "acceptance_criteria": {"type": "string", "description": "What counts as done"},
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of mission IDs this depends on"
                },
                "auto_dispatch": {"type": "boolean", "description": "Auto-dispatch when dependencies complete"},
                "priority": {"type": "integer", "description": "Priority (0=normal, 1=high, 2=critical)"},
                "model": {"type": "string", "description": "Model to use (default: claude-sonnet-4-6)"},
            },
            "required": ["project_id", "title", "prompt"],
        },
    ),
    types.Tool(
        name="dispatch_mission",
        description="Dispatch an agent to work on a mission. The agent runs asynchronously.",
        inputSchema={
            "type": "object",
            "properties": {
                "mission_id": {"type": "string", "description": "ID of the mission to dispatch"},
                "model": {"type": "string", "description": "Override model for this dispatch"},
                "max_turns": {"type": "integer", "description": "Max conversation turns"},
            },
            "required": ["mission_id"],
        },
    ),
    types.Tool(
        name="get_mission_status",
        description=(
            "Get current status and details of a mission including its latest session and report."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "mission_id": {"type": "string", "description": "Mission ID"},
            },
            "required": ["mission_id"],
        },
    ),
    types.Tool(
        name="get_report",
        description=(
            "Get the structured report from a completed mission — "
            "what was done, tested, untested, files changed, errors, and next steps."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "mission_id": {"type": "string", "description": "Mission ID"},
            },
            "required": ["mission_id"],
        },
    ),
    types.Tool(
        name="list_projects",
        description="List all Claude DevFleet projects.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    types.Tool(
        name="list_missions",
        description="List missions in a project, optionally filtered by status.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "status": {
                    "type": "string",
                    "description": "Filter by status (draft, running, completed, failed)",
                    "enum": ["draft", "running", "completed", "failed"],
                },
            },
            "required": ["project_id"],
        },
    ),
    types.Tool(
        name="cancel_mission",
        description="Cancel a running mission and stop its agent.",
        inputSchema={
            "type": "object",
            "properties": {
                "mission_id": {"type": "string", "description": "Mission ID to cancel"},
            },
            "required": ["mission_id"],
        },
    ),
    types.Tool(
        name="wait_for_mission",
        description=(
            "Wait for a mission to complete and return its final status and report. "
            "Polls every 5 seconds. Use after dispatch_mission to block until done."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "mission_id": {"type": "string", "description": "Mission ID to wait for"},
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Max seconds to wait (default: 600, max: 1800)",
                },
            },
            "required": ["mission_id"],
        },
    ),
    types.Tool(
        name="get_dashboard",
        description=(
            "Get a high-level dashboard of Claude DevFleet: running agents, "
            "project count, mission stats, and recent activity."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
]


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    from plugins import registry
    return TOOLS + registry.tools


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        # Check plugin tools first
        from plugins import registry
        if name in registry.tool_handlers:
            result = await registry.tool_handlers[name](arguments)
        else:
            result = await _handle_tool(name, arguments)
        return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]
    except Exception as e:
        log.exception(f"MCP tool {name} failed")
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def _handle_tool(name: str, args: dict) -> dict:
    conn = await db.get_db()
    try:
        if name == "plan_project":
            return await _plan_project(args, conn)
        elif name == "create_project":
            return await _create_project(args, conn)
        elif name == "create_mission":
            return await _create_mission(args, conn)
        elif name == "dispatch_mission":
            return await _dispatch_mission(args, conn)
        elif name == "get_mission_status":
            return await _get_mission_status(args, conn)
        elif name == "get_report":
            return await _get_report(args, conn)
        elif name == "list_projects":
            return await _list_projects(conn)
        elif name == "list_missions":
            return await _list_missions(args, conn)
        elif name == "cancel_mission":
            return await _cancel_mission(args, conn)
        elif name == "wait_for_mission":
            return await _wait_for_mission(args)
        elif name == "get_dashboard":
            return await _get_dashboard(conn)
        else:
            return {"error": f"Unknown tool: {name}"}
    finally:
        await conn.close()


# ── Tool Implementations ──

async def _plan_project(args: dict, conn) -> dict:
    from planner import plan_project

    prompt = args["prompt"]
    project_path = args.get("project_path")
    if not project_path:
        slug = _slugify(prompt)
        project_path = os.path.join(_projects_base(), slug)

    result = await plan_project(prompt, project_path)
    return {
        "project_id": result["project"]["id"],
        "project_name": result["project"]["name"],
        "project_path": project_path,
        "missions": [
            {
                "id": m["id"],
                "number": m["mission_number"],
                "title": m["title"],
                "depends_on": m["depends_on"],
                "auto_dispatch": m["auto_dispatch"],
            }
            for m in result["missions"]
        ],
        "hint": "Dispatch the first mission to start the chain. The rest auto-dispatch as dependencies complete.",
    }


async def _create_project(args: dict, conn) -> dict:
    pid = str(uuid.uuid4())
    name = args["name"]
    path = args.get("path") or os.path.join(_projects_base(), _slugify(name))
    description = args.get("description", "")

    os.makedirs(path, exist_ok=True)

    await conn.execute(
        "INSERT INTO projects (id, name, path, description) VALUES (?, ?, ?, ?)",
        (pid, name, path, description),
    )
    await conn.commit()

    return {"id": pid, "name": name, "path": path, "description": description}


async def _create_mission(args: dict, conn) -> dict:
    mid = str(uuid.uuid4())

    # Verify project exists
    row = await conn.execute("SELECT id FROM projects WHERE id = ?", (args["project_id"],))
    if not await row.fetchone():
        return {"error": f"Project {args['project_id']} not found"}

    # Get next mission number
    cur = await conn.execute(
        "SELECT COALESCE(MAX(mission_number), 0) + 1 FROM missions WHERE project_id = ?",
        (args["project_id"],),
    )
    next_num = (await cur.fetchone())[0]

    depends_on = json.dumps(args.get("depends_on", []))
    auto_dispatch = 1 if args.get("auto_dispatch", False) else 0

    await conn.execute(
        """INSERT INTO missions
           (id, project_id, title, detailed_prompt, acceptance_criteria,
            depends_on, auto_dispatch, priority, model, mission_number)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            mid,
            args["project_id"],
            args["title"],
            args["prompt"],
            args.get("acceptance_criteria", ""),
            depends_on,
            auto_dispatch,
            args.get("priority", 0),
            args.get("model", "claude-sonnet-4-6"),
            next_num,
        ),
    )
    await conn.commit()

    return {
        "id": mid,
        "mission_number": next_num,
        "title": args["title"],
        "project_id": args["project_id"],
        "auto_dispatch": bool(auto_dispatch),
        "depends_on": args.get("depends_on", []),
    }


async def _dispatch_mission(args: dict, conn) -> dict:
    import uuid as _uuid
    from datetime import datetime, timezone

    mid = args["mission_id"]

    # Fetch mission with project path (needed by dispatch engine)
    cur = await conn.execute(
        "SELECT m.*, p.path AS project_path FROM missions m "
        "JOIN projects p ON p.id = m.project_id WHERE m.id = ?",
        (mid,),
    )
    mission = await cur.fetchone()
    if not mission:
        return {"error": f"Mission {mid} not found"}

    mission = dict(mission)
    if mission["status"] == "running":
        return {"error": "Mission is already running"}

    # Check agent slot availability
    from app import running_tasks, MAX_CONCURRENT_AGENTS

    running_count = sum(1 for t in running_tasks.values() if not t.done())
    if running_count >= MAX_CONCURRENT_AGENTS:
        return {"error": f"All {MAX_CONCURRENT_AGENTS} agent slots in use. Wait for one to finish or cancel a running mission."}

    # Get last report for context (matches app.py flow)
    cur = await conn.execute(
        "SELECT * FROM reports WHERE mission_id = ? ORDER BY created_at DESC LIMIT 1",
        (mid,),
    )
    report_row = await cur.fetchone()
    last_report = dict(report_row) if report_row else None

    # Create session in DB (matches app.py flow)
    session_id = str(_uuid.uuid4())
    model_used = args.get("model") or mission.get("model") or "claude-sonnet-4-6"
    await conn.execute(
        "INSERT INTO agent_sessions (id, mission_id, model) VALUES (?, ?, ?)",
        (session_id, mid, model_used),
    )
    await conn.execute(
        "UPDATE missions SET status='running', updated_at=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), mid),
    )
    await conn.commit()

    # Import and dispatch
    USE_SDK = os.environ.get("DEVFLEET_ENGINE", "sdk").lower() == "sdk"
    if USE_SDK:
        from sdk_engine import dispatch_mission
    else:
        from dispatcher import dispatch_mission

    # Build opts from args
    from models import DispatchOptions

    opts_kwargs = {}
    if args.get("model"):
        opts_kwargs["model"] = args["model"]
    if args.get("max_turns"):
        opts_kwargs["max_turns"] = args["max_turns"]
    opts = DispatchOptions(**opts_kwargs) if opts_kwargs else None

    # Dispatch asynchronously (matches app.py flow)
    import asyncio

    task = asyncio.create_task(
        dispatch_mission(session_id, mission, last_report, opts=opts)
    )
    running_tasks[session_id] = task

    return {
        "session_id": session_id,
        "mission_id": mid,
        "status": "dispatched",
        "model": model_used,
        "hint": "Mission is now running. Use get_mission_status to check progress.",
    }


async def _get_mission_status(args: dict, conn) -> dict:
    mid = args["mission_id"]

    cur = await conn.execute("SELECT * FROM missions WHERE id = ?", (mid,))
    mission = await cur.fetchone()
    if not mission:
        return {"error": f"Mission {mid} not found"}
    mission = dict(mission)

    # Get latest session
    cur = await conn.execute(
        "SELECT * FROM agent_sessions WHERE mission_id = ? ORDER BY started_at DESC LIMIT 1",
        (mid,),
    )
    session = await cur.fetchone()

    result = {
        "id": mission["id"],
        "title": mission["title"],
        "status": mission["status"],
        "mission_number": mission["mission_number"],
        "depends_on": json.loads(mission["depends_on"] or "[]"),
        "auto_dispatch": bool(mission["auto_dispatch"]),
    }

    if session:
        session = dict(session)
        result["session"] = {
            "id": session["id"],
            "status": session["status"],
            "started_at": session["started_at"],
            "ended_at": session["ended_at"],
            "total_cost_usd": session["total_cost_usd"],
            "total_tokens": session["total_tokens"],
        }

    return result


async def _get_report(args: dict, conn) -> dict:
    mid = args["mission_id"]

    cur = await conn.execute(
        "SELECT * FROM reports WHERE mission_id = ? ORDER BY created_at DESC LIMIT 1",
        (mid,),
    )
    report = await cur.fetchone()
    if not report:
        return {"error": f"No report found for mission {mid}", "hint": "The mission may not have completed yet."}

    report = dict(report)
    return {
        "mission_id": mid,
        "files_changed": report["files_changed"],
        "what_done": report["what_done"],
        "what_open": report["what_open"],
        "what_tested": report["what_tested"],
        "what_untested": report["what_untested"],
        "next_steps": report["next_steps"],
        "errors_encountered": report["errors_encountered"],
        "created_at": report["created_at"],
    }


async def _list_projects(conn) -> dict:
    cur = await conn.execute("SELECT id, name, path, description, created_at FROM projects ORDER BY created_at DESC")
    rows = await cur.fetchall()
    return {
        "projects": [dict(r) for r in rows],
        "count": len(rows),
    }


async def _list_missions(args: dict, conn) -> dict:
    pid = args["project_id"]
    status = args.get("status")

    if status:
        cur = await conn.execute(
            "SELECT id, title, status, mission_number, depends_on, auto_dispatch, priority, created_at "
            "FROM missions WHERE project_id = ? AND status = ? ORDER BY mission_number",
            (pid, status),
        )
    else:
        cur = await conn.execute(
            "SELECT id, title, status, mission_number, depends_on, auto_dispatch, priority, created_at "
            "FROM missions WHERE project_id = ? ORDER BY mission_number",
            (pid,),
        )

    rows = await cur.fetchall()
    missions = []
    for r in rows:
        m = dict(r)
        m["depends_on"] = json.loads(m["depends_on"] or "[]")
        m["auto_dispatch"] = bool(m["auto_dispatch"])
        missions.append(m)

    return {"missions": missions, "count": len(missions)}


async def _cancel_mission(args: dict, conn) -> dict:
    mid = args["mission_id"]

    # Find running session
    cur = await conn.execute(
        "SELECT id, pid FROM agent_sessions WHERE mission_id = ? AND status = 'running' ORDER BY started_at DESC LIMIT 1",
        (mid,),
    )
    session = await cur.fetchone()
    if not session:
        return {"error": f"No running session for mission {mid}"}

    session = dict(session)
    sid = session["id"]

    # Try to cancel the process
    try:
        from sdk_engine import cancel_session
        await cancel_session(sid)
    except Exception as e:
        log.warning(f"cancel_session failed for {sid}: {e}")
        # Fallback: kill PID if available
        pid = session.get("pid")
        if pid:
            try:
                import signal
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    # Update status
    await conn.execute("UPDATE agent_sessions SET status = 'cancelled' WHERE id = ?", (sid,))
    await conn.execute("UPDATE missions SET status = 'failed' WHERE id = ?", (mid,))
    await conn.commit()

    return {"mission_id": mid, "session_id": sid, "status": "cancelled"}


async def _wait_for_mission(args: dict) -> dict:
    mid = args["mission_id"]
    timeout = min(args.get("timeout_seconds", 600), 1800)  # cap at 30 min
    elapsed = 0
    poll_interval = 5

    while elapsed < timeout:
        conn = await db.get_db()
        try:
            cur = await conn.execute("SELECT status FROM missions WHERE id = ?", (mid,))
            row = await cur.fetchone()
            if not row:
                return {"error": f"Mission {mid} not found"}

            status = row["status"]
            if status in ("completed", "failed"):
                # Get report if available
                result = await _get_mission_status({"mission_id": mid}, conn)
                report_cur = await conn.execute(
                    "SELECT * FROM reports WHERE mission_id = ? ORDER BY created_at DESC LIMIT 1",
                    (mid,),
                )
                report = await report_cur.fetchone()
                if report:
                    report = dict(report)
                    result["report"] = {
                        "what_done": report["what_done"],
                        "what_tested": report["what_tested"],
                        "what_untested": report["what_untested"],
                        "files_changed": report["files_changed"],
                        "errors_encountered": report["errors_encountered"],
                        "next_steps": report["next_steps"],
                    }
                return result
        finally:
            await conn.close()

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    return {"mission_id": mid, "status": "timeout", "error": f"Mission did not complete within {timeout}s"}


async def _get_dashboard(conn) -> dict:
    # Project count
    cur = await conn.execute("SELECT COUNT(*) FROM projects")
    project_count = (await cur.fetchone())[0]

    # Mission stats
    cur = await conn.execute(
        "SELECT status, COUNT(*) as cnt FROM missions GROUP BY status"
    )
    mission_stats = {row["status"]: row["cnt"] for row in await cur.fetchall()}

    # Running agents
    cur = await conn.execute(
        "SELECT s.id, s.mission_id, m.title FROM agent_sessions s "
        "JOIN missions m ON s.mission_id = m.id WHERE s.status = 'running'"
    )
    running = [dict(r) for r in await cur.fetchall()]

    # Recent completions (last 5)
    cur = await conn.execute(
        "SELECT m.id, m.title, m.status, s.ended_at, s.total_cost_usd "
        "FROM missions m LEFT JOIN agent_sessions s ON m.id = s.mission_id "
        "WHERE m.status IN ('completed', 'failed') "
        "ORDER BY s.ended_at DESC LIMIT 5"
    )
    recent = [dict(r) for r in await cur.fetchall()]

    return {
        "projects": project_count,
        "missions": mission_stats,
        "running_agents": running,
        "agent_slots": f"{len(running)}/{os.environ.get('DEVFLEET_MAX_AGENTS', '3')}",
        "recent_activity": recent,
    }
