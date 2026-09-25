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
end is ambiguous (empty or 24 h?) and is rejected as invalid: the API returns
422, and a stored row like that fails open.

Format rule: times must be exactly HH:MM with ASCII digits ("07:05", not
"7:5", "+7:00" or non-ASCII digits). Timezone names are matched
case-insensitively and stored in canonical form ("utc" -> "UTC"), so
behaviour is identical on Windows, macOS and Linux.

Timezone rule: `now` is converted to the window's timezone before
comparison. A naive `now` is treated as UTC, matching the rest of the
codebase which uses datetime.now(timezone.utc).
"""

import logging
import re
from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

import db

logger = logging.getLogger("devfleet.night_window")

DEFAULT_TIMEZONE = "Europe/London"

# [0-9], not \d: \d also matches non-ASCII digits such as "٠٧".
_HHMM = re.compile(r"[0-9]{2}:[0-9]{2}")


def _parse_hhmm(value: str) -> int:
    """Parse strict 'HH:MM' into minutes since midnight. Raises ValueError if malformed."""
    if not isinstance(value, str) or not _HHMM.fullmatch(value):
        raise ValueError(f"time must be HH:MM (e.g. 07:05), got {value!r}")
    hours, minutes = int(value[:2]), int(value[3:])
    if hours > 23 or minutes > 59:
        raise ValueError(f"time out of range: {value!r}")
    return hours * 60 + minutes


@lru_cache(maxsize=1)
def _timezones_by_lower() -> dict[str, str]:
    return {name.lower(): name for name in available_timezones()}


def canonical_timezone(name: str | None) -> str:
    """Return the canonical IANA spelling ("utc" -> "UTC"). Raises ValueError if unknown."""
    if name is None:
        return DEFAULT_TIMEZONE
    if not isinstance(name, str):
        raise ValueError(f"timezone must be a string, got {type(name).__name__}")
    canonical = _timezones_by_lower().get(name.lower())
    if canonical is None:
        raise ValueError(f"unknown timezone: {name!r}")
    return canonical


def is_within_window(window: dict, now: datetime) -> bool:
    """
    Pure function: is `now` inside the window?

    window: {"start_time": "HH:MM", "end_time": "HH:MM",
             "timezone": "Europe/London" (optional), "enabled": 1 (optional)}
    now:    aware or naive datetime. Naive is treated as UTC.

    Handles wrap-past-midnight (e.g. 23:00–07:00). Raises ValueError on a
    malformed time string, start == end, or unknown timezone — callers that
    must fail open (is_project_in_window) catch this; tests can assert on it.
    """
    if "enabled" in window and not window["enabled"]:
        return False

    start = _parse_hhmm(window["start_time"])
    end = _parse_hhmm(window["end_time"])
    if start == end:
        raise ValueError(f"start_time and end_time must differ, both are {window['start_time']!r}")

    tz = ZoneInfo(canonical_timezone(window.get("timezone") or DEFAULT_TIMEZONE))

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(tz)
    current = local.hour * 60 + local.minute

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
