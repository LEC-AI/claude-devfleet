"""
Swarm observability — read-only tree endpoint.

Given a swarm root mission id (a mission tagged ``swarm_root`` — the swarm planner's
convention), return the root plus every descendant mission, each annotated with
status, dependencies, what it is still blocked on, and the cost/tokens of its
latest agent session. Everything here is a plain SELECT over the existing
``missions`` and ``agent_sessions`` tables — no new schema, no writes, and no
imports from the dispatch/loop modules.

Integration (one line in app.py):

    import routes_swarm_tree
    app.include_router(routes_swarm_tree.router)

Endpoints:
    GET /api/swarms                 — list swarm roots (missions tagged swarm_root)
    GET /api/swarms/{id}/tree       — root + full descendant tree with roll-up
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

import db

router = APIRouter(prefix="/api", tags=["swarms"])

SWARM_ROOT_TAG = "swarm_root"
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
BASE_STATUSES = ("draft", "running", "completed", "failed", "cancelled")
MAX_DEPTH = 20


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _parse_json_list(raw) -> list:
    """Parse a JSON-array column defensively. NULL / '' / garbage → []."""
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return val if isinstance(val, list) else []


def _session_dict(row) -> dict | None:
    if row is None or row.get("session_id") is None:
        return None
    return {
        "id": row["session_id"],
        "status": row.get("session_status"),
        "model": row.get("session_model"),
        "started_at": row.get("session_started_at"),
        "ended_at": row.get("session_ended_at"),
        "total_cost_usd": float(row.get("session_cost") or 0),
        "total_tokens": int(row.get("session_tokens") or 0),
    }


def _node(row: dict, depth: int, completed_ids: set) -> dict:
    depends_on = _parse_json_list(row.get("depends_on"))
    status = row.get("status")
    blocked_on = [d for d in depends_on if d not in completed_ids] if status == "draft" else []
    return {
        "id": row["id"],
        "title": row.get("title"),
        "status": status,
        "mission_type": row.get("mission_type"),
        "priority": row.get("priority"),
        "parent_mission_id": row.get("parent_mission_id"),
        "depth": depth,
        "depends_on": depends_on,
        "blocked_on": blocked_on,
        "auto_dispatch": int(row.get("auto_dispatch") or 0),
        "tags": _parse_json_list(row.get("tags")),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "latest_session": _session_dict(row),
    }


def _summarize(children: list[dict]) -> dict:
    counts = {s: 0 for s in BASE_STATUSES}
    cost = 0.0
    tokens = 0
    blocked = 0
    for c in children:
        counts[c["status"] or "unknown"] = counts.get(c["status"] or "unknown", 0) + 1
        sess = c.get("latest_session")
        if sess:
            cost += sess["total_cost_usd"]
            tokens += sess["total_tokens"]
        if c["blocked_on"]:
            blocked += 1
    return {
        "total": len(children),
        "counts": counts,
        "blocked": blocked,
        "total_cost_usd": round(cost, 6),
        "total_tokens": tokens,
        "is_active": any((c["status"] not in TERMINAL_STATUSES) for c in children),
    }


# Latest session per mission: the most recently started row (rowid breaks ties).
_LATEST_SESSION_JOIN = """
    LEFT JOIN agent_sessions s
        ON s.id = (
            SELECT id FROM agent_sessions
            WHERE mission_id = m.id
            ORDER BY started_at DESC, rowid DESC
            LIMIT 1
        )
"""

_NODE_COLUMNS = """
    m.id, m.project_id, m.title, m.status, m.mission_type, m.priority,
    m.parent_mission_id, m.depends_on, m.auto_dispatch, m.tags,
    m.created_at, m.updated_at,
    s.id            AS session_id,
    s.status        AS session_status,
    s.model         AS session_model,
    s.started_at    AS session_started_at,
    s.ended_at      AS session_ended_at,
    s.total_cost_usd AS session_cost,
    s.total_tokens  AS session_tokens
