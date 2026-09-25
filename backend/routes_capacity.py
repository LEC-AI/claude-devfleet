"""
Capacity Manager routes — Track 4.

Not wired into app.py yet: per the feature spec's Integration Plan, the
integrator adds a single `app.include_router(router)` line at integration
time. Building/testing this in isolation needs nothing else from app.py.
Prefixed with /api to match every other route in app.py.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from capacity import (
    InvalidCapacityConfig,
    ProjectNotFound,
    get_capacity_config,
    set_capacity_config,
)

router = APIRouter(prefix="/api")


class CapacityConfigUpdate(BaseModel):
    project_id: Optional[str] = None       # omit/null for the global default config
    day_limit: int = Field(ge=0)
    night_limit: int = Field(ge=0)
    window_id: Optional[str] = None        # references a Track 2 night_windows row


@router.put("/capacity")
async def put_capacity(body: CapacityConfigUpdate):
    try:
        return await set_capacity_config(
            body.project_id, body.day_limit, body.night_limit, body.window_id
        )
    except ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=f"No project with id '{e}'") from e
    except InvalidCapacityConfig as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/capacity")
async def get_capacity(project_id: Optional[str] = Query(default=None)):
    try:
        config = await get_capacity_config(project_id)
    except InvalidCapacityConfig as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if config is None:
        raise HTTPException(status_code=404, detail="No capacity config found")
    return config
