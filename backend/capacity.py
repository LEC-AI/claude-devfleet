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
import sqlite3
import uuid
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import db

log = logging.getLogger("devfleet.capacity")

DEFAULT_LIMIT = int(os.environ.get("DEVFLEET_MAX_AGENTS", "3"))

# Columns exposed to callers/API — excludes the internal scope_key generated column
# (see db.py) that exists only to give SQLite a non-null uniqueness target.
_CONFIG_COLUMNS = "id, project_id, day_limit, night_limit, window_id, created_at, updated_at"


class InvalidCapacityConfig(ValueError):
    """Invalid input to set_capacity_config: a negative limit, or project_id=""."""


class ProjectNotFound(LookupError):
    """project_id doesn't reference an existing row in the projects table."""

try:
    from night_window import is_within_window  # Track 2 — used once it lands on this branch
except ImportError:
    def is_within_window(window: dict, now: datetime) -> bool:
        """Local stub matching Track 2's contract
        (backend/night_window.py::is_within_window(window, now) -> bool).

        Pure function: does `window` (start_time/end_time as "HH:MM", possibly
        wrapping past midnight, e.g. 23:00-07:00, interpreted in `window["timezone"]`)
        contain `now`? `now` is converted into the window's own timezone before
        comparing — comparing raw UTC clock time against a local-time window would
        be off by the zone's UTC offset (and wrong for half the year across a DST
        transition like Europe/London).
        """
        tz = ZoneInfo(window.get("timezone") or "UTC")
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        current = now.astimezone(tz).time()
        start = _parse_hhmm(window["start_time"])
        end = _parse_hhmm(window["end_time"])
        if start <= end:
            return start <= current < end
        return current >= start or current < end


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


async def _fetch_config_row(project_id: str | None) -> dict | None:
    conn = await db.get_db()
    try:
        if project_id is not None:
            rows = await conn.execute_fetchall(
                f"SELECT {_CONFIG_COLUMNS} FROM capacity_config WHERE project_id=?", (project_id,)
            )
            if rows:
                return dict(rows[0])
        rows = await conn.execute_fetchall(
            f"SELECT {_CONFIG_COLUMNS} FROM capacity_config WHERE project_id IS NULL"
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
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e):
            raise
        # night_windows table doesn't exist yet on this branch (Track 2 not merged) —
        # treat as "no window configured" rather than erroring. Any other operational
        # error (locked DB, corrupt file, etc.) propagates instead of being swallowed.
        log.debug("night_windows table not found (Track 2 not merged yet) — treating as no window")
        return None
    finally:
        await conn.close()


async def _window_exists(conn, window_id: str) -> bool | None:
    """True/False if night_windows exists and window_id was/wasn't found in it;
    None if night_windows doesn't exist yet (Track 2 not merged) — the caller
    should skip validation rather than reject every window_id."""
    try:
        rows = await conn.execute_fetchall("SELECT 1 FROM night_windows WHERE id=?", (window_id,))
        return bool(rows)
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e):
            raise
        return None


async def get_capacity_config(project_id: str | None) -> dict | None:
    """Return the raw config row (project-specific, else global), or None if neither exists."""
    if project_id == "":
        raise InvalidCapacityConfig('project_id must be omitted/null for the global config, not ""')
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
    """Create or update the config row for a project (or the global default when project_id is None).

    Atomic upsert: two concurrent writers targeting the same scope must converge on a
    single row. A separate select-then-insert-or-update here would race (that was the
    bug: N concurrent first-writes for the same scope each saw "no existing row" and
    each inserted). The single INSERT .. ON CONFLICT DO UPDATE below is one statement,
    and capacity_config's scope_key UNIQUE index (db.py) is what makes SQLite pick a
    single winner row instead of accepting duplicates.
    """
    if project_id == "":
        raise InvalidCapacityConfig('project_id must be omitted/null for the global config, not ""')
    if day_limit < 0 or night_limit < 0:
        raise InvalidCapacityConfig(
            f"day_limit and night_limit must be >= 0 (got day_limit={day_limit}, night_limit={night_limit})"
        )

    conn = await db.get_db()
    try:
        if project_id is not None:
            rows = await conn.execute_fetchall("SELECT 1 FROM projects WHERE id=?", (project_id,))
            if not rows:
                raise ProjectNotFound(project_id)

        if window_id is not None:
            exists = await _window_exists(conn, window_id)
            if exists is False:
                raise InvalidCapacityConfig(f"window_id '{window_id}' does not reference an existing night window")
            # exists is None: night_windows doesn't exist on this branch yet (Track 2
            # not merged) — can't validate, so don't reject every window_id because of it.

        now = datetime.now(timezone.utc).isoformat()
        new_id = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO capacity_config (id, project_id, day_limit, night_limit, window_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_key) DO UPDATE SET
                day_limit=excluded.day_limit,
                night_limit=excluded.night_limit,
                window_id=excluded.window_id,
                updated_at=excluded.updated_at
            """,
            (new_id, project_id, day_limit, night_limit, window_id, now),
        )
        await conn.commit()
    finally:
        await conn.close()

    return await get_capacity_config(project_id)


async def get_limit(project_id: str | None) -> int:
    """Resolve the effective concurrency cap for right now.

    Project-specific config wins over the global default; within whichever
    config applies, night_limit is used if a night window is configured and
    currently active, else day_limit. Falls back to DEFAULT_LIMIT
    (DEVFLEET_MAX_AGENTS, matching today's behavior) when no config row
    exists at all.

    A capacity lookup sits on the dispatch path — a bad window row (malformed
    time string, unknown IANA timezone) must not take dispatch down with it,
    so is_within_window() failures are logged and treated as "not in the
    window" (fail open to day_limit) rather than propagated.
    """
    config = await get_capacity_config(project_id)
    if config is None:
        return DEFAULT_LIMIT

    if config.get("window_id"):
        window = await _fetch_window(config["window_id"])
        if window:
            try:
                if is_within_window(window, datetime.now(timezone.utc)):
                    return config["night_limit"]
            except Exception:
                log.warning(
                    "is_within_window failed for window_id=%r — falling back to day_limit",
                    config["window_id"], exc_info=True,
                )

    return config["day_limit"]


def _running_tasks_dict() -> dict:
    """Mirrors app.py's dispatch-engine selection (DEVFLEET_ENGINE, default
    "sdk", falling back to the CLI dispatcher if claude-code-sdk isn't
    installed) — sdk_engine.running_tasks is the wrong dict to read under
    DEVFLEET_ENGINE=cli, dispatcher.running_tasks is the wrong one under sdk.
    """
    use_sdk = os.environ.get("DEVFLEET_ENGINE", "sdk").lower() == "sdk"
    if use_sdk:
        try:
            from sdk_engine import running_tasks
            return running_tasks
        except ImportError:
            pass
    from dispatcher import running_tasks
    return running_tasks


async def available_slots(project_id: str | None = None) -> int:
    """Drop-in replacement for the duplicated `limit - running` arithmetic in
    autoloop.py / mission_watcher.py; wiring this in for them is an
    integration step, not this track's job. Clamped to >= 0 — a limit lowered
    below the current running count is "no slots", not a negative number.
    """
    running_tasks = _running_tasks_dict()
    limit = await get_limit(project_id)
    running = sum(1 for t in running_tasks.values() if not t.done())
    return max(0, limit - running)
