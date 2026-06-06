"""
Tasks Router - Mining task management

Uses TaskService for all business logic.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime
from celery.result import AsyncResult

from backend.database import get_db
from backend.services.task_service import TaskService
from backend.celery_app import celery_app

router = APIRouter(
    prefix="/tasks",
    tags=["tasks"],
    responses={404: {"description": "Not found"}},
)


# =============================================================================
# DEPENDENCY INJECTION
# =============================================================================

def get_task_service(db: AsyncSession = Depends(get_db)) -> TaskService:
    """Get TaskService instance with injected dependencies."""
    return TaskService(db)


# =============================================================================
# REQUEST/RESPONSE MODELS
# =============================================================================



class TaskResponse(BaseModel):
    id: int
    task_name: str
    region: str
    universe: str
    dataset_strategy: str
    status: str
    daily_goal: int
    progress_current: int
    current_iteration: int = 0
    max_iterations: int = 10
    created_at: datetime
    updated_at: Optional[datetime] = None
    schedule: Optional[str] = None

    class Config:
        from_attributes = True


class TraceStepResponse(BaseModel):
    id: int
    step_type: str
    step_order: int
    iteration: int = 1
    input_data: dict
    output_data: dict
    duration_ms: Optional[int] = None
    status: str
    error_message: Optional[str] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


class TaskDetailResponse(TaskResponse):
    trace_steps: List[TraceStepResponse] = []
    alphas_count: int = 0




class InterventionRequest(BaseModel):
    action: str  # PAUSE, RESUME, SKIP, ADJUST
    parameters: dict = {}


# =============================================================================
# ENDPOINTS
# =============================================================================

@router.get("", response_model=List[TaskResponse])
async def list_tasks(
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    service: TaskService = Depends(get_task_service),
):
    """List all mining tasks with optional status filter."""
    tasks = await service.list_tasks(status=status, limit=limit, offset=offset)

    return [
        TaskResponse(
            id=t.id,
            task_name=t.task_name,
            region=t.region,
            universe=t.universe,
            dataset_strategy=t.dataset_strategy,
            status=t.status,
            daily_goal=t.daily_goal,
            progress_current=t.progress_current,
            current_iteration=t.current_iteration,
            max_iterations=t.max_iterations,
            created_at=t.created_at,
            updated_at=t.updated_at,
            schedule=getattr(t, "schedule", None),
        )
        for t in tasks
    ]




@router.get("/{task_id}", response_model=TaskDetailResponse)
async def get_task(
    task_id: int,
    service: TaskService = Depends(get_task_service),
):
    """Get task details including trace steps."""
    detail = await service.get_task_detail(task_id)

    if not detail:
        raise HTTPException(status_code=404, detail="Task not found")

    return TaskDetailResponse(
        id=detail.id,
        task_name=detail.task_name,
        region=detail.region,
        universe=detail.universe,
        dataset_strategy=detail.dataset_strategy,
        status=detail.status,
        daily_goal=detail.daily_goal,
        progress_current=detail.progress_current,
        current_iteration=detail.current_iteration,
        max_iterations=detail.max_iterations,
        created_at=detail.created_at,
        updated_at=detail.updated_at,
        schedule=getattr(detail, "schedule", None),
        trace_steps=[
            TraceStepResponse(
                id=s.id,
                step_type=s.step_type,
                step_order=s.step_order,
                iteration=s.iteration,
                input_data=s.input_data,
                output_data=s.output_data,
                duration_ms=s.duration_ms,
                status=s.status,
                error_message=s.error_message,
                created_at=s.created_at,
            )
            for s in detail.trace_steps
        ],
        alphas_count=detail.alphas_count,
    )


@router.get("/{task_id}/trace", response_model=List[TraceStepResponse])
async def get_task_trace(
    task_id: int,
    service: TaskService = Depends(get_task_service),
):
    """Get the complete trace (all steps) for a task."""
    try:
        steps = await service.get_task_trace(task_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    
    return [
        TraceStepResponse(
            id=s.id,
            step_type=s.step_type,
            step_order=s.step_order,
            iteration=s.iteration,
            input_data=s.input_data,
            output_data=s.output_data,
            duration_ms=s.duration_ms,
            status=s.status,
            error_message=s.error_message,
            created_at=s.created_at,
        )
        for s in steps
    ]






@router.post("/{task_id}/intervene")
async def intervene_task(
    task_id: int,
    request: InterventionRequest,
    service: TaskService = Depends(get_task_service),
):
    """Human intervention endpoint - pause, resume, skip, or adjust a running task."""
    try:
        result = await service.intervene_task(
            task_id=task_id,
            action=request.action,
            parameters=request.parameters,
        )
        return {
            "message": f"Task {result['action']}",
            "task_id": task_id,
            **result,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/celery/{task_id}/status")
async def get_celery_task_status(task_id: str):
    """Get status of a Celery background task by UUID."""
    result = AsyncResult(task_id, app=celery_app)
    
    response = {
        "task_id": task_id,
        "status": result.status,
    }
    
    if result.ready():
        try:
            if result.failed():
                response["error"] = str(result.result)
            else:
                response["result"] = result.result
        except Exception as e:
            response["error"] = str(e)
            
    return response
