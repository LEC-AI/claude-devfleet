"""
DevFleet MCP Server — Self-Service Agent Tools

A stdio MCP server that lets agents interact with DevFleet itself:
- Submit structured reports (replaces text markers)
- Create sub-missions with auto-dispatch (agents decompose work)
- Request review from another agent
- Check sub-mission status (multi-agent coordination)

Runs as a subprocess spawned by the SDK via McpStdioServerConfig.
Connects to the DevFleet API (localhost) to perform actions.
"""

import asyncio
import json
import os

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

DEVFLEET_API = os.environ.get("DEVFLEET_API_URL", "http://localhost:18801")
MISSION_ID = os.environ.get("DEVFLEET_MISSION_ID", "")
PROJECT_ID = os.environ.get("DEVFLEET_PROJECT_ID", "")
SESSION_ID = os.environ.get("DEVFLEET_SESSION_ID", "")

server = Server("devfleet-tools")


async def _api_post(path: str, data: dict) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{DEVFLEET_API}{path}", json=data)
            if resp.status_code in (200, 201):
                return resp.json()
    except Exception:
        pass
    return None


async def _api_get(path: str) -> dict | list | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{DEVFLEET_API}{path}")
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return None


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="submit_report",
            description=(
                "Submit a structured end-of-mission report to DevFleet. Call this when your work is complete. "
                "Your report feeds into the next agent's context — be precise and actionable. "
                "Flag any blockers that need human intervention (sudo, API keys, DNS, etc)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "files_changed": {
                        "type": "string",
                        "description": "List every file created/modified/deleted with one-line descriptions. Example: 'src/api.py (created) — REST API with /users endpoint'",
                    },
                    "what_done": {
                        "type": "string",
                        "description": "Bullet list of what you accomplished. Be specific — mention function names, endpoints, components. Another agent should know exactly what exists now.",
                    },
                    "what_open": {
                        "type": "string",
                        "description": "What remains to complete the full mission. Be honest about half-done work. Say 'None' if everything is done.",
                    },
                    "what_tested": {
                        "type": "string",
                        "description": "Exactly what you verified works — include commands run and results. Example: 'Ran npm test — 12 tests pass'. If no tests, say 'Manual verification only' and explain what you checked.",
                    },
                    "what_untested": {
                        "type": "string",
                        "description": "What you did NOT verify — edge cases, error handling, cross-browser, performance, etc. The next agent/human needs to know what to check.",
                    },
                    "next_steps": {
                        "type": "string",
                        "description": "Specific actionable recommendations for the NEXT mission/agent. Frame as mission titles. Example: 'Add authentication middleware — JWT tokens for /users'. Say 'None — mission complete' if fully done.",
                    },
                    "errors_encountered": {
                        "type": "string",
                        "description": "Errors, blockers, or issues needing HUMAN attention — permission issues, sudo commands needed, API keys required, services to start, DNS changes. Example: 'BLOCKER: Need sudo systemctl restart nginx'. Say 'None' if no blockers.",
                    },
                    "preview_url": {"type": "string", "description": "Preview URL (http://localhost:4321) or 'None — no UI'"},
                },
                "required": ["files_changed", "what_done", "what_open", "what_tested", "what_untested", "next_steps", "errors_encountered"],
            },
        ),
        types.Tool(
            name="create_sub_mission",
            description=(
                "Create a sub-mission that gets auto-dispatched to another agent. "
                "Use this to decompose complex work into parallel tasks. "
                "By default, the sub-mission starts immediately. Set wait_for_me=true "
                "if it should wait until your mission completes first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short task title"},
                    "detailed_prompt": {"type": "string", "description": "Full implementation prompt"},
                    "acceptance_criteria": {"type": "string", "description": "What defines done"},
                    "mission_type": {
                        "type": "string",
                        "enum": ["implement", "review", "test", "fix", "explore", "security", "e2e", "qa", "research", "planner"],
                        "description": "Type of mission — determines which lane it runs in (default: implement → coder lane)",
                    },
                    "lane": {
                        "type": "string",
                        "enum": ["coder", "reviewer", "tester", "e2e", "security", "qa", "dynamic_tester", "researcher", "explorer", "orchestrator"],
                        "description": "Override the lane directly. If omitted, derived from mission_type. Use get_fleet_shape to check which lanes have free slots before dispatching.",
                    },
                    "priority": {"type": "integer", "description": "Priority 0-5 (default: 2)"},
                    "wait_for_me": {
                        "type": "boolean",
                        "description": "If true, sub-mission waits for this mission to complete before starting (default: false)",
                    },
                },
                "required": ["title", "detailed_prompt"],
            },
        ),
        types.Tool(
            name="request_review",
            description="Request a code review agent to review your changes. Creates and auto-dispatches a review mission.",
            inputSchema={
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "What to review and what to look for"},
                    "files_to_review": {"type": "string", "description": "Specific files or areas to focus on"},
                },
                "required": ["description"],
            },
        ),
        types.Tool(
            name="get_sub_mission_status",
            description="Check the status of sub-missions you created. Shows if they're running, completed, or failed.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="list_project_missions",
            description="List all missions in the current project to understand the broader context",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["draft", "running", "completed", "failed"],
                        "description": "Filter by status",
                    },
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "submit_report":
        return await _submit_report(arguments)
    elif name == "create_sub_mission":
        return await _create_sub_mission(arguments)
    elif name == "request_review":
        return await _request_review(arguments)
    elif name == "get_sub_mission_status":
        return await _get_sub_mission_status(arguments)
    elif name == "list_project_missions":
        return await _list_project_missions(arguments)
    else:
        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


