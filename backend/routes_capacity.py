"""
Capacity Manager routes — Track 4.

Not wired into app.py yet: per the feature spec's Integration Plan, the
integrator adds a single `app.include_router(router)` line at integration
time. Building/testing this in isolation needs nothing else from app.py.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from capacity import get_capacity_config, set_capacity_config

router = APIRouter()


class CapacityConfigUpdate(BaseModel):
    project_id: Optional[str] = None  # omit/null for the global default config
    day_limit: int
    night_limit: int
    window_id: Optional[str] = None   # references a Track 2 night_windows row


@router.put("/capacity")
async def put_capacity(body: CapacityConfigUpdate):
    return await set_capacity_config(
        body.project_id, body.day_limit, body.night_limit, body.window_id
    )


@router.get("/capacity")
async def get_capacity(project_id: Optional[str] = None):
    config = await get_capacity_config(project_id)
    if config is None:
        raise HTTPException(status_code=404, detail="No capacity config found")
    return config
