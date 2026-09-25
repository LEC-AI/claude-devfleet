"""
Night-window API — Track 2.

    GET /api/projects/{pid}/window   -> current window or {"configured": false}
    PUT /api/projects/{pid}/window   -> create or replace the project's window

Mounted under /api like every other route, because nginx and the Vite dev
proxy only forward /api/. One window per project; PUT is an upsert keyed on
project_id. Stored values are normalised: strict HH:MM, canonical timezone.
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator, model_validator

import db
from night_window import DEFAULT_TIMEZONE, _parse_hhmm, canonical_timezone

logger = logging.getLogger("devfleet.night_window.routes")

router = APIRouter(prefix="/api", tags=["night-window"])


class NightWindowUpsert(BaseModel):
    start_time: str                              # "HH:MM"
    end_time: str                                # "HH:MM", may be earlier than start (wraps midnight)
    timezone: Optional[str] = DEFAULT_TIMEZONE   # IANA name, e.g. "Europe/London"
    enabled: bool = True

    @field_validator("start_time", "end_time")
    @classmethod
    def _valid_hhmm(cls, v: str) -> str:
        _parse_hhmm(v)  # strict HH:MM, ASCII digits; raises ValueError -> 422
        return v

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: Optional[str]) -> str:
        return canonical_timezone(v or DEFAULT_TIMEZONE)  # "utc" -> "UTC"; unknown -> 422

    @model_validator(mode="after")
    def _start_differs_from_end(self):
        if self.start_time == self.end_time:
            raise ValueError("start_time and end_time must differ (a window cannot be empty or 24 h)")
        return self


def _row_to_response(row) -> dict:
    d = dict(row)
    d["enabled"] = bool(d.get("enabled", 0))
    d["configured"] = True
    return d


async def _require_project(conn, pid: str) -> None:
    rows = await conn.execute_fetchall("SELECT 1 FROM projects WHERE id=?", (pid,))
    if not rows:
        raise HTTPException(404, "Project not found")


@router.get("/projects/{pid}/window")
async def get_project_window(pid: str):
    conn = await db.get_db()
    try:
        await _require_project(conn, pid)
        rows = await conn.execute_fetchall(
            "SELECT * FROM night_windows WHERE project_id=?", (pid,)
        )
        if not rows:
            return {"configured": False, "project_id": pid}
        return _row_to_response(rows[0])
    finally:
        await conn.close()


@router.put("/projects/{pid}/window")
async def put_project_window(pid: str, body: NightWindowUpsert):
    conn = await db.get_db()
    try:
        await _require_project(conn, pid)
        await conn.execute(
            """
            INSERT INTO night_windows (id, project_id, start_time, end_time, timezone, enabled)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                start_time = excluded.start_time,
                end_time   = excluded.end_time,
                timezone   = excluded.timezone,
                enabled    = excluded.enabled,
                updated_at = datetime('now')
            """,
            (str(uuid.uuid4()), pid, body.start_time, body.end_time,
             body.timezone, 1 if body.enabled else 0),
        )
        await conn.commit()
        rows = await conn.execute_fetchall(
            "SELECT * FROM night_windows WHERE project_id=?", (pid,)
        )
        logger.info("night_window: project %s window set %s-%s %s enabled=%s",
                    pid, body.start_time, body.end_time, body.timezone, body.enabled)
        return _row_to_response(rows[0])
    finally:
        await conn.close()