async def _submit_report(args: dict) -> list[types.TextContent]:
    """Write report data to a temp file that the SDK engine reads after completion."""
    report = {
        "files_changed": args.get("files_changed", ""),
        "what_done": args.get("what_done", ""),
        "what_open": args.get("what_open", ""),
        "what_tested": args.get("what_tested", ""),
        "what_untested": args.get("what_untested", ""),
        "next_steps": args.get("next_steps", ""),
        "errors_encountered": args.get("errors_encountered", ""),
        "preview_url": args.get("preview_url", ""),
    }

    # Write to a session-specific file that the SDK engine picks up
    report_dir = os.environ.get("DEVFLEET_REPORT_DIR", "/tmp/devfleet-reports")
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, f"{SESSION_ID}.json")
    with open(report_path, "w") as f:
        json.dump(report, f)

    return [types.TextContent(type="text", text="Report submitted successfully to DevFleet.")]


async def _create_sub_mission(args: dict) -> list[types.TextContent]:
    if not PROJECT_ID:
        return [types.TextContent(type="text", text="Cannot create sub-mission: no project context")]

    mission_type = args.get("mission_type", "implement")
    lane = args.get("lane")

    # Pre-check lane capacity before creating the mission
    capacity = await _api_get("/api/lanes")
    if capacity:
        from_lanes = {l["name"]: l for l in capacity}
        # Resolve target lane (explicit override > mission_type mapping)
        target_lane = lane
        if not target_lane:
            type_to_lane = {
                "implement": "coder", "fix": "coder", "full": "coder",
                "review": "reviewer", "security": "security", "test": "tester",
                "e2e": "e2e", "qa": "qa", "research": "researcher",
                "planner": "orchestrator", "orchestrator": "orchestrator", "explore": "explorer",
            }
            target_lane = type_to_lane.get(mission_type, "coder")

        lane_info = from_lanes.get(target_lane)
        if lane_info and lane_info.get("free", 0) == 0:
            # Suggest alternatives
            free_lanes = [l["name"] for l in capacity if l.get("free", 0) > 0]
            return [types.TextContent(type="text", text=(
                f"Lane '{target_lane}' is full ({lane_info['running']}/{lane_info['max_agents']} slots used). "
                f"Free lanes: {', '.join(free_lanes) or 'none'}. "
                f"Use get_fleet_shape to see current capacity."
            ))]

    wait_for_me = args.get("wait_for_me", False)
    depends_on = [MISSION_ID] if (wait_for_me and MISSION_ID) else []

    payload = {
        "project_id": PROJECT_ID,
        "title": args["title"],
        "detailed_prompt": args["detailed_prompt"],
        "acceptance_criteria": args.get("acceptance_criteria", ""),
        "mission_type": mission_type,
        "priority": args.get("priority", 2),
        "tags": ["sub-mission"],
        "parent_mission_id": MISSION_ID or None,
        "depends_on": depends_on,
        "auto_dispatch": True,
    }
    if lane:
        payload["lane"] = lane

    result = await _api_post("/api/missions", payload)

    if result:
        dispatch_note = "Will start after your mission completes." if wait_for_me else "Auto-dispatching to another agent now."
        target = result.get("lane") or result.get("mission_type", "")
        return [types.TextContent(type="text",
            text=f"Sub-mission created: {result['id']}\nTitle: {result['title']}\nLane: {target}\nStatus: {result['status']}\n{dispatch_note}")]
    return [types.TextContent(type="text", text="Failed to create sub-mission")]


