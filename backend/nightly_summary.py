"""
Nightly Summary — one rollup of what ran overnight, what it cost, what it produced.

Pure aggregation over existing tables (agent_sessions, reports, missions) for a
time window. Each run is stored as a row in `nightly_runs`, so there's a single
place to look after a night of unattended agent work instead of piecing it
together from sessions and reports by hand. It summarizes by time window only,
regardless of whether missions came from a swarm, the auto-loop, the scheduler
or a manual dispatch.

Window semantics:
    Half-open [since, until), UTC. A session belongs to the window its
    `ended_at` falls in; a report belongs to the window its `created_at` falls
    in. Timestamps are compared through SQLite's datetime() because
    agent_sessions.ended_at holds both 'YYYY-MM-DD HH:MM:SS' (SQLite) and
    'YYYY-MM-DDTHH:MM:SS.ffffff+00:00' (Python isoformat) values, which don't
    compare correctly as strings.

Counting rules:
    - total_cost_usd / total_tokens: summed over every session that ended in
      the window, whatever its status (failed and cancelled runs cost money too).
    - missions_completed / missions_failed: a mission's outcome is the status of
      its latest session in the window, so a mission that failed then succeeded
      on retry the same night counts once, as completed. Cancelled/takeover
      outcomes are reported as `missions_other` in the returned dict.

Integration (the integrator's job, not done in this module):
    app.py        import nightly_summary
                  app.include_router(nightly_summary.router)
    db.py         optional — paste NIGHTLY_RUNS_SCHEMA into SCHEMA. Not required:
                  every public function here creates the table if it's missing.
    scheduler.py  in _scheduler_loop, after `await _check_schedules()`:
                      await nightly_summary.maybe_run_nightly_summary()
                  (idempotent, safe every tick) — or, from a night-window-close
                  event: await maybe_run_nightly_summary(since=start, until=end)

Env vars:
    DEVFLEET_NIGHTLY_WINDOW_START  UTC HH:MM, default 22:00
    DEVFLEET_NIGHTLY_WINDOW_END    UTC HH:MM, default 06:00

Known limitations:
    - A resumed session reuses its row and accumulates cost, so a session that
      ended in an earlier window and was resumed in this one contributes its
      full cumulative cost here.
    - Sessions with no ended_at (orphaned 'running' rows, or cancelled through
      the external MCP cancel_mission tool) never fall into any window.
    - Windows are UTC-only, matching the scheduler's cron matching.
"""

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import db

log = logging.getLogger("devfleet.nightly_summary")

NIGHT_WINDOW_START = os.environ.get("DEVFLEET_NIGHTLY_WINDOW_START", "22:00")
NIGHT_WINDOW_END = os.environ.get("DEVFLEET_NIGHTLY_WINDOW_END", "06:00")

NIGHTLY_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS nightly_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    missions_completed INTEGER DEFAULT 0,
    missions_failed INTEGER DEFAULT 0,
    total_cost_usd REAL DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    summary_text TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_nightly_runs_project
    ON nightly_runs(project_id, window_end DESC);
"""

_SESSIONS_IN_WINDOW = """
    SELECT s.id, s.mission_id, s.status, s.ended_at, s.total_cost_usd, s.total_tokens,
           m.title, m.mission_number
    FROM agent_sessions s
    JOIN missions m ON m.id = s.mission_id
    WHERE m.project_id = ?
      AND s.ended_at IS NOT NULL
      AND datetime(s.ended_at) >= ? AND datetime(s.ended_at) < ?
    ORDER BY datetime(s.ended_at), s.rowid
"""

_REPORTS_IN_WINDOW = """
    SELECT r.mission_id, r.what_done, m.title, m.mission_number
    FROM reports r
    JOIN missions m ON m.id = r.mission_id
    WHERE m.project_id = ?
      AND datetime(r.created_at) >= ? AND datetime(r.created_at) < ?
    ORDER BY datetime(r.created_at), r.rowid
"""


async def ensure_schema(conn):
    """Create the nightly_runs table if it doesn't exist yet (idempotent)."""
    await conn.executescript(NIGHTLY_RUNS_SCHEMA)


# ── Time helpers ──

