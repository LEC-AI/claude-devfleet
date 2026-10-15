"""
Mission Watcher — Auto-dispatch engine for sub-missions and dependencies.

Background task that polls for missions marked auto_dispatch=1 whose
dependencies are satisfied, then dispatches them to available agent slots.

This is the core coordination layer for Phase 3 multi-agent teams:
- Agents create sub-missions via MCP tools → watcher auto-dispatches them
- Missions with depends_on wait until all dependencies complete
- Respects MAX_CONCURRENT_AGENTS concurrency limit
- Emits mission_events for observability
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import httpx

import db
from night_window import is_within_window

log = logging.getLogger("devfleet.mission_watcher")

_watcher_task: asyncio.Task | None = None
POLL_INTERVAL = int(os.environ.get("DEVFLEET_WATCHER_INTERVAL", "5"))
MAX_CONCURRENT_AGENTS = int(os.environ.get("DEVFLEET_MAX_AGENTS", "3"))

# Night shift: missions tagged "night" only dispatch inside this window, and ones
# that die on quota/transient errors auto-resume every NIGHT_RESUME_GAP_MIN until
# they finish or hit NIGHT_RESUME_MAX attempts.
NIGHT_WINDOW = {
    "start_time": os.environ.get("DEVFLEET_NIGHT_START", "20:00"),
    "end_time": os.environ.get("DEVFLEET_NIGHT_END", "08:00"),
    "timezone": "Europe/London",
}
NIGHT_RESUME_GAP_MIN = int(os.environ.get("DEVFLEET_NIGHT_RESUME_GAP_MIN", "20"))
NIGHT_RESUME_MAX = int(os.environ.get("DEVFLEET_NIGHT_RESUME_MAX", "30"))
API_URL = os.environ.get("DEVFLEET_API_URL", "http://localhost:18801")
RESUMABLE_ERRORS = ("rate_limit_exhausted", "transient_exhausted", "unknown_early_exit")
NIGHT_TAG_SQL = "EXISTS (SELECT 1 FROM json_each(m.tags) t WHERE t.value = 'night')"


def in_night_window() -> bool:
    return is_within_window(NIGHT_WINDOW, datetime.now(timezone.utc))


async def _find_eligible_missions(limit: int) -> list[dict]:
    """Find auto_dispatch missions whose dependencies are all completed.
    Night-tagged missions are skipped outside the night window."""
    night_ok = 1 if in_night_window() else 0
    conn = await db.get_db()
    try:
        # SQLite json_each lets us check each dependency ID is completed
        rows = await conn.execute_fetchall(
            """SELECT m.*, p.path AS project_path, p.name AS project_name
               FROM missions m
               JOIN projects p ON p.id = m.project_id
               WHERE m.auto_dispatch = 1
                 AND m.status = 'draft'
                 AND NOT EXISTS (
                   SELECT 1 FROM json_each(m.depends_on) dep
                   WHERE dep.value NOT IN (
                     SELECT id FROM missions WHERE status = 'completed'
                   )
                 )
                 AND (NOT """ + NIGHT_TAG_SQL + """ OR (? = 1 AND NOT EXISTS (
                   SELECT 1 FROM missions r WHERE r.project_id = m.project_id
                     AND r.status = 'running'
                     AND EXISTS (SELECT 1 FROM json_each(r.tags) t WHERE t.value = 'night'))))
               ORDER BY m.priority DESC, m.created_at ASC
               LIMIT ?""",
            (night_ok, limit),
        )
        # The running-check above can't see missions picked in this same batch:
        # keep at most one night mission per project per tick.
        picked, night_projects = [], set()
        for r in map(dict, rows):
            if "night" in json.loads(r.get("tags") or "[]"):
                if r["project_id"] in night_projects:
                    continue
                night_projects.add(r["project_id"])
            picked.append(r)
        return picked
    finally:
        await conn.close()


async def _find_resumable_night_missions(limit: int) -> list[str]:
    """Failed night missions whose latest session died on a resumable error at
    least NIGHT_RESUME_GAP_MIN ago and that are under the auto-resume cap."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT m.id FROM missions m
               JOIN agent_sessions s ON s.id = (
                 SELECT id FROM agent_sessions WHERE mission_id = m.id
                 ORDER BY started_at DESC LIMIT 1)
               WHERE m.status = 'failed' AND """ + NIGHT_TAG_SQL + """
                 AND NOT EXISTS (SELECT 1 FROM missions r WHERE r.project_id = m.project_id
                   AND r.status = 'running'
                   AND EXISTS (SELECT 1 FROM json_each(r.tags) t WHERE t.value = 'night'))
                 AND s.error_type IN (?, ?, ?)
                 AND s.claude_session_id IS NOT NULL AND s.claude_session_id != ''
                 AND datetime(s.ended_at) <= datetime('now', ?)
                 AND (SELECT COUNT(*) FROM mission_events e
                      WHERE e.mission_id = m.id AND e.event_type = 'night_auto_resumed') < ?
                 AND NOT EXISTS (SELECT 1 FROM mission_events e
                      WHERE e.mission_id = m.id AND e.event_type = 'night_auto_resumed'
                        AND datetime(e.created_at) > datetime('now', ?))
               GROUP BY m.project_id
               ORDER BY m.priority DESC, m.created_at ASC
               LIMIT ?""",
            (*RESUMABLE_ERRORS, f"-{NIGHT_RESUME_GAP_MIN} minutes", NIGHT_RESUME_MAX,
             f"-{NIGHT_RESUME_GAP_MIN} minutes", limit),
        )
        return [r["id"] for r in rows]
    finally:
        await conn.close()


async def _auto_resume_night(slots: int) -> int:
    """Resume quota-killed night missions through the API's own resume route."""
    if slots <= 0 or not in_night_window():
        return 0
    resumed = 0
    async with httpx.AsyncClient(timeout=15) as client:
        for mid in await _find_resumable_night_missions(slots):
            try:
                r = await client.post(f"{API_URL}/api/missions/{mid}/resume", json={})
                code = r.status_code
            except httpx.HTTPError as e:
                code = str(e)[:100]
            await _emit_event(mid, "night_auto_resumed", data={"http": code})
            log.info("Night auto-resume %s -> %s", mid, code)
            resumed += code == 200
    return resumed