async def _request_review(args: dict) -> list[types.TextContent]:
    if not PROJECT_ID:
        return [types.TextContent(type="text", text="Cannot request review: no project context")]

    prompt = f"""Review the following changes:\n\n{args['description']}"""
    if args.get("files_to_review"):
        prompt += f"\n\nFocus on these files:\n{args['files_to_review']}"

    # Review waits for current mission to complete (needs the code changes first)
    depends_on = [MISSION_ID] if MISSION_ID else []

    result = await _api_post("/api/missions", {
        "project_id": PROJECT_ID,
        "title": f"Review: {args['description'][:60]}",
        "detailed_prompt": prompt,
        "acceptance_criteria": "Code review completed with findings documented",
        "mission_type": "review",
        "priority": 3,
        "tags": ["review"],
        "parent_mission_id": MISSION_ID or None,
        "depends_on": depends_on,
        "auto_dispatch": True,
    })

    if result:
        return [types.TextContent(type="text",
            text=f"Review mission created: {result['id']}\nTitle: {result['title']}\nWill auto-dispatch when your mission completes.")]
    return [types.TextContent(type="text", text="Failed to create review mission")]


async def _get_sub_mission_status(args: dict) -> list[types.TextContent]:
    """Check status of sub-missions created by the current mission."""
    if not MISSION_ID:
        return [types.TextContent(type="text", text="No mission context available")]

    data = await _api_get(f"/api/missions?project_id={PROJECT_ID}")
    if not data:
        return [types.TextContent(type="text", text="No missions found")]

    # Filter to children of current mission
    children = [
        {
            "id": m["id"],
            "title": m["title"],
            "status": m["status"],
            "type": m.get("mission_type", ""),
            "lane": m.get("lane") or "",
        }
        for m in data
        if m.get("parent_mission_id") == MISSION_ID
    ]

    if not children:
        return [types.TextContent(type="text", text="No sub-missions found for this mission")]

    return [types.TextContent(type="text", text=json.dumps(children, indent=2))]


async def _list_project_missions(args: dict) -> list[types.TextContent]:
    if not PROJECT_ID:
        return [types.TextContent(type="text", text="No project context available")]

    path = f"/api/missions?project_id={PROJECT_ID}"
    if args.get("status"):
        path += f"&status={args['status']}"

    data = await _api_get(path)
    if not data:
        return [types.TextContent(type="text", text="No missions found")]

    missions = [
        {"id": m["id"], "title": m["title"], "status": m["status"],
         "type": m.get("mission_type", ""), "lane": m.get("lane") or "",
         "priority": m.get("priority", 0)}
        for m in data[:20]
    ]
    return [types.TextContent(type="text", text=json.dumps(missions, indent=2))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
