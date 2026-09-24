"""Persistent goals; no dependency on planners, dispatch, or in-memory loops."""

import uuid
from datetime import datetime, timezone

import db

GOAL_STATUSES = frozenset({"active", "paused", "complete", "stopped"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_goal(project_id: str, goal_text: str, max_iterations: int = 20) -> str:
    """Create an active goal. Raise LookupError if the project does not exist."""
    if not isinstance(goal_text, str) or not goal_text.strip():
        raise ValueError("goal_text must not be blank")
    if type(max_iterations) is not int or max_iterations < 1:
        raise ValueError("max_iterations must be a positive integer")
    goal_id = str(uuid.uuid4())
    now = _now()
    conn = await db.get_db()
    try:
        # The INSERT checks existence atomically, avoiding a check/write race.
        cursor = await conn.execute(
            """INSERT INTO goals_registry
               (id, project_id, goal_text, max_iterations, created_at, updated_at)
               SELECT ?, id, ?, ?, ?, ? FROM projects WHERE id = ?""",
            (goal_id, goal_text.strip(), max_iterations, now, now, project_id),
        )
        if cursor.rowcount == 0:
            raise LookupError("Project not found")
        await conn.commit()
        return goal_id
    finally:
        await conn.close()


async def get_goal(goal_id: str) -> dict | None:
    """Return a goal by ID, including paused and terminal goals."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT * FROM goals_registry WHERE id = ?", (goal_id,),
        )
        return dict(rows[0]) if rows else None
    finally:
        await conn.close()


async def get_active_goal(project_id: str) -> dict | None:
    """Return the newest active goal (ID breaks creation-time ties), or None.

    Multiple active goals are allowed; callers needing all of them use list_goals.
    Selection is based on creation time, so updating progress does not reorder goals.
    """
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT * FROM goals_registry WHERE project_id = ? AND status = 'active'
               ORDER BY created_at DESC, id DESC LIMIT 1""", (project_id,),
        )
        return dict(rows[0]) if rows else None
    finally:
        await conn.close()


async def list_goals(project_id: str) -> list[dict]:
    """Return all goals for a project, newest first, including goal history."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT * FROM goals_registry WHERE project_id = ?
               ORDER BY created_at DESC, id DESC""", (project_id,),
        )
        return [dict(row) for row in rows]
    finally:
        await conn.close()


async def update_goal_status(goal_id: str, status: str, reason: str | None = None):
    """Set a valid status and optional reason; resume clears the old reason.

    Transition policy belongs to callers. Missing goals raise LookupError.
    """
    if status not in GOAL_STATUSES:
        raise ValueError("status must be active, paused, complete, or stopped")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be a string or None")
    conn = await db.get_db()
    try:
        cursor = await conn.execute(
            """UPDATE goals_registry SET status = ?, stopped_reason = ?, updated_at = ?
               WHERE id = ?""",
            (status, None if status == "active" else reason, _now(), goal_id),
        )
        if cursor.rowcount == 0:
            raise LookupError("Goal not found")
        await conn.commit()
    finally:
        await conn.close()


async def increment_iteration(goal_id: str):
    """Atomically record an iteration; callers enforce max_iterations and status.

    This does not automatically mark a goal complete: a limit is not success.
    Missing goals raise LookupError.
    """
    conn = await db.get_db()
    try:
        cursor = await conn.execute(
            """UPDATE goals_registry
               SET current_iteration = current_iteration + 1, updated_at = ? WHERE id = ?""",
            (_now(), goal_id),
        )
        if cursor.rowcount == 0:
            raise LookupError("Goal not found")
        await conn.commit()
    finally:
        await conn.close()