def _to_utc(dt: datetime) -> datetime:
    """Naive datetimes are treated as UTC; aware ones are converted to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sql_ts(dt: datetime) -> str:
    """Format matching SQLite datetime() output, for window comparisons."""
    return _to_utc(dt).strftime("%Y-%m-%d %H:%M:%S")


def _iso(dt: datetime) -> str:
    """Stored form of window bounds — also the key maybe_run dedupes on."""
    return _to_utc(dt).replace(microsecond=0).isoformat()


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = (int(part) for part in value.strip().split(":", 1))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid HH:MM time: {value!r}")
    return hour, minute


def last_closed_window(now: datetime, start: str, end: str) -> tuple[datetime, datetime]:
    """Most recent [start, end) window (UTC HH:MM bounds) that has closed by `now`.

    A window whose start is at or after its end (e.g. 22:00–06:00) spans midnight.
    """
    now = _to_utc(now)
    start_h, start_m = _parse_hhmm(start)
    end_h, end_m = _parse_hhmm(end)

    until = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if until > now:
        until -= timedelta(days=1)
    since = until.replace(hour=start_h, minute=start_m)
    if since >= until:
        since -= timedelta(days=1)
    return since, until


# ── Aggregation ──

def _mission_entry(row: dict) -> dict:
    return {
        "mission_id": row["mission_id"],
        "number": row["mission_number"],
        "title": row["title"],
        "status": "reported",  # overwritten by the latest session in the window, if any
        "sessions": 0,
        "cost_usd": 0.0,
        "tokens": 0,
        "what_done": [],
    }


def _format_digest(project_name: str, since: datetime, until: datetime,
                   totals: dict, missions: list[dict]) -> str:
    lines = [
        f"Nightly summary: {project_name}",
        f"Window: {since:%Y-%m-%d %H:%M} → {until:%Y-%m-%d %H:%M} UTC",
        f"Missions: {totals['missions_completed']} completed, "
        f"{totals['missions_failed']} failed, {totals['missions_other']} other"
        f" · Sessions: {totals['sessions_total']}"
        f" · Cost: ${totals['total_cost_usd']:.4f}"
        f" · Tokens: {totals['total_tokens']:,}",
    ]
    if not missions:
        lines += ["", "No agent activity in this window."]
    for m in missions:
        number = f"#{m['number']} " if m["number"] is not None else ""
        lines += ["", f"[{m['status']}] {number}{m['title']} (${m['cost_usd']:.4f})"]
        if m["what_done"]:
            lines += ["  - " + text.replace("\n", "\n    ") for text in m["what_done"]]
        else:
            lines.append("  - (no report)")
    return "\n".join(lines)


async def build_nightly_summary(project_id: str, since: datetime, until: datetime) -> dict:
    """Aggregate one project's agent activity in [since, until) and store it as a nightly_runs row.

    Returns the stored row plus non-stored detail: session counts, missions_other
    and a per-mission breakdown. Raises ValueError for an empty/inverted window
    and LookupError for an unknown project.
    """
    since, until = _to_utc(since), _to_utc(until)
    if since >= until:
        raise ValueError("since must be earlier than until")
    params = (project_id, _sql_ts(since), _sql_ts(until))

    conn = await db.get_db()
    try:
        await ensure_schema(conn)
        rows = await conn.execute_fetchall("SELECT name FROM projects WHERE id=?", (project_id,))
        if not rows:
            raise LookupError(f"Project {project_id} not found")
        project_name = rows[0]["name"]

        sessions = [dict(r) for r in await conn.execute_fetchall(_SESSIONS_IN_WINDOW, params)]
        reports = [dict(r) for r in await conn.execute_fetchall(_REPORTS_IN_WINDOW, params)]

        missions: dict[str, dict] = {}
        for s in sessions:  # ordered by ended_at, so the last one sets the outcome
            m = missions.setdefault(s["mission_id"], _mission_entry(s))
            m["status"] = s["status"]
            m["sessions"] += 1
            m["cost_usd"] += float(s["total_cost_usd"] or 0)
            m["tokens"] += int(s["total_tokens"] or 0)
        for r in reports:
            m = missions.setdefault(r["mission_id"], _mission_entry(r))
            text = (r["what_done"] or "").strip()
            if text and text != "None":
                m["what_done"].append(text)
        for m in missions.values():
            m["cost_usd"] = round(m["cost_usd"], 6)

        outcomes = [m["status"] for m in missions.values() if m["sessions"]]
        totals = {
            "missions_completed": outcomes.count("completed"),
            "missions_failed": outcomes.count("failed"),
            "missions_other": sum(1 for o in outcomes if o not in ("completed", "failed")),
            "sessions_total": len(sessions),
            "sessions_completed": sum(1 for s in sessions if s["status"] == "completed"),
            "sessions_failed": sum(1 for s in sessions if s["status"] == "failed"),
            "total_cost_usd": round(sum(float(s["total_cost_usd"] or 0) for s in sessions), 6),
            "total_tokens": sum(int(s["total_tokens"] or 0) for s in sessions),
        }
        summary_text = _format_digest(project_name, since, until, totals, list(missions.values()))

        run = {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "window_start": _iso(since),
            "window_end": _iso(until),
            "missions_completed": totals["missions_completed"],
            "missions_failed": totals["missions_failed"],
            "total_cost_usd": totals["total_cost_usd"],
            "total_tokens": totals["total_tokens"],
            "summary_text": summary_text,
        }
        await conn.execute(
            """INSERT INTO nightly_runs
               (id, project_id, window_start, window_end, missions_completed,
                missions_failed, total_cost_usd, total_tokens, summary_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            tuple(run.values()),
        )
        await conn.commit()
        created = await conn.execute_fetchall(
            "SELECT created_at FROM nightly_runs WHERE id=?", (run["id"],)
        )
        run["created_at"] = created[0]["created_at"]
    finally:
        await conn.close()

    log.info(
        "Nightly summary for '%s' [%s → %s]: %d completed, %d failed, $%.4f",
        project_name, run["window_start"], run["window_end"],
        run["missions_completed"], run["missions_failed"], run["total_cost_usd"],
    )
    return {
        **run,
        "missions_other": totals["missions_other"],
        "sessions_total": totals["sessions_total"],
        "sessions_completed": totals["sessions_completed"],
        "sessions_failed": totals["sessions_failed"],
        "missions": list(missions.values()),
    }


