"""
Lane manager — scheduling dimension for the DevFleet agent fleet.

Lanes are logical slot budgets (coder/reviewer/tester/planner/explorer).
Each lane has an independent concurrency cap, default model, tool preset,
and system prompt addendum. mission_type stays as the tool-preset label;
lane controls how many agents of each role run in parallel.
"""

import asyncio
import logging
from typing import Optional

import db
from models import LANE_DEFAULTS, MISSION_TYPE_TO_LANE

log = logging.getLogger("devfleet.lanes")

_cache: dict[str, dict] = {}


async def reload_cache() -> None:
    """Reload lane policies from the DB into the in-memory cache."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM lanes WHERE enabled = 1")
        _cache.clear()
        for row in rows:
            _cache[row["name"]] = dict(row)
        log.info("Lane cache reloaded: %s", list(_cache.keys()))
    finally:
        await conn.close()


async def get_lane(name: str) -> dict:
    """Return lane policy dict; falls back to 'coder' defaults if unknown."""
    if not _cache:
        await reload_cache()
    if name in _cache:
        return _cache[name]
    # Fallback: reconstruct from LANE_DEFAULTS without DB
    fallback = LANE_DEFAULTS.get(name) or LANE_DEFAULTS["coder"]
    return {"name": name or "coder", **fallback}


def derive_lane(mission: dict) -> str:
    """Resolve the effective lane name for a mission.

    Precedence: explicit mission.lane > MISSION_TYPE_TO_LANE[mission_type] > 'coder'
    """
    lane = mission.get("lane")
    if lane and lane.strip():
        return lane.strip()
    mission_type = mission.get("mission_type", "implement")
    return MISSION_TYPE_TO_LANE.get(mission_type, "coder")


def running_by_lane() -> dict[str, int]:
    """Return count of currently-running tasks per lane.

    Reads the lane attribute set on asyncio.Task objects at dispatch time.
    Deferred import of running_tasks avoids circular import with sdk_engine.
    """
    try:
        from sdk_engine import running_tasks
    except ImportError:
        return {}

    counts: dict[str, int] = {}
    for task in running_tasks.values():
        if task.done():
            continue
        lane_name = getattr(task, "lane", "coder")
        counts[lane_name] = counts.get(lane_name, 0) + 1
    return counts


async def check_slot(mission: dict) -> tuple[bool, str]:
    """Return (ok, reason). ok=False means the lane is at capacity."""
    lane_name = derive_lane(mission)
    lane = await get_lane(lane_name)
    cap: int = lane.get("max_agents", 1)
    running = running_by_lane().get(lane_name, 0)
    if running >= cap:
        return False, f"{lane_name.capitalize()} lane full ({running}/{cap}) — wait for a slot or switch lanes"
    return True, ""


async def free_slots() -> dict[str, int]:
    """Return {lane_name: free_slots} for all enabled lanes with capacity > 0."""
    if not _cache:
        await reload_cache()
    running = running_by_lane()
    result: dict[str, int] = {}
    for name, policy in _cache.items():
        cap: int = policy.get("max_agents", 1)
        free = cap - running.get(name, 0)
        if free > 0:
            result[name] = free
    return result


def total_capacity() -> int:
    """Return the sum of all lane caps — used as the global MAX_CONCURRENT_AGENTS."""
    if not _cache:
        # Compute from LANE_DEFAULTS before cache is warm (startup)
        return sum(p["max_agents"] for p in LANE_DEFAULTS.values())
    return sum(p.get("max_agents", 1) for p in _cache.values())


async def snapshot() -> list[dict]:
    """Return a list of lane dicts with live running/free counts for the status endpoint."""
    if not _cache:
        await reload_cache()
    running = running_by_lane()
    result = []
    for name, policy in _cache.items():
        cap: int = policy.get("max_agents", 1)
        r = running.get(name, 0)
        result.append({
            "name": name,
            "icon": policy.get("icon", ""),
            "color": policy.get("color", "#888"),
            "max_agents": cap,
            "running": r,
            "free": cap - r,
            "default_model": policy.get("default_model", "claude-sonnet-4-6"),
            "tool_preset": policy.get("tool_preset", "implement"),
            "enabled": bool(policy.get("enabled", 1)),
        })
    return sorted(result, key=lambda x: list(LANE_DEFAULTS.keys()).index(x["name"])
                  if x["name"] in LANE_DEFAULTS else 99)


async def get_all_lanes() -> list[dict]:
    """Return all lanes from DB (including disabled) for the CRUD endpoints."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM lanes ORDER BY created_at")
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def get_one_lane(name: str) -> Optional[dict]:
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM lanes WHERE name = ?", (name,))
        return dict(rows[0]) if rows else None
    finally:
        await conn.close()


async def update_lane(name: str, patch: dict) -> Optional[dict]:
    """Apply a partial update to a lane and reload cache."""
    conn = await db.get_db()
    try:
        # Build SET clause from non-None fields
        fields = {k: v for k, v in patch.items() if v is not None}
        if not fields:
            rows = await conn.execute_fetchall("SELECT * FROM lanes WHERE name = ?", (name,))
            return dict(rows[0]) if rows else None
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [name]
        await conn.execute(f"UPDATE lanes SET {set_clause} WHERE name = ?", values)
        await conn.commit()
        rows = await conn.execute_fetchall("SELECT * FROM lanes WHERE name = ?", (name,))
        result = dict(rows[0]) if rows else None
    finally:
        await conn.close()
    await reload_cache()
    return result
