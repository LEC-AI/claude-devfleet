"""
Capacity Manager — Track 4 of the DevFleet Swarm & Nightly Goal-Loop spec.

Centralizes the concurrency-cap resolution that's today duplicated in
autoloop.py and mission_watcher.py as
`sum(1 for t in running_tasks.values() if not t.done())` against a fixed
MAX_CONCURRENT_AGENTS. Owns the capacity_config table: a per-project (or
global, project_id IS NULL) pair of day_limit/night_limit, plus an optional
reference to a Track 2 night_windows row.

Standalone by design: Track 2 (backend/night_window.py) doesn't exist on
this branch yet, so `is_within_window` is imported from it when available
and a local stub with the identical signature is used otherwise. The stub
is swapped out automatically the moment night_window.py lands — no code
change needed here.

This module does not modify autoloop.py or mission_watcher.py. Wiring
`available_slots()` into those two files' dispatch loops is an integration
step, not part of this track (see spec's "Contract for other tracks").
"""

import logging
import os
import uuid
from datetime import datetime, time, timezone

import db

log = logging.getLogger("devfleet.capacity")

DEFAULT_LIMIT = int(os.environ.get("DEVFLEET_MAX_AGENTS", "3"))

try:
    from night_window import is_within_window  # Track 2 — used once it lands on this branch
except ImportError:
    def is_within_window(window: dict, now: datetime) -> bool:
        """Local stub matching Track 2's contract
        (backend/night_window.py::is_within_window(window, now) -> bool).

        Pure function: does `window` (start_time/end_time as "HH:MM", possibly
        wrapping past midnight, e.g. 23:00-07:00) contain `now`?
        """
        start = _parse_hhmm(window["start_time"])
        end = _parse_hhmm(window["end_time"])
        current = now.time()
        if start <= end:
            return start <= current < end
        return current >= start or current < end


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


async def _fetch_config_row(project_id: str | None) -> dict | None:
    conn = await db.get_db()
    try:
        if project_id:
            rows = await conn.execute_fetchall(
                "SELECT * FROM capacity_config WHERE project_id=?", (project_id,)
            )
            if rows:
                return dict(rows[0])
        rows = await conn.execute_fetchall(
            "SELECT * FROM capacity_config WHERE project_id IS NULL"
        )
        return dict(rows[0]) if rows else None
    finally:
        await conn.close()


async def _fetch_window(window_id: str) -> dict | None:
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT * FROM night_windows WHERE id=?", (window_id,)
        )
        return dict(rows[0]) if rows else None
    except Exception:
        # night_windows table doesn't exist yet on this branch (Track 2 not merged) —
        # treat as "no window configured" rather than erroring.
        log.debug("night_windows lookup failed (Track 2 not merged yet?) — treating as no window")
        return None
    finally:
        await conn.close()


async def get_capacity_config(project_id: str | None) -> dict | None:
    """Return the raw config row (project-specific, else global), or None if neither exists."""
    config = await _fetch_config_row(project_id)
    if config is None and project_id is not None:
        config = await _fetch_config_row(None)
    return config


async def set_capacity_config(
    project_id: str | None,
    day_limit: int,
    night_limit: int,
    window_id: str | None = None,
) -> dict:
    """Create or update the config row for a project (or the global default when project_id is None)."""
    conn = await db.get_db()
    try:
        if project_id:
            existing = await conn.execute_fetchall(
                "SELECT id FROM capacity_config WHERE project_id=?", (project_id,)
            )
        else:
            existing = await conn.execute_fetchall(
                "SELECT id FROM capacity_config WHERE project_id IS NULL"
            )
        now = datetime.now(timezone.utc).isoformat()
        if existing:
            config_id = existing[0]["id"]
            await conn.execute(
                "UPDATE capacity_config SET day_limit=?, night_limit=?, window_id=?, updated_at=? WHERE id=?",
                (day_limit, night_limit, window_id, now, config_id),
            )
        else:
            config_id = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO capacity_config (id, project_id, day_limit, night_limit, window_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (config_id, project_id, day_limit, night_limit, window_id, now),
            )
        await conn.commit()
        return {
            "id": config_id,
            "project_id": project_id,
            "day_limit": day_limit,
            "night_limit": night_limit,
            "window_id": window_id,
        }
    finally:
        await conn.close()


async def get_limit(project_id: str | None) -> int:
    """Resolve the effective concurrency cap for right now.

    Project-specific config wins over the global default; within whichever
    config applies, night_limit is used if a night window is configured and
    currently active, else day_limit. Falls back to DEFAULT_LIMIT
    (DEVFLEET_MAX_AGENTS, matching today's behavior) when no config row
    exists at all.
    """
    config = await get_capacity_config(project_id)
    if config is None:
        return DEFAULT_LIMIT

    if config.get("window_id"):
        window = await _fetch_window(config["window_id"])
        if window and is_within_window(window, datetime.now(timezone.utc)):
            return config["night_limit"]

    return config["day_limit"]


async def available_slots(project_id: str | None = None) -> int:
    """Drop-in replacement for the duplicated `limit - running` arithmetic in
    autoloop.py / mission_watcher.py. Reads running_tasks from sdk_engine
    exactly as those two files do today; wiring this in for them is an
    integration step, not this track's job.
    """
    from sdk_engine import running_tasks

    limit = await get_limit(project_id)
    running = sum(1 for t in running_tasks.values() if not t.done())
    return limit - running