async def _emit_event(mission_id: str, event_type: str, source_mission_id: str | None = None, data: dict | None = None):
    """Record a mission event for observability."""
    conn = await db.get_db()
    try:
        await conn.execute(
            "INSERT INTO mission_events (mission_id, event_type, source_mission_id, data) VALUES (?, ?, ?, ?)",
            (mission_id, event_type, source_mission_id, json.dumps(data or {})),
        )
        await conn.commit()
    except Exception as e:
        log.warning("Failed to emit event %s for %s: %s", event_type, mission_id, e)
    finally:
        await conn.close()


async def _dispatch_eligible(mission: dict):
    """Dispatch a single eligible mission."""
    # Import here to avoid circular imports
    from sdk_engine import dispatch_mission, running_tasks
    from prompt_template import build_prompt

    mission_id = mission["id"]
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Get last report for context (from parent or previous runs)
    conn = await db.get_db()
    try:
        # Check for report from parent mission first
        parent_id = mission.get("parent_mission_id")
        if parent_id:
            rows = await conn.execute_fetchall(
                "SELECT * FROM reports WHERE mission_id=? ORDER BY created_at DESC LIMIT 1",
                (parent_id,),
            )
        else:
            rows = await conn.execute_fetchall(
                "SELECT * FROM reports WHERE mission_id=? ORDER BY created_at DESC LIMIT 1",
                (mission_id,),
            )
        last_report = dict(rows[0]) if rows else None

        # Create session
        model = mission.get("model") or "claude-opus-4-6"
        await conn.execute(
            "INSERT INTO agent_sessions (id, mission_id, model) VALUES (?, ?, ?)",
            (session_id, mission_id, model),
        )
        await conn.execute(
            "UPDATE missions SET status='running', updated_at=? WHERE id=?",
            (now, mission_id),
        )
        await conn.commit()
    finally:
        await conn.close()

    await _emit_event(mission_id, "auto_dispatched", data={"session_id": session_id})

    log.info("Auto-dispatching mission '%s' (session %s)", mission["title"], session_id)

    task = asyncio.create_task(dispatch_mission(session_id, mission, last_report))
    running_tasks[session_id] = task


async def _watch_loop():
    """Main polling loop — find and dispatch eligible missions."""
    log.info("Mission watcher started (poll every %ds)", POLL_INTERVAL)

    while True:
        try:
            # Import here to get current state
            from sdk_engine import running_tasks

            running = sum(1 for t in running_tasks.values() if not t.done())
            slots = MAX_CONCURRENT_AGENTS - running

            slots -= await _auto_resume_night(slots)

            if slots > 0:
                eligible = await _find_eligible_missions(limit=slots)
                for mission in eligible:
                    try:
                        await _dispatch_eligible(mission)
                    except Exception as e:
                        log.error("Failed to auto-dispatch mission %s: %s", mission["id"], e)
                        await _emit_event(mission["id"], "dispatch_failed", data={"error": str(e)})

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Mission watcher error: %s", e)

        await asyncio.sleep(POLL_INTERVAL)


async def start_watcher():
    """Start the mission watcher background task."""
    global _watcher_task
    if _watcher_task and not _watcher_task.done():
        return
    _watcher_task = asyncio.create_task(_watch_loop())
    log.info("Mission watcher started")


async def stop_watcher():
    """Stop the mission watcher."""
    global _watcher_task
    if _watcher_task and not _watcher_task.done():
        _watcher_task.cancel()
        try:
            await _watcher_task
        except asyncio.CancelledError:
            pass
    _watcher_task = None
    log.info("Mission watcher stopped")


def get_watcher_status() -> dict:
    """Get the watcher status."""
    return {
        "active": _watcher_task is not None and not _watcher_task.done(),
        "poll_interval": POLL_INTERVAL,
    }