async def list_nightly_runs(project_id: str, limit: int = 30) -> list[dict]:
    """Stored nightly_runs rows for a project, newest window first."""
    conn = await db.get_db()
    try:
        await ensure_schema(conn)
        rows = await conn.execute_fetchall(
            """SELECT * FROM nightly_runs WHERE project_id=?
               ORDER BY window_end DESC, created_at DESC LIMIT ?""",
            (project_id, limit),
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def maybe_run_nightly_summary(since: Optional[datetime] = None,
                                    until: Optional[datetime] = None,
                                    now: Optional[datetime] = None) -> list[dict]:
    """Summarize a night window for every project that had activity in it, once.

    With explicit since/until, summarizes exactly that window. Without them,
    uses the most recently closed DEVFLEET_NIGHTLY_WINDOW_START/END window as
    of `now` (default: current UTC time). Projects that already have a
    nightly_runs row for the window, or had no session end in it, are skipped —
    so this is safe to call on every scheduler tick. Returns the new summaries.
    """
    if (since is None) != (until is None):
        raise ValueError("Pass both since and until, or neither")
    if since is None:
        since, until = last_closed_window(
            now or datetime.now(timezone.utc), NIGHT_WINDOW_START, NIGHT_WINDOW_END
        )
    since, until = _to_utc(since), _to_utc(until)
    if since >= until:
        raise ValueError("since must be earlier than until")

    conn = await db.get_db()
    try:
        await ensure_schema(conn)
        rows = await conn.execute_fetchall(
            """SELECT DISTINCT m.project_id
               FROM agent_sessions s
               JOIN missions m ON m.id = s.mission_id
               WHERE s.ended_at IS NOT NULL
                 AND datetime(s.ended_at) >= ? AND datetime(s.ended_at) < ?
                 AND m.project_id NOT IN (
                     SELECT project_id FROM nightly_runs
                     WHERE window_start = ? AND window_end = ?)""",
            (_sql_ts(since), _sql_ts(until), _iso(since), _iso(until)),
        )
        project_ids = [r["project_id"] for r in rows]
    finally:
        await conn.close()

    results = []
    for project_id in project_ids:
        try:
            results.append(await build_nightly_summary(project_id, since, until))
        except Exception as e:
            log.error("Nightly summary failed for project %s: %s", project_id, e)
    return results


# ──────────────────────────────────────────────
# API — mounted by the integrator via app.include_router(router)
# ──────────────────────────────────────────────

router = APIRouter(prefix="/api", tags=["nightly-summary"])


class NightlyRunRequest(BaseModel):
    since: Optional[datetime] = None  # default: 24h before `until`
    until: Optional[datetime] = None  # default: now


async def _require_project(pid: str):
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT id FROM projects WHERE id=?", (pid,))
        if not rows:
            raise HTTPException(404, "Project not found")
    finally:
        await conn.close()


@router.get("/projects/{pid}/nightly-runs")
async def api_list_nightly_runs(pid: str, limit: int = Query(30, ge=1, le=365)):
    await _require_project(pid)
    return await list_nightly_runs(pid, limit)


@router.post("/projects/{pid}/nightly-runs/run", status_code=201)
async def api_run_nightly_summary(pid: str, body: Optional[NightlyRunRequest] = None):
    """Manual trigger — summarize [since, until) now, defaulting to the last 24 hours."""
    await _require_project(pid)
    body = body or NightlyRunRequest()
    until = body.until or datetime.now(timezone.utc)
    since = body.since or _to_utc(until) - timedelta(hours=24)
    try:
        return await build_nightly_summary(pid, since, until)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
