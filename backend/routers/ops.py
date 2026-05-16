"""Ops Router — runtime feature flags + manual task triggers.

Source: docs/alphagbm_skills_research_2026-05-15.md ops dashboard plan §1.2.

Phase 1 endpoints (this file):

* ``GET    /ops/flags``                       — list all whitelisted flags
* ``PATCH  /ops/flags/{name}``                — set / change one flag
* ``DELETE /ops/flags/{name}/override``       — clear override (revert to env)
* ``GET    /ops/flags/audit``                 — audit log
* ``POST   /ops/flags/refresh-all``           — broadcast cache refresh
* ``POST   /ops/tasks/trigger``               — manually fire a beat task
* ``GET    /ops/tasks/recent-runs``           — recent Celery results

Phase 2/3 will append /ops/alpha-health, /ops/pillar, etc. We keep them
all in this file (rather than splitting per page) so the router stays
discoverable; section headers below organize the file.

Auth: every endpoint requires the ``X-Ops-Token`` header to match the
``OPS_API_TOKEN`` env var. Dev / test sets the var to empty string to
disable auth — production MUST set it.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.services.feature_flag_service import (
    SUPPORTED_FLAGS,
    FeatureFlagService,
)
from backend.services.ops_service import (
    GLOBAL_THROTTLE_LIMIT,
    GlobalThrottledError,
    OpsService,
    OpsTriggerError,
    PerTaskThrottledError,
    UnknownTaskError,
)

router = APIRouter(
    prefix="/ops",
    tags=["ops"],
    responses={
        401: {"description": "Missing or invalid X-Ops-Token"},
        404: {"description": "Not found"},
    },
)


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def _require_ops_token(
    x_ops_token: Optional[str] = Header(default=None, alias="X-Ops-Token"),
) -> str:
    """Validate the ops token header against the OPS_API_TOKEN env var.

    Empty / unset env var disables auth (dev convenience). Production must
    set OPS_API_TOKEN to a non-empty secret.
    """
    expected = os.getenv("OPS_API_TOKEN", "").strip()
    if not expected:
        # Auth disabled — dev / test mode
        return "dev"
    if not x_ops_token or x_ops_token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-Ops-Token header",
        )
    return x_ops_token


# ---------------------------------------------------------------------------
# Service factories
# ---------------------------------------------------------------------------

def get_feature_flag_service(
    db: AsyncSession = Depends(get_db),
) -> FeatureFlagService:
    return FeatureFlagService(db)


def get_ops_service(
    db: AsyncSession = Depends(get_db),
) -> OpsService:
    return OpsService(db)


# ---------------------------------------------------------------------------
# Pydantic response / request models
# ---------------------------------------------------------------------------

class FlagStateOut(BaseModel):
    """Wire shape of FeatureFlagService.list_all entries."""
    name: str
    flag_type: str
    group: str
    description: str
    env_default: Any = None
    override_value: Optional[Any] = None
    effective_value: Any = None
    source: str
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None
    note: Optional[str] = None


class FlagSetIn(BaseModel):
    value: Any
    note: Optional[str] = Field(default=None, max_length=500)


class FlagAuditOut(BaseModel):
    id: int
    flag_name: str
    old_value: Optional[str]
    new_value: str
    action: str
    actor: str
    note: Optional[str]
    created_at: datetime


class TriggerIn(BaseModel):
    name: str = Field(..., description="Whitelisted Celery task name")
    kwargs: Optional[Dict[str, Any]] = None


class TriggerOut(BaseModel):
    task_id: str
    name: str
    accepted_at: datetime
    throttle_remaining_sec: int


class RecentRunOut(BaseModel):
    task_id: str
    name: Optional[str]
    status: Optional[str]
    date_done: Optional[str]
    result: Any = None


class RefreshAllOut(BaseModel):
    refreshed: int
    flags: List[str]


# ---------- Phase 2 (Alpha / Hypothesis Health + Overview) ----------------

class AlphaHealthSummaryOut(BaseModel):
    """Wire mirror of services.ops_service.AlphaHealthSummary."""
    report_date: Optional[str] = None
    band_counts: Dict[str, int] = Field(default_factory=dict)
    band_pcts: Dict[str, float] = Field(default_factory=dict)
    by_region: Dict[str, Dict[str, int]] = Field(default_factory=dict)
    total_alphas: int = 0
    failed: int = 0
    record_count: int = 0
    source: str
    stale_days: Optional[int] = None


class AlphaHealthLatestOut(BaseModel):
    summary: AlphaHealthSummaryOut
    source: str
    # We forward the raw payload so the React Drawer can show the full
    # JSON without a second round-trip — small reports (<5MB) keep this cheap.
    payload: Dict[str, Any] = Field(default_factory=dict)


class AlphaHealthRecordsOut(BaseModel):
    records: List[Dict[str, Any]] = Field(default_factory=list)
    total_unfiltered: int = 0
    source: str


class HypothesisHealthSummaryOut(BaseModel):
    report_date: Optional[str] = None
    total_active: int = 0
    total_triggered: int = 0
    avg_thesis_score: Optional[float] = None
    trigger_histogram: Dict[str, int] = Field(default_factory=dict)
    score_buckets: Dict[str, int] = Field(default_factory=dict)
    source: str
    stale_days: Optional[int] = None


class HypothesisHealthLatestOut(BaseModel):
    summary: HypothesisHealthSummaryOut
    source: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class HypothesisTransitionOut(BaseModel):
    id: int
    hypothesis_id: int
    old_is_triggered: Optional[bool]
    new_is_triggered: bool
    sharpe_at_transition: Optional[float]
    reason: Optional[str]
    source: Optional[str]
    transitioned_at: Optional[str]


class BeatStatusOut(BaseModel):
    source: str
    date: Optional[str] = None


class OverviewOut(BaseModel):
    beat_status: Dict[str, BeatStatusOut]
    alpha_health_summary: AlphaHealthSummaryOut
    hypothesis_health_summary: HypothesisHealthSummaryOut
    region_regime: Dict[str, Optional[str]] = Field(default_factory=dict)
    top_pitfalls: List[Dict[str, Any]] = Field(default_factory=list)


# ===========================================================================
# Feature flag endpoints
# ===========================================================================

@router.get("/flags", response_model=List[FlagStateOut])
async def list_flags(
    _token: str = Depends(_require_ops_token),
    svc: FeatureFlagService = Depends(get_feature_flag_service),
) -> List[FlagStateOut]:
    """List effective state of every supported flag.

    Includes env default + active override + source so the UI can render
    a 3-column table per row.
    """
    states = await svc.list_all()
    return [FlagStateOut(**s.__dict__) for s in states]


@router.patch("/flags/{name}", response_model=FlagStateOut)
async def set_flag(
    name: str,
    body: FlagSetIn,
    _token: str = Depends(_require_ops_token),
    svc: FeatureFlagService = Depends(get_feature_flag_service),
    actor: Optional[str] = Header(default=None, alias="X-Ops-Actor"),
) -> FlagStateOut:
    """Set / change a flag override.

    The body is ``{"value": <typed>, "note": "<optional>"}``. The value's
    Python type must match the flag's declared ``flag_type`` (else 400).
    """
    if name not in SUPPORTED_FLAGS:
        raise HTTPException(404, f"flag {name!r} is not in SUPPORTED_FLAGS")
    try:
        state = await svc.set(name, body.value, actor=actor or "ops_console", note=body.note)
    except ValueError as ex:
        raise HTTPException(400, str(ex)) from ex
    return FlagStateOut(**state.__dict__)


@router.delete("/flags/{name}/override", response_model=FlagStateOut)
async def clear_flag(
    name: str,
    _token: str = Depends(_require_ops_token),
    svc: FeatureFlagService = Depends(get_feature_flag_service),
    actor: Optional[str] = Header(default=None, alias="X-Ops-Actor"),
) -> FlagStateOut:
    """Clear an override → next read falls back to env default."""
    if name not in SUPPORTED_FLAGS:
        raise HTTPException(404, f"flag {name!r} is not in SUPPORTED_FLAGS")
    try:
        state = await svc.clear_override(name, actor=actor or "ops_console")
    except ValueError as ex:
        raise HTTPException(400, str(ex)) from ex
    return FlagStateOut(**state.__dict__)


@router.get("/flags/audit", response_model=List[FlagAuditOut])
async def list_flag_audit(
    limit: int = Query(50, ge=1, le=500),
    _token: str = Depends(_require_ops_token),
    svc: FeatureFlagService = Depends(get_feature_flag_service),
) -> List[FlagAuditOut]:
    """Most recent flip / clear audit records, newest first."""
    rows = await svc.list_audit(limit=limit)
    return [
        FlagAuditOut(
            id=r.id,
            flag_name=r.flag_name,
            old_value=r.old_value,
            new_value=r.new_value,
            action=r.action,
            actor=r.actor,
            note=r.note,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/flags/refresh-all", response_model=RefreshAllOut)
async def refresh_all_flags(
    _token: str = Depends(_require_ops_token),
    svc: FeatureFlagService = Depends(get_feature_flag_service),
) -> RefreshAllOut:
    """Force-pull overrides from DB into the in-process cache.

    Useful right after a flip if the operator doesn't want to wait the
    full 60s refresher window. Note: this only refreshes THIS process's
    cache (FastAPI). Celery worker processes still tick on their own
    timer, but the per-task lifetime of a flag is short enough that the
    drift doesn't matter for daily-beat tasks.
    """
    cache = await svc.load_overrides_into_cache()
    return RefreshAllOut(refreshed=len(cache), flags=sorted(cache.keys()))


# ===========================================================================
# Manual task trigger endpoints
# ===========================================================================

@router.post("/tasks/trigger", response_model=TriggerOut)
async def trigger_task(
    body: TriggerIn,
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
    actor: Optional[str] = Header(default=None, alias="X-Ops-Actor"),
) -> TriggerOut:
    """Fire a whitelisted Celery beat task on demand.

    Errors are translated:

    * task name unknown → 400
    * per-task throttled (within 60s) → 409
    * global rate limit hit (>10/min) → 429
    """
    try:
        result = await svc.trigger_task(
            body.name, body.kwargs, actor=actor or "ops_console",
        )
    except UnknownTaskError as ex:
        raise HTTPException(400, str(ex)) from ex
    except PerTaskThrottledError as ex:
        raise HTTPException(409, str(ex)) from ex
    except GlobalThrottledError as ex:
        raise HTTPException(
            429,
            f"{ex} (global limit {GLOBAL_THROTTLE_LIMIT}/min)",
        ) from ex
    except OpsTriggerError as ex:
        raise HTTPException(400, str(ex)) from ex
    return TriggerOut(**result.__dict__)


@router.get("/tasks/recent-runs", response_model=List[RecentRunOut])
async def recent_task_runs(
    task_name: Optional[str] = Query(None, description="Filter by Celery task name"),
    limit: int = Query(20, ge=1, le=200),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> List[RecentRunOut]:
    """Walk the Celery result backend for recent task results.

    Returns at most 200 entries; default 20. Empty list on Redis outage —
    the dashboard renders a "no recent runs visible" hint instead of
    erroring out.
    """
    rows = await svc.list_recent_celery_runs(task_name=task_name, limit=limit)
    return [RecentRunOut(**r.__dict__) for r in rows]


# ===========================================================================
# Phase 2 — Alpha Health endpoints
# ===========================================================================

# Trigger task names for the convenience "rerun" buttons. Kept here (rather
# than inline strings on each endpoint) so the whitelist is the single
# source of truth alongside _ALLOWED_TRIGGER_NAMES in OpsService.
_ALPHA_HEALTH_TASK = "backend.tasks.run_alpha_health_check"
_HYPOTHESIS_HEALTH_TASK = "backend.tasks.run_hypothesis_health_check"


@router.get("/alpha-health/latest", response_model=AlphaHealthLatestOut)
async def alpha_health_latest(
    date_: Optional[date] = Query(None, alias="date"),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> AlphaHealthLatestOut:
    """Latest alpha_health_check summary + raw payload."""
    result = await svc.get_alpha_health(date_)
    return AlphaHealthLatestOut(
        summary=AlphaHealthSummaryOut(**result["summary"].__dict__),
        source=result["source"],
        payload=result["payload"],
    )


@router.get("/alpha-health/history")
async def alpha_health_history(
    days: int = Query(30, ge=1, le=180),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> List[Dict[str, Any]]:
    """Chronological per-day summary, oldest→newest, missing days skipped.

    Each entry shape: ``{"_date": "2026-05-16", "band_counts": {...},
    "band_pcts": {...}, "total_alphas": N}``. Returned as a raw dict list
    rather than a typed BaseModel because the ``_date`` key starts with
    an underscore (matches OpsReportReader's stamp convention) which
    Pydantic v2 rejects as a field name.
    """
    return await svc.get_alpha_health_history(days=days)


@router.get("/alpha-health/alphas", response_model=AlphaHealthRecordsOut)
async def alpha_health_records(
    band: Optional[str] = Query(None, description="Comma-separated bands, e.g. 'RED,YELLOW'"),
    region: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    date_: Optional[date] = Query(None, alias="date"),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> AlphaHealthRecordsOut:
    """Filtered drill-down list — defaults to today's records, no filter."""
    bands_list = [b.strip().upper() for b in band.split(",")] if band else None
    result = await svc.get_alpha_health_records(
        target=date_, bands=bands_list, region=region, limit=limit,
    )
    return AlphaHealthRecordsOut(**result)


@router.post("/alpha-health/rerun", response_model=TriggerOut)
async def alpha_health_rerun(
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
    actor: Optional[str] = Header(default=None, alias="X-Ops-Actor"),
) -> TriggerOut:
    """Fire the daily alpha_health_check Celery task on demand.

    Same throttle rules as /tasks/trigger — 60s per task, 10/min global.
    Operator polls GET /alpha-health/latest a few seconds later for the
    refreshed payload.
    """
    try:
        result = await svc.trigger_task(_ALPHA_HEALTH_TASK, actor=actor or "ops_console")
    except UnknownTaskError as ex:
        raise HTTPException(400, str(ex)) from ex
    except PerTaskThrottledError as ex:
        raise HTTPException(409, str(ex)) from ex
    except GlobalThrottledError as ex:
        raise HTTPException(429, str(ex)) from ex
    return TriggerOut(**result.__dict__)


# ===========================================================================
# Phase 2 — Hypothesis Health endpoints
# ===========================================================================

@router.get("/hypothesis-health/latest", response_model=HypothesisHealthLatestOut)
async def hypothesis_health_latest(
    date_: Optional[date] = Query(None, alias="date"),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> HypothesisHealthLatestOut:
    """Latest hypothesis_health_check summary + raw payload."""
    result = await svc.get_hypothesis_health(date_)
    return HypothesisHealthLatestOut(
        summary=HypothesisHealthSummaryOut(**result["summary"]),
        source=result["source"],
        payload=result["payload"],
    )


@router.get("/hypothesis-health/history")
async def hypothesis_health_history(
    days: int = Query(30, ge=1, le=180),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> List[Dict[str, Any]]:
    """30d trend of triggered count + avg score.

    Raw list (see alpha_health_history for why) — ``_date`` key clashes
    with Pydantic v2's leading-underscore field-name guard.
    """
    return await svc.get_hypothesis_health_history(days=days)


@router.get(
    "/hypothesis-health/transitions", response_model=List[HypothesisTransitionOut],
)
async def hypothesis_transitions(
    hypothesis_id: Optional[int] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> List[HypothesisTransitionOut]:
    """Audit log of is_triggered edge transitions, newest first."""
    return await svc.get_hypothesis_transitions(
        hypothesis_id=hypothesis_id, limit=limit,
    )


@router.post("/hypothesis-health/rerun", response_model=TriggerOut)
async def hypothesis_health_rerun(
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
    actor: Optional[str] = Header(default=None, alias="X-Ops-Actor"),
) -> TriggerOut:
    """Fire the daily hypothesis_health_check Celery task on demand."""
    try:
        result = await svc.trigger_task(
            _HYPOTHESIS_HEALTH_TASK, actor=actor or "ops_console",
        )
    except UnknownTaskError as ex:
        raise HTTPException(400, str(ex)) from ex
    except PerTaskThrottledError as ex:
        raise HTTPException(409, str(ex)) from ex
    except GlobalThrottledError as ex:
        raise HTTPException(429, str(ex)) from ex
    return TriggerOut(**result.__dict__)


# ===========================================================================
# Phase 2 — Overview (one GET fills the whole /ops/overview page)
# ===========================================================================

@router.get("/overview", response_model=OverviewOut)
async def overview(
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> OverviewOut:
    """Aggregates all seven daily-beat sources into one payload."""
    raw = await svc.get_overview()
    return OverviewOut(
        beat_status={
            k: BeatStatusOut(**v) for k, v in raw["beat_status"].items()
        },
        alpha_health_summary=AlphaHealthSummaryOut(
            **raw["alpha_health_summary"].__dict__
        ),
        hypothesis_health_summary=HypothesisHealthSummaryOut(
            **raw["hypothesis_health_summary"]
        ),
        region_regime=raw["region_regime"],
        top_pitfalls=raw["top_pitfalls"],
    )
