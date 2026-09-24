"""Goal registry HTTP API. Status changes affect persistence only until integration."""

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

import db
import goals

router = APIRouter(tags=["goals"])


class GoalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    max_iterations: int = Field(default=20, gt=0, strict=True)


class GoalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "paused", "stopped"]
    reason: str | None = None


@router.post("/projects/{project_id}/goals", status_code=201)
async def create_project_goal(project_id: str, body: GoalCreate):
    try:
        goal_id = await goals.create_goal(project_id, body.goal_text, body.max_iterations)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return await goals.get_goal(goal_id)


@router.get("/projects/{project_id}/goals")
async def list_project_goals(project_id: str):
    conn = await db.get_db()
    try:
        projects = await conn.execute_fetchall(
            "SELECT id FROM projects WHERE id = ?", (project_id,),
        )
        if not projects:
            raise HTTPException(404, "Project not found")
    finally:
        await conn.close()
    return await goals.list_goals(project_id)


@router.patch("/goals/{goal_id}")
async def patch_goal(goal_id: str, body: GoalUpdate):
    try:
        await goals.update_goal_status(goal_id, body.status, body.reason)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return await goals.get_goal(goal_id)
