"""
Swarm routes (Track 1).

Wire in at integration with one line in app.py:
    from routes_swarm import router as swarm_router
    app.include_router(swarm_router)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from swarm_planner import launch_swarm, get_swarm_status

router = APIRouter(prefix="/api", tags=["swarms"])


class SwarmCreate(BaseModel):
    goal: str = Field(..., min_length=1)
    max_agents: int = Field(3, ge=1, le=20)


@router.post("/projects/{pid}/swarms", status_code=201)
async def create_swarm(pid: str, body: SwarmCreate):
    try:
        swarm_id = await launch_swarm(pid, body.goal, body.max_agents)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(502, f"Swarm planner failed: {e}")
    return {"swarm_id": swarm_id}


@router.get("/swarms/{swarm_id}")
async def swarm_status(swarm_id: str):
    status = await get_swarm_status(swarm_id)
    if status is None:
        raise HTTPException(404, "Swarm not found")
    return status
