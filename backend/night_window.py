"""
Night-window scheduling — Track 2.

Gates dispatch to a per-project time window (e.g. 23:00–07:00 Europe/London)
so unattended loops can use an overnight usage allowance without running
into the working day.

Public contract
---------------
    is_project_in_window(project_id) -> bool

That is the ONE function other modules gate dispatch on. Integration (not
done in this module) adds a guard at the top of
mission_watcher._dispatch_eligible and inside autoloop.auto_loop.

Semantics
---------
* No row in night_windows for the project  -> unrestricted (True).
* Row exists but enabled = 0               -> unrestricted (True).
* Row exists and enabled = 1               -> is_within_window(...).
* Any error (bad timezone, malformed time) -> True + logged warning.
  This module must never be the reason dispatch silently stops.

Boundary rule: start-inclusive, end-exclusive. For 23:00–07:00, 23:00 is
inside, 06:59 is inside, 07:00 is outside. A window whose start equals its
end is empty (always False).

Timezone rule: `now` is converted to the window's timezone before
comparison. A naive `now` is treated as UTC, matching the rest of the
codebase which uses datetime.now(timezone.utc).
"""

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import db

logger = logging.getLogger("devfleet.night_window")

DEFAULT_TIMEZONE = "Europe/London"


def _parse_hhmm(value: str) -> int:
    """Parse 'HH:MM' into minutes since midnight. Raises ValueError if malformed."""
    if not isinstance(value, str):
        raise ValueError(f"time must be a string, got {type(value).__name__}")
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"time must be HH:MM, got {value!r}")
    hours, minutes = int(parts[0]), int(parts[1])
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError(f"time out of range: {value!r}")
    return hours * 60 + minutes


def is_within_window(window: dict, now: datetime) -> bool:
    """
    Pure function: is `now` inside the window?

    window: {"start_time": "HH:MM", "end_time": "HH:MM",
             "timezone": "Europe/London" (optional), "enabled": 1 (optional)}
    now:    aware or naive datetime. Naive is treated as UTC.

    Handles wrap-past-midnight (e.g. 23:00–07:00). Raises ValueError on a
    malformed time string or unknown timezone — callers that must fail open
    (is_project_in_window) catch this; tests can assert on it.
    """
    if "enabled" in window and not window["enabled"]:
        return False

    start = _parse_hhmm(window["start_time"])
    end = _parse_hhmm(window["end_time"])

    tz_name = window.get("timezone") or DEFAULT_TIMEZONE
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:  # ZoneInfoNotFoundError, or bad key type
        raise ValueError(f"unknown timezone: {tz_name!r}") from exc

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(tz)
    current = local.hour * 60 + local.minute

    if start == end:
        return False  # empty window
    if start < end:
        return start <= current < end  # same-day range, e.g. 09:00–17:00
    return current >= start or current < end  # wraps midnight, e.g. 23:00–07:00


async def get_active_window(project_id: str) -> dict | None:
    """Return the enabled night_windows row for the project, or None."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT * FROM night_windows WHERE project_id=? AND enabled=1",
            (project_id,),
        )
        return dict(rows[0]) if rows else None
    finally:
        await conn.close()


async def is_project_in_window(project_id: str) -> bool:
    """
    The dispatch gate. True when the project may dispatch right now.

    Unrestricted (True) when no enabled window is configured. Fails open on
    any error so a bad row can never block the fleet.
    """
    try:
        window = await get_active_window(project_id)
    except Exception as exc:
        logger.warning("night_window: lookup failed for project %s (%s); allowing dispatch",
                       project_id, exc)
        return True

    if window is None:
        return True

    try:
        return is_within_window(window, datetime.now(timezone.utc))
    except Exception as exc:
        logger.warning("night_window: invalid window for project %s (%s); allowing dispatch",
                       project_id, exc)
        return True