"""


async def _fetch_subtree(conn, root_id: str) -> tuple[list[dict], bool]:
    """All descendants of root_id (root excluded), ordered depth → created_at → id.

    Returns (rows, truncated) where truncated is True if MAX_DEPTH was hit.
    """
    rows = await conn.execute_fetchall(
        f"""
        WITH RECURSIVE tree(id, depth) AS (
            SELECT id, 1 FROM missions WHERE parent_mission_id = ?
            UNION ALL
            SELECT c.id, t.depth + 1
            FROM missions c JOIN tree t ON c.parent_mission_id = t.id
            WHERE t.depth < ?
        )
        SELECT t.depth, {_NODE_COLUMNS}
        FROM tree t
        JOIN missions m ON m.id = t.id
        {_LATEST_SESSION_JOIN}
        ORDER BY t.depth, m.created_at, m.id
        """,
        (root_id, MAX_DEPTH + 1),
    )
    rows = [dict(r) for r in rows]
    truncated = any(r["depth"] > MAX_DEPTH for r in rows)
    rows = [r for r in rows if r["depth"] <= MAX_DEPTH]
    return rows, truncated


async def _fetch_mission(conn, mission_id: str) -> dict | None:
    rows = await conn.execute_fetchall(
        f"""
        SELECT {_NODE_COLUMNS}
        FROM missions m
        {_LATEST_SESSION_JOIN}
        WHERE m.id = ?
        """,
        (mission_id,),
    )
    return dict(rows[0]) if rows else None


async def _completed_ids(conn, ids: set[str]) -> set[str]:
    """Which of the given mission ids are completed (deps may point outside the tree)."""
    if not ids:
        return set()
    placeholders = ",".join("?" for _ in ids)
    rows = await conn.execute_fetchall(
        f"SELECT id FROM missions WHERE status = 'completed' AND id IN ({placeholders})",
        tuple(ids),
    )
    return {r["id"] for r in rows}


async def build_tree(root_id: str) -> dict:
    """Core logic, callable without HTTP (used by the test file)."""
    conn = await db.get_db()
    try:
        root_row = await _fetch_mission(conn, root_id)
        if root_row is None:
            raise HTTPException(404, "Mission not found")

        child_rows, truncated = await _fetch_subtree(conn, root_id)

        dep_ids: set[str] = set()
        for r in child_rows:
            if r.get("status") == "draft":
                dep_ids.update(_parse_json_list(r.get("depends_on")))
        completed = {r["id"] for r in child_rows if r.get("status") == "completed"}
        completed |= await _completed_ids(conn, dep_ids - completed)

        root = _node(root_row, 0, completed)
        children = [_node(r, r["depth"], completed) for r in child_rows]

        return {
            "root": root,
            "is_swarm_root": SWARM_ROOT_TAG in root["tags"],
            "missions": children,
            "summary": _summarize(children),
            "truncated": truncated,
            "max_depth": MAX_DEPTH,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        await conn.close()


async def list_swarms(project_id: str | None = None) -> list[dict]:
    """All missions tagged swarm_root (newest first), each with a roll-up."""
    conn = await db.get_db()
    try:
        query = f"""
            SELECT {_NODE_COLUMNS}, p.name AS project_name
            FROM missions m
            JOIN projects p ON p.id = m.project_id
            {_LATEST_SESSION_JOIN}
            WHERE json_valid(m.tags)
              AND EXISTS (SELECT 1 FROM json_each(m.tags) WHERE json_each.value = ?)
        """
        params: list = [SWARM_ROOT_TAG]
        if project_id:
            query += " AND m.project_id = ?"
            params.append(project_id)
        query += " ORDER BY m.created_at DESC, m.id DESC"
        rows = [dict(r) for r in await conn.execute_fetchall(query, params)]
    finally:
        await conn.close()

    results = []
    for r in rows:
        tree = await build_tree(r["id"])
        results.append({
            **tree["root"],
            "project_id": r["project_id"],
            "project_name": r["project_name"],
            "summary": tree["summary"],
        })
    return results


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────

@router.get("/swarms")
async def api_list_swarms(project_id: str = Query(None)):
    """List swarm roots (missions tagged ``swarm_root``) with a per-swarm status roll-up."""
    return await list_swarms(project_id)


@router.get("/swarms/{swarm_id}/tree")
async def api_swarm_tree(swarm_id: str):
    """Root + every descendant mission, annotated with status, deps, blocked_on and latest cost."""
    return await build_tree(swarm_id)
