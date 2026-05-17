"""
Mining Session Router — V-19 Persistent Mining Service mode (2026-05-10)

Single-button start/stop endpoint for the persistent CONTINUOUS_CASCADE
mining service. Per-region singleton (enforced by partial unique index).

Endpoints:
  GET    /api/v1/mining-session              — list all active sessions
  GET    /api/v1/mining-session/{region}     — single region active session
  POST   /api/v1/mining-session/start        — start/resume session for region
  POST   /api/v1/mining-session/stop         — pause active session by task_id
  POST   /api/v1/mining-session/resume       — explicit resume by task_id

start_session is idempotent: hits the same endpoint twice returns the
same active session (or auto-resumes a PAUSED one).
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.services.task_service import TaskService, MiningSessionInfo

router = APIRouter(
    prefix="/mining-session",
    tags=["mining-session"],
    responses={404: {"description": "Not found"}},
)


# =============================================================================
# DEPENDENCY INJECTION
# =============================================================================

def get_task_service(db: AsyncSession = Depends(get_db)) -> TaskService:
    """Inject TaskService — same pattern as routers/tasks.py."""
    return TaskService(db)


# =============================================================================
# REQUEST / RESPONSE MODELS
# =============================================================================

class MiningSessionResponse(BaseModel):
    task_id: int
    task_name: str
    region: str
    universe: str
    status: str
    mining_mode: str
    cascade_phase: Optional[str] = None
    cascade_round_idx: int
    progress_current: int
    last_alpha_persisted_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    paused_at: Optional[datetime] = None
    # Phase 1.5-C [V1.2-C5] (2026-05-18): new authoritative scheduling
    # fields. Optional for backward compat — old clients ignoring these
    # still work.
    schedule: Optional[str] = None
    starting_tier: Optional[int] = None
    current_tier: Optional[int] = None

    class Config:
        from_attributes = True


def _info_to_response(info: MiningSessionInfo) -> MiningSessionResponse:
    return MiningSessionResponse(
        task_id=info.task_id,
        task_name=info.task_name,
        region=info.region,
        universe=info.universe,
        status=info.status,
        mining_mode=info.mining_mode,
        cascade_phase=info.cascade_phase,
        cascade_round_idx=info.cascade_round_idx,
        progress_current=info.progress_current,
        last_alpha_persisted_at=info.last_alpha_persisted_at,
        started_at=info.started_at,
        paused_at=info.paused_at,
        # Phase 1.5-C new fields
        schedule=info.schedule,
        starting_tier=info.starting_tier,
        current_tier=info.current_tier,
    )


class StartSessionRequest(BaseModel):
    region: str = Field(default="USA", description="Region code (USA/CHN/EUR/ASI/GLB)")
    universe: str = Field(default="TOP3000", description="Universe code")


class SessionActionRequest(BaseModel):
    task_id: int = Field(description="Session task_id to act on")


# =============================================================================
# ENDPOINTS
# =============================================================================

@router.get("", response_model=List[MiningSessionResponse])
async def list_active_sessions(
    service: TaskService = Depends(get_task_service),
) -> List[MiningSessionResponse]:
    """List all active CONTINUOUS_CASCADE sessions (across regions)."""
    sessions = await service.list_active_sessions()
    return [_info_to_response(s) for s in sessions]


@router.get("/{region}", response_model=Optional[MiningSessionResponse])
async def get_active_session(
    region: str,
    service: TaskService = Depends(get_task_service),
) -> Optional[MiningSessionResponse]:
    """Return the active session for a region, or 404 if none.

    region is case-sensitive (USA/CHN/EUR/ASI/GLB).
    """
    if region not in TaskService.SUPPORTED_REGIONS:
        raise HTTPException(
            status_code=400,
            detail=f"region={region!r} not supported; choose one of {TaskService.SUPPORTED_REGIONS}",
        )
    session = await service.get_active_session(region)
    if not session:
        raise HTTPException(
            status_code=404,
            detail=f"no active mining session for region={region}",
        )
    return _info_to_response(session)


@router.post("/start", response_model=MiningSessionResponse)
async def start_session(
    body: StartSessionRequest,
    service: TaskService = Depends(get_task_service),
) -> MiningSessionResponse:
    """Start (or auto-resume) the singleton mining session for a region.

    Idempotent — already-RUNNING returns as-is, PAUSED auto-resumes.
    """
    try:
        session = await service.start_session(region=body.region, universe=body.universe)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _info_to_response(session)


@router.post("/stop", response_model=MiningSessionResponse)
async def stop_session(
    body: SessionActionRequest,
    service: TaskService = Depends(get_task_service),
) -> MiningSessionResponse:
    """Pause an active mining session.

    Worker detects PAUSED at next round boundary and exits gracefully.
    cascade_phase / cascade_round_idx preserved for RESUME.
    """
    try:
        session = await service.stop_session(body.task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _info_to_response(session)


@router.post("/resume", response_model=MiningSessionResponse)
async def resume_session(
    body: SessionActionRequest,
    service: TaskService = Depends(get_task_service),
) -> MiningSessionResponse:
    """Explicit RESUME from PAUSED. Equivalent to POST /start when the
    region's session is PAUSED, but lets clients re-dispatch by task_id.
    """
    try:
        session = await service.resume_session(body.task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _info_to_response(session)
