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

import db

log = logging.getLogger("devfleet.mission_watcher")

_watcher_task: asyncio.Task | None = None
POLL_INTERVAL = int(os.environ.get("DEVFLEET_WATCHER_INTERVAL", "5"))
MAX_CONCURRENT_AGENTS = int(os.environ.get("DEVFLEET_MAX_AGENTS", "3"))


async def _find_eligible_missions(lane_capacity: dict[str, int]) -> list[dict]:
    """Find auto_dispatch missions whose dependencies are all completed and whose target lane has capacity."""
    from lanes import derive_lane
    conn = await db.get_db()
    try:
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
               ORDER BY m.priority DESC, m.created_at ASC
               LIMIT 50""",
        )
        # Post-filter: only return missions whose target lane has free slots
        eligible = []
        for row in rows:
            m = dict(row)
            target_lane = derive_lane(m)
            if lane_capacity.get(target_lane, 0) > 0:
                eligible.append(m)
                # Decrement optimistically so we don't dispatch two missions to the same full lane
                lane_capacity[target_lane] -= 1
        return eligible
    finally:
        await conn.close()


async def _emit_event(mission_id: str, event_type: str, source_mission_id: str | None = None, data: dict | None = None, failure_layer: str | None = None):
    """Record a mission event for observability."""
    conn = await db.get_db()
    try:
        await conn.execute(
            "INSERT INTO mission_events (mission_id, event_type, source_mission_id, data, failure_layer) VALUES (?, ?, ?, ?, ?)",
            (mission_id, event_type, source_mission_id, json.dumps(data or {}), failure_layer),
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
        model = mission.get("model") or "claude-sonnet-4-6"
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


async def _reap_stuck_sessions():
    """Find sessions silent for > STUCK_THRESHOLD and cancel their tasks."""
    stuck_threshold_minutes = int(os.environ.get("DEVFLEET_STUCK_THRESHOLD_MINUTES", "20"))
    conn = await db.get_db()
    try:
        stuck = await conn.execute_fetchall(
            """SELECT s.id, s.mission_id FROM agent_sessions s
               WHERE s.status = 'running'
                 AND (
                   s.last_activity_at IS NULL
                   OR s.last_activity_at < datetime('now', ? || ' minutes')
                 )
                 AND s.started_at < datetime('now', '-5 minutes')""",
            (f"-{stuck_threshold_minutes}",),
        )
    finally:
        await conn.close()

    if not stuck:
        return

    from sdk_engine import running_tasks
    for row in stuck:
        session_id = row["id"]
        task = running_tasks.get(session_id)
        if task and not task.done():
            log.warning(
                "Session %s has been silent for >%d min — cancelling as stuck",
                session_id, stuck_threshold_minutes,
            )
            task.cancel()
        elif not task:
            # No task running but DB says running — orphaned session, mark failed
            conn2 = await db.get_db()
            try:
                now = datetime.now(timezone.utc).isoformat()
                await conn2.execute(
                    "UPDATE agent_sessions SET status='failed', ended_at=? WHERE id=?",
                    (now, session_id),
                )
                await conn2.execute(
                    "UPDATE missions SET status='failed', updated_at=? WHERE id=?",
                    (now, row["mission_id"]),
                )
                await conn2.commit()
                log.warning("Cleaned up orphaned session %s (no task, DB said running)", session_id)
            finally:
                await conn2.close()


async def _watch_loop():
    """Main polling loop — find and dispatch eligible missions, reap stuck sessions."""
    log.info("Mission watcher started (poll every %ds)", POLL_INTERVAL)

    while True:
        try:
            # Import here to get current state
            from sdk_engine import running_tasks
            from lanes import free_slots as lane_free_slots

            # Reap sessions that have gone silent
            await _reap_stuck_sessions()

            # Global ceiling check (safety override)
            running = sum(1 for t in running_tasks.values() if not t.done())
            if running >= MAX_CONCURRENT_AGENTS:
                pass  # global cap hit — skip dispatch this cycle
            else:
                # Per-lane capacity — only dispatch to lanes with free slots
                lane_capacity = await lane_free_slots()
                if lane_capacity:
                    eligible = await _find_eligible_missions(lane_capacity)
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
