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


# ---------- Phase 3 (Pillar / Negative / Macro / Regime) ------------------

class PillarLatestOut(BaseModel):
    payload: Dict[str, Any] = Field(default_factory=dict)
    source: str


class PillarDeficitOut(BaseModel):
    region: str
    next_pillar: Optional[str] = None


class NegativeTopOut(BaseModel):
    records: List[Dict[str, Any]] = Field(default_factory=list)
    source: str


class NegativeCategoryOut(BaseModel):
    by_category: Dict[str, int] = Field(default_factory=dict)
    source: str


class PitfallToggleIn(BaseModel):
    is_active: bool


class PitfallToggleOut(BaseModel):
    id: int
    is_active: bool
    updated: bool


class MacroLatestOut(BaseModel):
    payload: Dict[str, Any] = Field(default_factory=dict)
    source: str


class MacroCoverageOut(BaseModel):
    coverage: Dict[str, Any]
    source: str


class MacroByScopeOut(BaseModel):
    records: List[Dict[str, Any]] = Field(default_factory=list)
    source: str


class MacroTokenBudgetOut(BaseModel):
    utc_date: str
    tokens_used: int
    redis_ok: bool


class RegimeCurrentOut(BaseModel):
    region: str
    regime: Optional[str] = None
    source: str


class RegimeSnapshotOut(BaseModel):
    snapshot: Dict[str, Any] = Field(default_factory=dict)
    source: str


class LLMOpSummary(BaseModel):
    scanned: int = 0
    valid_ops_in_registry: int = 0
    clean: int = 0
    pattern_halluc: int = 0
    template_halluc: int = 0
    deactivated: int = 0
    hallucinated_ops: List[Dict[str, Any]] = Field(default_factory=list)
    affected_entries: List[Dict[str, Any]] = Field(default_factory=list)


class LLMOpLatestOut(BaseModel):
    summary: LLMOpSummary
    source: str
    stale_days: Optional[int] = None
    report_date: Optional[str] = None


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


# ===========================================================================
# Phase 3 — P2-B Pillar Balance endpoints
# ===========================================================================

_PILLAR_TASK = "backend.tasks.run_pillar_balance_check"
_NEGATIVE_TASK = "backend.tasks.run_negative_knowledge_extract"
_MACRO_TASK = "backend.tasks.run_macro_narrative_extract"
_REGIME_TASK = "backend.tasks.run_regime_infer"


async def _run_trigger(svc: OpsService, task_name: str, actor: str):
    """Translate OpsService trigger errors to HTTP statuses.

    Phase 3 added 4 more rerun endpoints; this helper dedupes the same
    try/except shape used by /alpha-health/rerun and /hypothesis-health
    /rerun above. Kept module-local (not in OpsService) because it's
    purely an HTTP-translation concern.
    """
    try:
        return await svc.trigger_task(task_name, actor=actor)
    except UnknownTaskError as ex:
        raise HTTPException(400, str(ex)) from ex
    except PerTaskThrottledError as ex:
        raise HTTPException(409, str(ex)) from ex
    except GlobalThrottledError as ex:
        raise HTTPException(429, str(ex)) from ex


@router.get("/pillar/latest", response_model=PillarLatestOut)
async def pillar_latest(
    date_: Optional[date] = Query(None, alias="date"),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> PillarLatestOut:
    """Pillar balance report for ``date`` (defaults to today, fresh service)."""
    result = await svc.get_pillar_latest(date_)
    return PillarLatestOut(**result)


@router.get("/pillar/history")
async def pillar_history(
    days: int = Query(14, ge=1, le=180),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> List[Dict[str, Any]]:
    """Per-day pillar reports oldest→newest. Each entry carries ``_date``."""
    return await svc.get_pillar_history(days=days)


@router.get("/pillar/deficit-recommendation", response_model=PillarDeficitOut)
async def pillar_deficit(
    region: str = Query(..., min_length=1),
    skew_threshold: float = Query(0.0, ge=0.0, le=1.0),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> PillarDeficitOut:
    """Which pillar is most under-represented in ``region`` right now."""
    out = await svc.get_pillar_deficit_recommendation(
        region, skew_threshold=skew_threshold,
    )
    return PillarDeficitOut(**out)


@router.post("/pillar/rerun", response_model=TriggerOut)
async def pillar_rerun(
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
    actor: Optional[str] = Header(default=None, alias="X-Ops-Actor"),
) -> TriggerOut:
    result = await _run_trigger(svc, _PILLAR_TASK, actor or "ops_console")
    return TriggerOut(**result.__dict__)


# ===========================================================================
# Phase 3 — P2-D Negative Knowledge endpoints
# ===========================================================================

@router.get("/negative-knowledge/top", response_model=NegativeTopOut)
async def negative_top(
    region: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=200),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> NegativeTopOut:
    """Top active pitfalls (DB live)."""
    result = await svc.get_negative_knowledge_top(
        region=region, limit=limit, category=category,
    )
    return NegativeTopOut(**result)


@router.get("/negative-knowledge/category-breakdown", response_model=NegativeCategoryOut)
async def negative_category_breakdown(
    region: Optional[str] = Query(None),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> NegativeCategoryOut:
    result = await svc.get_negative_knowledge_category_breakdown(region=region)
    return NegativeCategoryOut(**result)


@router.get("/negative-knowledge/timeline")
async def negative_timeline(
    days: int = Query(30, ge=1, le=180),
    region: Optional[str] = Query(None),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> List[Dict[str, Any]]:
    return await svc.get_negative_knowledge_timeline(days=days, region=region)


@router.patch("/negative-knowledge/entries/{entry_id}", response_model=PitfallToggleOut)
async def negative_toggle(
    entry_id: int,
    body: PitfallToggleIn,
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> PitfallToggleOut:
    """Soft-enable / disable a single pitfall row (操作员的"禁用" Switch)."""
    updated = await svc.set_pitfall_active(entry_id, body.is_active)
    if not updated:
        raise HTTPException(404, f"pitfall id={entry_id} not found or not a FAILURE_PITFALL")
    return PitfallToggleOut(id=entry_id, is_active=body.is_active, updated=True)


@router.post("/negative-knowledge/rerun", response_model=TriggerOut)
async def negative_rerun(
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
    actor: Optional[str] = Header(default=None, alias="X-Ops-Actor"),
) -> TriggerOut:
    result = await _run_trigger(svc, _NEGATIVE_TASK, actor or "ops_console")
    return TriggerOut(**result.__dict__)


# ===========================================================================
# Phase 3 — P2-A Macro Narrative endpoints
# ===========================================================================

@router.get("/macro/latest", response_model=MacroLatestOut)
async def macro_latest(
    date_: Optional[date] = Query(None, alias="date"),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> MacroLatestOut:
    result = await svc.get_macro_latest(date_)
    return MacroLatestOut(**result)


@router.get("/macro/coverage", response_model=MacroCoverageOut)
async def macro_coverage(
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> MacroCoverageOut:
    """Field coverage + per-scope narrative counts."""
    result = await svc.get_macro_coverage()
    return MacroCoverageOut(**result)


@router.get("/macro/by-scope", response_model=MacroByScopeOut)
async def macro_by_scope(
    scope: str = Query(..., pattern="^(field|dataset|category)$"),
    dataset_category: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> MacroByScopeOut:
    result = await svc.get_macro_by_scope(
        scope=scope, dataset_category=dataset_category, limit=limit,
    )
    return MacroByScopeOut(**result)


@router.get("/macro/token-budget", response_model=MacroTokenBudgetOut)
async def macro_token_budget(
    utc_date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    _token: str = Depends(_require_ops_token),
) -> MacroTokenBudgetOut:
    """Daily LLM token counter from Redis."""
    out = OpsService.get_macro_token_budget(utc_date)
    return MacroTokenBudgetOut(**out)


@router.post("/macro/rerun", response_model=TriggerOut)
async def macro_rerun(
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
    actor: Optional[str] = Header(default=None, alias="X-Ops-Actor"),
) -> TriggerOut:
    result = await _run_trigger(svc, _MACRO_TASK, actor or "ops_console")
    return TriggerOut(**result.__dict__)


# ===========================================================================
# Phase 3 — P2-C Regime endpoints
# ===========================================================================

@router.get("/regime/current", response_model=RegimeCurrentOut)
async def regime_current(
    region: str = Query("USA", min_length=1),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> RegimeCurrentOut:
    """Live Redis read of current regime for ``region``."""
    result = await svc.get_regime_current(region=region)
    return RegimeCurrentOut(**result)


@router.get("/regime/snapshot", response_model=RegimeSnapshotOut)
async def regime_snapshot(
    region: str = Query("USA", min_length=1),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> RegimeSnapshotOut:
    """Full inference snapshot from Redis (fall back to today's archive)."""
    result = await svc.get_regime_snapshot(region=region)
    return RegimeSnapshotOut(**result)


@router.get("/regime/history")
async def regime_history(
    region: str = Query("USA", min_length=1),
    days: int = Query(14, ge=1, le=90),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> List[Dict[str, Any]]:
    """Per-day regime + pass_rate over ``days``, oldest first."""
    return await svc.get_regime_history(region=region, days=days)


@router.post("/regime/rerun", response_model=TriggerOut)
async def regime_rerun(
    region: Optional[str] = Query(None),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
    actor: Optional[str] = Header(default=None, alias="X-Ops-Actor"),
) -> TriggerOut:
    """Fire the daily regime-infer task. ``region`` is a hint only —
    the task iterates over all configured regions regardless."""
    result = await _run_trigger(svc, _REGIME_TASK, actor or "ops_console")
    return TriggerOut(**result.__dict__)


# ===========================================================================
# Phase 4 — LLM op hallucination monitor
# ===========================================================================

_LLM_OP_TASK = "backend.tasks.monitor_llm_op_hallucinations"


@router.get("/llm-op/latest", response_model=LLMOpLatestOut)
async def llm_op_latest(
    date_: Optional[date] = Query(None, alias="date"),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> LLMOpLatestOut:
    """Parse the daily LLM-op-monitor Markdown into KPI + lists.

    docs/llm_op_monitor/<date>.md is the historic format (designed for
    `cat`-ing in a terminal); we parse it server-side rather than
    refactoring the upstream task. Up to ARCHIVE_FALLBACK_DAYS days of
    fallback walk-back when today's file is missing.
    """
    result = await svc.get_llm_op_latest(date_)
    return LLMOpLatestOut(
        summary=LLMOpSummary(**result["summary"]),
        source=result["source"],
        stale_days=result.get("stale_days"),
        report_date=result.get("report_date"),
    )


@router.get("/llm-op/deactivated-kb")
async def llm_op_deactivated_kb(
    date_: Optional[date] = Query(None, alias="date"),
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
) -> List[Dict[str, Any]]:
    """Dedicated endpoint for the "affected KB entries" tab on the LLM-op
    page — alias of ``affected_entries`` from /latest.

    Same row schema as ``affected_entries`` in /latest; the monitor task
    (not this endpoint) is the source of truth for the actual
    is_active toggling on ``knowledge_entries``. The "deactivated" name
    is historical — the list contains all entries flagged in the latest
    monitor run, including ones whose row is still active because the
    task only logged them without auto-disable.
    """
    result = await svc.get_llm_op_latest(date_)
    return result["summary"].get("affected_entries", [])


@router.post("/llm-op/rerun", response_model=TriggerOut)
async def llm_op_rerun(
    _token: str = Depends(_require_ops_token),
    svc: OpsService = Depends(get_ops_service),
    actor: Optional[str] = Header(default=None, alias="X-Ops-Actor"),
) -> TriggerOut:
    """Fire the daily monitor_llm_op_hallucinations Celery task."""
    result = await _run_trigger(svc, _LLM_OP_TASK, actor or "ops_console")
    return TriggerOut(**result.__dict__)


# ---------------------------------------------------------------------------
# P3-Brain — BRAIN Consultant mode role switch (2026-05-16)
# ---------------------------------------------------------------------------
# Manual switch: user receives BRAIN upgrade email, flips flag in ConfigCenter
# (OpsBrainRoleCard in FeatureFlagsConsole). No auto-detection.
# See plan §10-§11 + brain_role_switch_service.py.

from backend.services.brain_role_switch_service import BrainRoleSwitchService


def get_brain_role_switch_service(
    db: AsyncSession = Depends(get_db),
    flag_service: FeatureFlagService = Depends(get_feature_flag_service),
) -> BrainRoleSwitchService:
    return BrainRoleSwitchService(db, flag_service)


class BrainRoleStateOut(BaseModel):
    mode: str                                     # "USER" | "CONSULTANT"
    effective_default_test_period: str
    effective_sharpe_submit_min: float
    effective_region_universes: Dict[str, str]
    running_tasks_count: int
    last_switched_at: Optional[str] = None        # ISO 8601 UTC w/ Z suffix
    last_switched_by: Optional[str] = None


class BrainRoleSwitchOut(BaseModel):
    mode: str
    note: Optional[str] = None
    actor: Optional[str] = None
    sync_enqueued: Optional[bool] = None


@router.get("/brain/role-state", response_model=BrainRoleStateOut)
async def brain_role_state(
    _token: str = Depends(_require_ops_token),
    svc: BrainRoleSwitchService = Depends(get_brain_role_switch_service),
) -> BrainRoleStateOut:
    """Current BRAIN mode + effective_* + last-switched timestamp."""
    state = await svc.get_state()
    return BrainRoleStateOut(**state)


@router.post("/brain/activate-consultant", response_model=BrainRoleSwitchOut)
async def brain_activate_consultant(
    _token: str = Depends(_require_ops_token),
    svc: BrainRoleSwitchService = Depends(get_brain_role_switch_service),
    actor: Optional[str] = Header(default=None, alias="X-Ops-Actor"),
) -> BrainRoleSwitchOut:
    """Flip ENABLE_BRAIN_CONSULTANT_MODE → True, clean multi-sim latch,
    enqueue global dataset sync. Should be called only after BRAIN sent the
    Consultant upgrade email (system does not auto-detect)."""
    result = await svc.activate_consultant_mode(actor=actor or "ops_console")
    return BrainRoleSwitchOut(**result)


@router.post("/brain/deactivate-consultant", response_model=BrainRoleSwitchOut)
async def brain_deactivate_consultant(
    _token: str = Depends(_require_ops_token),
    svc: BrainRoleSwitchService = Depends(get_brain_role_switch_service),
    actor: Optional[str] = Header(default=None, alias="X-Ops-Actor"),
) -> BrainRoleSwitchOut:
    """Flip ENABLE_BRAIN_CONSULTANT_MODE → False. Does NOT touch multi-sim
    latch (manual latch set would create invisible 24h perf cliff)."""
    result = await svc.deactivate_consultant_mode(actor=actor or "ops_console")
    return BrainRoleSwitchOut(**result)


# =============================================================================
# A+ BRAIN auth circuit breaker (2026-05-19)
# =============================================================================
# Inspect + manually clear the BRAIN_AUTH_CIRCUIT used by simulate_alpha /
# mining_tasks._run_one_round_inline. OPEN state means callers are
# fast-failing — no mining LLM cost burnt until ops clears or the 300s TTL
# auto-HALF_OPENs and the next call probes.


class BrainAuthCircuitOut(BaseModel):
    state: str
    until_ts: Optional[float] = None
    until_iso: Optional[str] = None
    last_failure_at: Optional[float] = None
    last_failure_iso: Optional[str] = None
    last_failure_reason: Optional[str] = None
    trip_count: int = 0
    seconds_until_half_open: int = 0


class BrainAuthCircuitClearOut(BaseModel):
    cleared: bool
    actor: Optional[str] = None


@router.get("/brain/auth-circuit-status", response_model=BrainAuthCircuitOut)
async def brain_auth_circuit_status(
    _token: str = Depends(_require_ops_token),
) -> BrainAuthCircuitOut:
    """Current BRAIN auth circuit state. CLOSED = normal; OPEN = callers
    fast-failing (no mining LLM cost burnt); HALF_OPEN = TTL elapsed, next
    real call will probe + flip CLOSED/OPEN based on outcome.

    Read from Redis-backed CircuitBreaker; soft-fails to CLOSED state on
    any Redis error so ops console never shows misleading OPEN state due
    to a Redis blip.
    """
    from backend.adapters.brain_adapter import BRAIN_AUTH_CIRCUIT
    return BrainAuthCircuitOut(**BRAIN_AUTH_CIRCUIT.status().to_dict())


@router.post("/brain/auth-circuit-clear", response_model=BrainAuthCircuitClearOut)
async def brain_auth_circuit_clear(
    _token: str = Depends(_require_ops_token),
    actor: Optional[str] = Header(default=None, alias="X-Ops-Actor"),
) -> BrainAuthCircuitClearOut:
    """Force-close the BRAIN auth circuit. Use after manual BRAIN re-auth
    (e.g. ops updated `.env` BRAIN_PASSWORD or BRAIN account was unlocked).
    Next BRAIN call will probe; if it 401s again the circuit auto-trips.

    Note: a successful authenticate() inside BrainAdapter ALSO calls
    .clear() — this endpoint is for when ops fixed the upstream root cause
    and wants to skip the 300s TTL wait.
    """
    from backend.adapters.brain_adapter import BRAIN_AUTH_CIRCUIT
    BRAIN_AUTH_CIRCUIT.clear(reason=f"ops_manual_by_{actor or 'unknown'}")
    return BrainAuthCircuitClearOut(cleared=True, actor=actor)


# ---------------------------------------------------------------------------
# Phase 4 Sprint 0 PR0 — LLM_API_CIRCUIT ops endpoints (2026-05-19)
# ---------------------------------------------------------------------------
# Mirrors the BRAIN auth-circuit endpoints above but for the LLM provider
# (DeepSeek/Anthropic) circuit. The two circuits are independent — LLM can
# be OPEN while BRAIN is CLOSED (e.g. DeepSeek outage but BRAIN auth ok)
# and vice versa.


class LLMApiCircuitOut(BaseModel):
    state: str
    until_ts: Optional[float] = None
    until_iso: Optional[str] = None
    last_failure_at: Optional[float] = None
    last_failure_iso: Optional[str] = None
    last_failure_reason: Optional[str] = None
    trip_count: int = 0
    seconds_until_half_open: int = 0


class LLMApiCircuitClearOut(BaseModel):
    cleared: bool
    actor: Optional[str] = None


@router.get("/llm/api-circuit-status", response_model=LLMApiCircuitOut)
async def llm_api_circuit_status(
    _token: str = Depends(_require_ops_token),
) -> LLMApiCircuitOut:
    """Current LLM provider (DeepSeek/Anthropic) API circuit state.
    CLOSED = normal; OPEN = LLM callers fast-failing (no LLM cost burnt during
    a provider outage); HALF_OPEN = TTL elapsed, next real LLM call will
    probe + flip CLOSED/OPEN based on outcome.

    Trip trigger: ``LLM_API_CIRCUIT_FAIL_THRESHOLD`` consecutive 5xx/timeout/
    connection-error within ``LLM_API_CIRCUIT_FAIL_WINDOW_SEC`` seconds.
    JSON parse / ValueError do NOT trip — only API-transport failures.
    """
    from backend.agents.services.llm_service import LLM_API_CIRCUIT
    return LLMApiCircuitOut(**LLM_API_CIRCUIT.status().to_dict())


@router.post("/llm/api-circuit-clear", response_model=LLMApiCircuitClearOut)
async def llm_api_circuit_clear(
    _token: str = Depends(_require_ops_token),
    actor: Optional[str] = Header(default=None, alias="X-Ops-Actor"),
) -> LLMApiCircuitClearOut:
    """Force-close the LLM API circuit. Use after manual confirmation that
    the LLM provider has recovered (e.g. DeepSeek/Anthropic status page back
    to green) and ops wants to skip the 300s TTL wait.

    Note: any successful LLMService.call() inside the worker process ALSO
    clears the circuit on probe (HALF_OPEN → CLOSED). This endpoint is for
    immediate manual recovery.
    """
    from backend.agents.services.llm_service import LLM_API_CIRCUIT
    LLM_API_CIRCUIT.clear(reason=f"ops_manual_by_{actor or 'unknown'}")
    return LLMApiCircuitClearOut(cleared=True, actor=actor)


# ---------------------------------------------------------------------------
# Phase 4 Sprint 1 A1.2 — R12 LLM_MODE sentinel restore endpoint (2026-05-20)
# ---------------------------------------------------------------------------


class RestoreSentinelOut(BaseModel):
    sentinel_for: str
    restored_flags: List[str]
    skipped: List[str]
    # F3 (S1-B fix): reason per skipped flag — typically
    # 'operator_manual_intervention' when restore deferred to keep
    # operator's manual override after the cascade fired.
    skipped_reasons: Dict[str, str] = Field(default_factory=dict)
    audit_rows: int
    # F2 (S1-A Seam 1 fix): how many RUNNING/PAUSED/PENDING tasks had
    # their cross-mode residue keys drained (g5_pending_offspring etc).
    drained_tasks: int = 0
    drained_keys_total: int = 0
    actor: Optional[str] = None


# ---------------------------------------------------------------------------
# Phase 4 Sprint 1 A1.4 — R12 LLM_MODE comparison + GO gate (2026-05-20)
# ---------------------------------------------------------------------------


class LLMModeBucket(BaseModel):
    total: int
    pass_count: int = Field(alias="pass")
    rate: float
    sharpe_mean: float
    sharpe_count: int

    class Config:
        populate_by_name = True


class LLMModeComparisonOut(BaseModel):
    window_days: int
    region_filter: Optional[str] = None
    total_alphas: int
    by_mode: Dict[str, LLMModeBucket]
    by_region_mode: Dict[str, Dict[str, LLMModeBucket]]
    by_template: Dict[str, LLMModeBucket]
    assistant_fallthrough_count: int


class LLMModeGoGateOut(BaseModel):
    decision: str  # GO | NO-GO | PARTIAL | INSUFFICIENT | ERROR
    rationale: str
    stats: Optional[Dict[str, Any]] = None
    thresholds: Optional[Dict[str, Any]] = None


@router.get("/llm-mode/comparison", response_model=LLMModeComparisonOut)
async def llm_mode_comparison(
    days: int = Query(default=30, ge=1, le=365),
    region: Optional[str] = Query(default=None),
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> LLMModeComparisonOut:
    """A1.4: PASS-rate distribution stratified by (author/assistant) +
    region + assistant_template_id over the last ``days`` days.

    Powers the operator's pre-decision view BEFORE polling the GO gate.
    No decision is taken here — just the raw stats.

    Reads ``alpha.metrics['llm_mode_used']`` (planted by A1.1 via
    workflow.py initial_state) and ``alpha.metrics['assistant_template_id']``
    (planted by A1.3 per-alpha branch in node_code_gen). Alphas
    pre-dating A1.1 deploy have no llm_mode_used field — counted as
    'author' (which is what they actually were).
    """
    from backend.services.llm_mode_comparison import query_mode_pool
    result = await query_mode_pool(db, days=days, region=region)
    if result.get("error"):
        raise HTTPException(
            status_code=500,
            detail=f"comparison query failed: {result['error']}",
        )

    def _to_bucket(b: dict) -> LLMModeBucket:
        return LLMModeBucket(
            total=int(b.get("total", 0)),
            pass_count=int(b.get("pass", 0)),
            rate=float(b.get("rate", 0.0)),
            sharpe_mean=float(b.get("sharpe_mean", 0.0)),
            sharpe_count=int(b.get("sharpe_count", 0)),
        )

    return LLMModeComparisonOut(
        window_days=result["window_days"],
        region_filter=result["region_filter"],
        total_alphas=result["total_alphas"],
        by_mode={k: _to_bucket(v) for k, v in result["by_mode"].items()},
        by_region_mode={
            r: {m: _to_bucket(v) for m, v in mode_dict.items()}
            for r, mode_dict in result["by_region_mode"].items()
        },
        by_template={t: _to_bucket(v) for t, v in result["by_template"].items()},
        assistant_fallthrough_count=int(result["assistant_fallthrough_count"]),
    )


@router.get("/llm-mode/go-gate", response_model=LLMModeGoGateOut)
async def llm_mode_go_gate(
    days: int = Query(default=30, ge=1, le=365),
    region: Optional[str] = Query(default=None),
    effect_floor_pct_pts: float = Query(default=-0.10),
    iterations: int = Query(default=1000, ge=100, le=10000),
    ci_level: float = Query(default=0.80, ge=0.5, le=0.99),
    seed: Optional[int] = Query(default=None),
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> LLMModeGoGateOut:
    """A1.4: apply the R12 GO/NO-GO/PARTIAL gate on the current
    comparison window.

    Decision rules (plan v5 §6.1):
      - effect ≤ floor OR upper CI < floor → NO-GO
      - effect > floor AND lower CI > 0    → GO
      - otherwise                          → PARTIAL (or INSUFFICIENT)

    ``effect_floor_pct_pts`` defaults to -0.10pp (assistant ≥ author -
    10 percentage points). For production R12 decision, leave at default;
    for debugging or per-region exploration, operator can override.

    ``seed`` is optional — set in tests / debugging to reproduce CI.
    """
    from backend.services.llm_mode_comparison import (
        query_mode_pool, evaluate_go_gate,
    )
    comparison = await query_mode_pool(db, days=days, region=region)
    decision = evaluate_go_gate(
        comparison,
        effect_floor_pct_pts=effect_floor_pct_pts,
        iterations=iterations,
        ci_level=ci_level,
        seed=seed,
    )
    return LLMModeGoGateOut(**decision)


@router.post("/llm-mode/restore-sentinel", response_model=RestoreSentinelOut)
async def llm_mode_restore_sentinel(
    _token: str = Depends(_require_ops_token),
    actor: Optional[str] = Header(default=None, alias="X-Ops-Actor"),
    flag_service: FeatureFlagService = Depends(get_feature_flag_service),
) -> RestoreSentinelOut:
    """Reverse the most-recent R12 sentinel cascade.

    When ``ENABLE_LLM_ASSISTANT_MODE`` was set True, the feature flag
    service forced the 6 ``LLM_ASSISTANT_SENTINEL_FLAGS`` (R1b mutate,
    G5 crossover, G8 forest reuse, R8 L0, G3 originality, R9 sim cache)
    to False so author-mode mechanisms wouldn't fire under an
    assistant-mode hypothesis. This endpoint reads
    ``feature_flag_audit.sentinel_trigger_for='ENABLE_LLM_ASSISTANT_MODE' AND
    restored_at IS NULL`` and reverts each forced flip to its prior state
    (DELETE the override if there was none before, UPSERT back to the
    prior value otherwise) in a single transaction.

    Idempotent — stamps ``restored_at`` on the matched audit rows so a
    second call returns audit_rows=0.

    Operator runbook:
      - Use this after deciding R12 assistant mode obs window failed and
        you want to immediately restore the 6 sentinel flags to their
        pre-R12 production state.
      - Setting ``ENABLE_LLM_ASSISTANT_MODE=False`` via the regular
        /ops/flags PATCH endpoint does NOT auto-restore — that just
        turns off the kill switch. This endpoint is the explicit
        "give me my sentinel flags back" lever.
    """
    actor_str = actor or "ops_console"
    result = await flag_service.restore_sentinel(
        sentinel_for="ENABLE_LLM_ASSISTANT_MODE",
        actor=actor_str,
        note=f"ops endpoint restore by {actor_str}",
    )
    return RestoreSentinelOut(
        sentinel_for=result["sentinel_for"],
        restored_flags=result["restored_flags"],
        skipped=result["skipped"],
        skipped_reasons=result.get("skipped_reasons", {}),
        audit_rows=result["audit_rows"],
        drained_tasks=result.get("drained_tasks", 0),
        drained_keys_total=result.get("drained_keys_total", 0),
        actor=actor,
    )


# ---------------------------------------------------------------------------
# Flat session admin endpoints (post tier-system removal, 2026-05-18)
# ---------------------------------------------------------------------------
# Post tier-system removal cascade is permanently retired — the prior
# cascade_deprecation_readiness + cascade_drain endpoints (read/wrote the
# dropped task.mining_mode column) are deleted along with the parallel
# CONTINUOUS_CASCADE concept. Flat sessions are the only continuous path.

from backend.services.task_service import TaskService  # noqa: E402  (intentional late import)


def get_task_service_ops(db: AsyncSession = Depends(get_db)) -> TaskService:
    """Inject TaskService for ops endpoints — mirrors routers/tasks.py:get_task_service."""
    return TaskService(db)


class StartFlatSessionIn(BaseModel):
    region: str = Field(default="USA", description="BRAIN region (USA/CHN/EUR/ASI/GLB)")
    universe: str = Field(default="TOP3000", description="Region universe")
    datasets: List[str] = Field(
        default_factory=list,
        description="Explicit dataset list; empty = AUTO-pick",
    )


class FlatSessionOut(BaseModel):
    task_id: int
    region: str
    universe: str
    status: str
    runtime_state_inherited: bool = False


@router.post("/start-flat-session", response_model=FlatSessionOut)
async def start_flat_session(
    payload: StartFlatSessionIn,
    _token: str = Depends(_require_ops_token),
    svc: TaskService = Depends(get_task_service_ops),
    db: AsyncSession = Depends(get_db),
    actor: Optional[str] = Header(default=None, alias="X-Ops-Actor"),
) -> FlatSessionOut:
    """Create a new flat mining session and dispatch its worker.

    Gated by ``settings.ENABLE_FLAT_CONTINUOUS`` — returns 400 when flag is
    OFF so the caller knows to flip the flag first.

    Phase 4 Sprint 1 A3 (2026-05-19): flat-F4 cross-region quota guard.
    Before dispatching, computes last-N-day region share + checks whether
    adding this new task would push ``payload.region`` over its
    FLAT_CROSS_REGION_QUOTA cap. ENFORCE=True → reject with 400; ENFORCE=
    False (default) → warn-log only and proceed (observation window).
    Soft-fail: any DB error → skip the check + warn log + proceed.
    """
    from backend.config import settings  # local import — settings hot-reads flag overrides
    if not getattr(settings, "ENABLE_FLAT_CONTINUOUS", False):
        raise HTTPException(
            status_code=400,
            detail="ENABLE_FLAT_CONTINUOUS flag is OFF — flip via PATCH /ops/flags/ENABLE_FLAT_CONTINUOUS first",
        )

    # ---- A3 flat-F4 cross-region quota guard ----
    try:
        from backend.services.flat_region_quota import (
            compute_region_share as _compute_share,
            check_quota as _check_quota,
        )
        _quota = dict(getattr(settings, "FLAT_CROSS_REGION_QUOTA", {}) or {})
        _enforce = bool(getattr(settings, "FLAT_CROSS_REGION_ENFORCE", False))
        _lookback = int(getattr(settings, "FLAT_CROSS_REGION_LOOKBACK_DAYS", 30))
        # Quota empty → operator hasn't configured caps; nothing to guard against.
        if _quota:
            _share_now = await _compute_share(db, lookback_days=_lookback)
            _decision = _check_quota(
                new_region=payload.region,
                current_share=_share_now,
                quota=_quota,
            )
            if _decision.get("would_exceed"):
                _msg = (
                    f"flat-F4 quota check: region={payload.region} "
                    f"projected_share={_decision['projected_share']:.3f} "
                    f"> quota={_decision['quota']:.3f} "
                    f"(current_count={_decision['current_count']}, "
                    f"projected_total={_decision['projected_total']})"
                )
                if _enforce:
                    raise HTTPException(status_code=400, detail=_msg)
                from loguru import logger as _f4_logger
                _f4_logger.warning("[flat-F4 warn-only] {}", _msg)
    except HTTPException:
        raise  # ENFORCE=True trip propagates
    except Exception as _f4_ex:  # noqa: BLE001
        from loguru import logger as _f4_logger
        _f4_logger.warning(
            "[flat-F4] quota check failed (non-fatal, proceeding): {}", _f4_ex,
        )

    try:
        info = await svc.start_flat_session(
            region=payload.region,
            universe=payload.universe,
            datasets=payload.datasets or None,
        )
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex
    return FlatSessionOut(
        task_id=info.task_id,
        region=info.region,
        universe=info.universe,
        status=info.status,
        runtime_state_inherited=False,
    )


# ----- A3 flat-F4 distribution endpoint -----
class FlatRegionStatus(BaseModel):
    region: str
    count: int
    share: float
    quota: Optional[float] = None
    status: str  # ok / warn / exceeded / no_quota


class FlatRegionDistributionOut(BaseModel):
    total_active_tasks: int
    regions: List[FlatRegionStatus]
    enforce: bool
    lookback_days: int


@router.get("/flat-region/distribution", response_model=FlatRegionDistributionOut)
async def flat_region_distribution(
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> FlatRegionDistributionOut:
    """A3 flat-F4: per-region active-task share + configured quota with
    over-quota / warn / ok chips. Powers the frontend FlatRegionMonitor
    page (defer to next session) and the operator decision to flip
    FLAT_CROSS_REGION_ENFORCE from warn → reject.
    """
    from backend.config import settings
    from backend.services.flat_region_quota import (
        compute_region_share as _compute_share,
        build_distribution_summary as _summary,
    )
    _quota = dict(getattr(settings, "FLAT_CROSS_REGION_QUOTA", {}) or {})
    _enforce = bool(getattr(settings, "FLAT_CROSS_REGION_ENFORCE", False))
    _lookback = int(getattr(settings, "FLAT_CROSS_REGION_LOOKBACK_DAYS", 30))
    _share = await _compute_share(db, lookback_days=_lookback)
    _summary_dict = _summary(_share, _quota)
    return FlatRegionDistributionOut(
        total_active_tasks=int(_summary_dict["total_active_tasks"]),
        regions=[FlatRegionStatus(**r) for r in _summary_dict["regions"]],
        enforce=_enforce,
        lookback_days=_lookback,
    )


@router.post("/flat-sessions/{task_id}/resume", response_model=FlatSessionOut)
async def resume_flat_session(
    task_id: int,
    _token: str = Depends(_require_ops_token),
    svc: TaskService = Depends(get_task_service_ops),
    actor: Optional[str] = Header(default=None, alias="X-Ops-Actor"),
) -> FlatSessionOut:
    """Resume a paused flat session (preserves runtime_state['flat_cursor'])."""
    try:
        info = await svc.resume_flat_session(task_id)
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex
    return FlatSessionOut(
        task_id=info.task_id,
        region=info.region,
        universe=info.universe,
        status=info.status,
        runtime_state_inherited=True,
    )


@router.post("/flat-sessions/{task_id}/pause", response_model=FlatSessionOut)
async def pause_flat_session(
    task_id: int,
    _token: str = Depends(_require_ops_token),
    svc: TaskService = Depends(get_task_service_ops),
    actor: Optional[str] = Header(default=None, alias="X-Ops-Actor"),
) -> FlatSessionOut:
    """Pause a RUNNING flat session (sets status→PAUSED; worker exits at next
    round boundary). FLAT counterpart to /tasks/{id}/intervene which refuses
    FLAT PAUSE because it does not dispatch/manage the flat worker."""
    try:
        info = await svc.pause_flat_session(task_id)
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex
    return FlatSessionOut(
        task_id=info.task_id,
        region=info.region,
        universe=info.universe,
        status=info.status,
        runtime_state_inherited=True,
    )


# =============================================================================
# R1b CoSTEER loop telemetry (2026-05-18) — operator decision support
# =============================================================================
# Surfaces r1b_retry_log aggregations + Hypothesis chain depth distribution
# so operators flipping ENABLE_R1B_* flags have data — not raw SQL — to
# observe retry/mutate success rates, per-task budget consumption, and
# CoSTEER chain growth before promoting flags to default-ON.


class R1bAttemptStatsOut(BaseModel):
    attempt_type: str  # 'retry_impl' | 'mutate_hyp'
    outcome: str       # 'pending' | 'pass' | 'fail' | 'budget_exhausted' | ...
    count: int
    total_cost_usd: float
    total_tokens_used: int


class R1bBudgetLedgerOut(BaseModel):
    task_id: int
    retries_total: int
    mutations_total: int
    cost_usd_total: float


class R1bTelemetryOut(BaseModel):
    flags: Dict[str, bool]
    attempt_stats: List[R1bAttemptStatsOut]
    success_rate_retry_impl: float  # pass / (pass + fail) for attempt_type=retry_impl
    success_rate_mutate_hyp: float
    top_tasks_by_budget: List[R1bBudgetLedgerOut]
    window_days: int
    total_attempts_in_window: int


@router.get("/r1b/telemetry", response_model=R1bTelemetryOut)
async def r1b_telemetry(
    days: int = 7,
    top_n: int = 5,
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> R1bTelemetryOut:
    """R1b retry/mutate telemetry for operator decision support.

    Aggregates ``r1b_retry_log`` over the last ``days`` window by
    (attempt_type, outcome) + computes pass-rate per attempt_type. Also
    pulls the top ``top_n`` tasks by accumulated R1b budget consumption
    from ``MiningTask.config['r1b_loop_budget_consumed']``.

    Use this BEFORE flipping any R1b flag to default-ON so the GO gate
    decision is evidence-based (per plan §10 deploy sequence).
    """
    from sqlalchemy import text as _text
    from backend.config import settings as _stg

    flags = {
        "ENABLE_R1B_RETRY_LOOP": bool(getattr(_stg, "ENABLE_R1B_RETRY_LOOP", False)),
        "ENABLE_R1B_HYPOTHESIS_MUTATE": bool(getattr(_stg, "ENABLE_R1B_HYPOTHESIS_MUTATE", False)),
        "ENABLE_R1B_FAILURE_TREE": bool(getattr(_stg, "ENABLE_R1B_FAILURE_TREE", False)),
        "ENABLE_R1B_TYPED_PIPELINE": bool(getattr(_stg, "ENABLE_R1B_TYPED_PIPELINE", False)),
        "ENABLE_R1B_DAG_RETRY_REWARD": bool(getattr(_stg, "ENABLE_R1B_DAG_RETRY_REWARD", False)),
    }

    # Aggregation 1: per-attempt-type per-outcome counts + cost/token sums.
    # COALESCE keeps NULL outcome rows (in-flight) visible as a distinct
    # bucket — operators want to see pending volume too.
    stat_rows = (await db.execute(_text(
        "SELECT attempt_type, COALESCE(outcome, 'unknown') AS outcome, "
        "       COUNT(*) AS n, "
        "       COALESCE(SUM(llm_cost_usd), 0.0) AS cost, "
        "       COALESCE(SUM(llm_tokens_used), 0) AS toks "
        "FROM r1b_retry_log "
        "WHERE created_at > now() - (:days || ' day')::interval "
        "GROUP BY attempt_type, outcome "
        "ORDER BY attempt_type, n DESC"
    ), {"days": str(int(days))})).all()

    attempt_stats: List[R1bAttemptStatsOut] = []
    pass_retry = fail_retry = 0
    pass_mutate = fail_mutate = 0
    total_in_window = 0
    for at, oc, n, cost, toks in stat_rows:
        n_int = int(n or 0)
        attempt_stats.append(R1bAttemptStatsOut(
            attempt_type=at or "unknown",
            outcome=oc or "unknown",
            count=n_int,
            total_cost_usd=float(cost or 0.0),
            total_tokens_used=int(toks or 0),
        ))
        total_in_window += n_int
        if at == "retry_impl":
            if oc == "pass":
                pass_retry += n_int
            elif oc == "fail":
                fail_retry += n_int
        elif at == "mutate_hyp":
            if oc == "pass":
                pass_mutate += n_int
            elif oc == "fail":
                fail_mutate += n_int

    def _rate(p: int, f: int) -> float:
        denom = p + f
        return round(p / denom, 4) if denom > 0 else 0.0

    # Aggregation 2: top N tasks by accumulated R1b budget ledger.
    # JSONB extract is null-safe — tasks without an R1b ledger fall through
    # the WHERE filter rather than landing as zero rows.
    budget_rows = (await db.execute(_text(
        "SELECT id, "
        "       COALESCE((config->'r1b_loop_budget_consumed'->>'retries_total')::int, 0) AS retries, "
        "       COALESCE((config->'r1b_loop_budget_consumed'->>'mutations_total')::int, 0) AS mutations, "
        "       COALESCE((config->'r1b_loop_budget_consumed'->>'cost_usd_total')::float, 0.0) AS cost "
        "FROM mining_tasks "
        "WHERE config ? 'r1b_loop_budget_consumed' "
        "ORDER BY cost DESC NULLS LAST "
        "LIMIT :n"
    ), {"n": int(top_n)})).all()

    top_tasks = [
        R1bBudgetLedgerOut(
            task_id=int(tid),
            retries_total=int(r or 0),
            mutations_total=int(m or 0),
            cost_usd_total=float(c or 0.0),
        )
        for tid, r, m, c in budget_rows
    ]

    return R1bTelemetryOut(
        flags=flags,
        attempt_stats=attempt_stats,
        success_rate_retry_impl=_rate(pass_retry, fail_retry),
        success_rate_mutate_hyp=_rate(pass_mutate, fail_mutate),
        top_tasks_by_budget=top_tasks,
        window_days=int(days),
        total_attempts_in_window=total_in_window,
    )


class R1bChainDepthBucketOut(BaseModel):
    mutation_depth: int
    hypothesis_count: int


class R1bChainDepthOut(BaseModel):
    distribution: List[R1bChainDepthBucketOut]
    max_depth_observed: int
    total_mutated_hypotheses: int
    total_root_hypotheses: int
    chain_depth_avg: float
    # Cap-firing surface (review LOW 3, 2026-05-18) — saves operators
    # from eyeballing the distribution against the configured cap.
    # tasks_at_or_above_cap_count = COUNT(hypotheses where r1b_mutation_depth
    # >= R1B_MAX_MUTATION_DEPTH); paired with the setting value so the
    # dashboard can render "X / N tasks at depth ≥ Y" directly.
    tasks_at_or_above_cap_count: int
    r1b_max_mutation_depth_setting: int


@router.get("/r1b/chain-depth-distribution", response_model=R1bChainDepthOut)
async def r1b_chain_depth_distribution(
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> R1bChainDepthOut:
    """R1b.3-v2 CoSTEER chain growth distribution.

    Returns histogram of ``hypotheses.r1b_mutation_depth`` (0 = exploration
    root from node_hypothesis LLM; >0 = mutated descendant from
    node_hypothesis_mutate). Use to confirm the mutation chain is actually
    growing past depth=1 after R1b.3-v2 flag promotion.

    A healthy R1b deploy shows distribution {0: N, 1: M, 2: K, ...} with
    geometric decay — depth=0 dominant, depths>0 declining but non-zero
    confirming chain walk per plan §7.2.
    """
    from sqlalchemy import text as _text

    rows = (await db.execute(_text(
        "SELECT COALESCE(r1b_mutation_depth, 0) AS d, COUNT(*) AS n "
        "FROM hypotheses "
        "GROUP BY d "
        "ORDER BY d ASC"
    ))).all()

    buckets = [
        R1bChainDepthBucketOut(
            mutation_depth=int(d or 0),
            hypothesis_count=int(n or 0),
        )
        for d, n in rows
    ]
    total = sum(b.hypothesis_count for b in buckets)
    roots = next((b.hypothesis_count for b in buckets if b.mutation_depth == 0), 0)
    mutated = total - roots
    max_depth = max((b.mutation_depth for b in buckets), default=0)
    # Weighted avg depth across ALL hypotheses (roots count as 0)
    weighted_sum = sum(b.mutation_depth * b.hypothesis_count for b in buckets)
    avg = round(weighted_sum / total, 4) if total > 0 else 0.0

    # Cap-firing surface (review LOW 3) — sum buckets at or above the
    # configured cap so the dashboard can render "X / N at depth ≥ Y"
    # without round-trip math. Cheap: reuse the already-aggregated
    # buckets, no second SQL hit.
    from backend.config import settings as _stg
    cap = int(getattr(_stg, "R1B_MAX_MUTATION_DEPTH", 3))
    at_or_above_cap = sum(
        b.hypothesis_count for b in buckets if b.mutation_depth >= cap
    )

    return R1bChainDepthOut(
        distribution=buckets,
        max_depth_observed=max_depth,
        total_mutated_hypotheses=mutated,
        total_root_hypotheses=roots,
        chain_depth_avg=avg,
        tasks_at_or_above_cap_count=at_or_above_cap,
        r1b_max_mutation_depth_setting=cap,
    )


# =============================================================================
# R1a hook + R5 LLM-judge telemetry (2026-05-18) — operator decision support
# =============================================================================
# Replaces the standalone scripts/r1a_attribution_report.py with a live
# endpoint. R1a has been production-ON for months but operators have only
# raw SQL to see attribution distribution + R5 c1/c2 agreement rates.
# Plan §1.7 + feedback_no_reflex_flag_cleanup memory: R1a flag stays ON
# long-term — telemetry needs to be a permanent ops endpoint, not a
# one-shot diagnostic script.


class R1aAttributionBucketOut(BaseModel):
    attribution: str          # 'hypothesis' | 'implementation' | 'both' | 'unknown' | 'null'
    count: int
    errs_count: int           # rows with hook_error set (telemetry of self-caught failures)
    avg_confidence: float     # mean attribution_confidence within bucket


class R1aTelemetryOut(BaseModel):
    flags: Dict[str, bool]
    distribution: List[R1aAttributionBucketOut]
    total_in_window: int
    # KPI per plan §1.7 (R1a Phase 0 GO gate definitions)
    non_null_pct: float       # hook produced any attribution / total
    non_unknown_pct: float    # actionable attribution / non-null
    errs_count_total: int
    # R5 LLM judge stats (NULL when ENABLE_LLM_JUDGE=False, populated otherwise)
    r5_agrees_r1a_pct: Optional[float]   # rows where r5_agrees_r1a='true' / rows where field non-null
    r5_avg_composite_score: Optional[float]
    r5_total_cost_usd: float
    r5_sample_size: int        # rows with non-null r5_composite_score
    window_days: int


@router.get("/r1a/telemetry", response_model=R1aTelemetryOut)
async def r1a_telemetry(
    days: int = 7,
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> R1aTelemetryOut:
    """R1a hook + R5 LLM judge telemetry — replaces r1a_attribution_report.py.

    Aggregates ``r1a_attribution_log`` over the last ``days`` window:
      - per-attribution distribution + counts + errs + avg confidence
      - non_null_pct + non_unknown_pct per plan §1.7 GO gate
      - R5 c1/c2 stats (agreement rate + avg composite + total cost)

    Use to confirm R1a is healthy + R5 judge is producing actionable
    attribution before promoting R1b retry/mutate flags.
    """
    from sqlalchemy import text as _text
    from backend.config import settings as _stg

    flags = {
        "ENABLE_R1A_HOOK": bool(getattr(_stg, "ENABLE_R1A_HOOK", False)),
        "ENABLE_LLM_JUDGE": bool(getattr(_stg, "ENABLE_LLM_JUDGE", False)),
    }

    # Per-attribution bucket — COALESCE NULL → 'null' string so the bucket
    # shows in distribution even when hook fully fails. errs_count counts
    # rows with hook_error set (hook self-caught exceptions, not crashes).
    rows = (await db.execute(_text(
        "SELECT COALESCE(attribution, 'null') AS attr, "
        "       COUNT(*) AS n, "
        "       COUNT(*) FILTER (WHERE hook_error IS NOT NULL) AS errs, "
        "       COALESCE(AVG(attribution_confidence), 0.0) AS avg_conf "
        "FROM r1a_attribution_log "
        "WHERE created_at > now() - (:days || ' day')::interval "
        "GROUP BY attr "
        "ORDER BY n DESC"
    ), {"days": str(int(days))})).all()

    distribution: List[R1aAttributionBucketOut] = []
    total = 0
    non_null = 0
    actionable = 0  # hypothesis | implementation | both
    errs_total = 0
    for attr, n, errs, avg_conf in rows:
        n_int = int(n or 0)
        e_int = int(errs or 0)
        distribution.append(R1aAttributionBucketOut(
            attribution=attr or "null",
            count=n_int,
            errs_count=e_int,
            avg_confidence=round(float(avg_conf or 0.0), 4),
        ))
        total += n_int
        errs_total += e_int
        if attr and attr != "null":
            non_null += n_int
        if attr in ("hypothesis", "implementation", "both"):
            actionable += n_int

    non_null_pct = round(non_null / total, 4) if total > 0 else 0.0
    non_unknown_pct = round(actionable / non_null, 4) if non_null > 0 else 0.0

    # R5 stats — populated when at least one row carries non-null
    # r5_composite_score. Costs are total USD (not average) so operators
    # see cumulative spend in the window.
    r5_rows = (await db.execute(_text(
        "SELECT "
        "  COUNT(*) FILTER (WHERE r5_agrees_r1a = 'true') AS agree, "
        "  COUNT(*) FILTER (WHERE r5_agrees_r1a IS NOT NULL) AS r5_total, "
        "  COALESCE(AVG(r5_composite_score), 0.0) AS avg_score, "
        "  COALESCE(SUM(r5_cost_usd), 0.0) AS cost, "
        "  COUNT(*) FILTER (WHERE r5_composite_score IS NOT NULL) AS sample "
        "FROM r1a_attribution_log "
        "WHERE created_at > now() - (:days || ' day')::interval"
    ), {"days": str(int(days))})).one()

    agree_n, r5_total, avg_score, r5_cost, sample = r5_rows
    sample_int = int(sample or 0)
    r5_agrees_pct = None
    r5_avg_score = None
    if int(r5_total or 0) > 0:
        r5_agrees_pct = round(int(agree_n or 0) / int(r5_total), 4)
    if sample_int > 0:
        r5_avg_score = round(float(avg_score or 0.0), 4)

    return R1aTelemetryOut(
        flags=flags,
        distribution=distribution,
        total_in_window=total,
        non_null_pct=non_null_pct,
        non_unknown_pct=non_unknown_pct,
        errs_count_total=errs_total,
        r5_agrees_r1a_pct=r5_agrees_pct,
        r5_avg_composite_score=r5_avg_score,
        r5_total_cost_usd=round(float(r5_cost or 0.0), 4),
        r5_sample_size=sample_int,
        window_days=int(days),
    )


# =============================================================================
# G3 AST originality gate telemetry (Phase A shadow, 2026-05-19)
# =============================================================================
# Surfaces the shadow-mode block rate so operators can calibrate τ before
# promoting AST_ORIGINALITY_MODE to 'soft' / 'hard'. Reads two sources:
#   - ast_distance_log: 7d block rate against the current τ
#                       + min_distance histogram + top-N nearest-neighbor
#                       + per-region distribution. (R3/Q8 Phase 1 table.)
#   - alphas.metrics:   per-pillar block rate using the JSONB tag
#                       _g3_ast_originality_blocked stamped by
#                       backend.alpha_originality.apply_to_alpha (G3 Phase A).
# Both queries soft-fall to empty buckets if the underlying tables are
# unavailable. No HTTP self-call.


class G3DistanceHistogramBucketOut(BaseModel):
    # Half-open [lo, hi) buckets — 0.0..1.0 stepped by AST_ORIGINALITY_MIN_DISTANCE
    lo: float
    hi: float
    count: int


class G3NeighborBucketOut(BaseModel):
    nearest_neighbor_hash: str
    blocked_count: int


class G3PillarBucketOut(BaseModel):
    pillar: str
    blocked: int
    total: int
    block_rate: float


class G3OriginalityStatsOut(BaseModel):
    flags: Dict[str, bool]
    mode: str
    threshold: float
    window_days: int
    # Distance log aggregate (one row per code-gen candidate, R3/Q8)
    total_candidates: int          # rows in ast_distance_log window
    blocked_candidates: int        # rows where ast_distance_min < τ
    block_rate: float              # blocked_candidates / total_candidates
    # min_distance histogram for τ calibration
    distance_histogram: List[G3DistanceHistogramBucketOut]
    # Top-N nearest-neighbor hashes (shows the "换皮 magnet" alphas)
    top_neighbors: List[G3NeighborBucketOut]
    # Per-pillar block rate from alphas.metrics (post-gate signal)
    by_pillar: List[G3PillarBucketOut]


@router.get("/g3/originality-stats", response_model=G3OriginalityStatsOut)
async def g3_originality_stats(
    days: int = 7,
    histogram_bins: int = 10,
    top_neighbors: int = 10,
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> G3OriginalityStatsOut:
    """G3 Phase A shadow-mode stats — read before promoting τ / mode.

    KPIs answered:
      - block_rate vs the *current* τ: would the gate reject too many
        alphas if flipped to soft/hard?
      - min_distance histogram: where does the natural cluster sit?
        (operator targets a τ that catches the bottom ~5-10%)
      - top_neighbors: which historical alphas are getting "copied" the
        most — those are the AST-isomorphism magnets
      - by_pillar: which pillar is most saturated? (high block rate =
        next pillar to push diversity in)

    Soft-fails to empty buckets when ast_distance_log is empty (Phase 1
    flag was OFF) or when alphas.metrics has no _g3_* tags yet (Phase A
    flag was OFF / freshly flipped).
    """
    from sqlalchemy import text as _text
    from backend.config import settings as _stg

    threshold = float(getattr(_stg, "AST_ORIGINALITY_MIN_DISTANCE", 0.15))
    mode = str(getattr(_stg, "AST_ORIGINALITY_MODE", "shadow") or "shadow")
    bins = max(2, min(int(histogram_bins), 50))   # clamp to sane range
    top_n = max(1, min(int(top_neighbors), 100))
    flags = {
        "ENABLE_AST_ORIGINALITY_GATE": bool(getattr(_stg, "ENABLE_AST_ORIGINALITY_GATE", False)),
        "ENABLE_AST_DIVERSITY_DIM": bool(getattr(_stg, "ENABLE_AST_DIVERSITY_DIM", False)),
    }

    # --- 1. Distance log: total + blocked at current τ ---
    total_candidates = 0
    blocked_candidates = 0
    try:
        row = (await db.execute(_text(
            "SELECT "
            "  COUNT(*) AS total, "
            "  COUNT(*) FILTER (WHERE ast_distance_min < :tau) AS blocked "
            "FROM ast_distance_log "
            "WHERE created_at > now() - (:days || ' day')::interval "
            "  AND ast_distance_min IS NOT NULL"
        ), {"tau": threshold, "days": str(int(days))})).one()
        total_candidates = int(row[0] or 0)
        blocked_candidates = int(row[1] or 0)
    except Exception:
        # Soft-fail — table missing or driver issue. Leave at zero.
        pass

    block_rate = (
        round(blocked_candidates / total_candidates, 4)
        if total_candidates > 0 else 0.0
    )

    # --- 2. min_distance histogram ---
    # bin width = 1.0/bins (distance is bounded [0,1]). Use generate_series
    # so empty buckets still appear (operator wants a continuous histogram).
    distance_histogram: List[G3DistanceHistogramBucketOut] = []
    try:
        hist_rows = (await db.execute(_text(
            "WITH bins AS ( "
            "  SELECT generate_series(0, :bins - 1) AS i "
            ") "
            "SELECT "
            "  (i * (1.0 / :bins))::float AS lo, "
            "  ((i + 1) * (1.0 / :bins))::float AS hi, "
            "  COALESCE(( "
            "    SELECT COUNT(*) FROM ast_distance_log "
            "    WHERE ast_distance_min IS NOT NULL "
            "      AND ast_distance_min >= (i * (1.0 / :bins)) "
            "      AND (CASE WHEN i = :bins - 1 "
            "                THEN ast_distance_min <= ((i + 1) * (1.0 / :bins)) "
            "                ELSE ast_distance_min <  ((i + 1) * (1.0 / :bins)) END) "
            "      AND created_at > now() - (:days || ' day')::interval "
            "  ), 0) AS c "
            "FROM bins ORDER BY i"
        ), {"bins": int(bins), "days": str(int(days))})).all()
        for lo, hi, c in hist_rows:
            distance_histogram.append(G3DistanceHistogramBucketOut(
                lo=round(float(lo or 0.0), 4),
                hi=round(float(hi or 0.0), 4),
                count=int(c or 0),
            ))
    except Exception:
        # Fallback: equal-width empty histogram so the response shape stays
        # consistent for the frontend.
        step = 1.0 / bins
        for i in range(bins):
            distance_histogram.append(G3DistanceHistogramBucketOut(
                lo=round(i * step, 4),
                hi=round((i + 1) * step, 4),
                count=0,
            ))

    # --- 3. Top-N nearest_neighbor (the AST-isomorphism magnets) ---
    top_neighbors_out: List[G3NeighborBucketOut] = []
    try:
        nn_rows = (await db.execute(_text(
            "SELECT nearest_neighbor_hash, COUNT(*) AS n "
            "FROM ast_distance_log "
            "WHERE created_at > now() - (:days || ' day')::interval "
            "  AND ast_distance_min IS NOT NULL "
            "  AND ast_distance_min < :tau "
            "  AND nearest_neighbor_hash IS NOT NULL "
            "GROUP BY nearest_neighbor_hash "
            "ORDER BY n DESC "
            "LIMIT :lim"
        ), {"days": str(int(days)), "tau": threshold, "lim": top_n})).all()
        for nh, n in nn_rows:
            top_neighbors_out.append(G3NeighborBucketOut(
                nearest_neighbor_hash=str(nh),
                blocked_count=int(n or 0),
            ))
    except Exception:
        pass

    # --- 4. Per-pillar block rate (from alphas.metrics G3 tag) ---
    # Uses the JSONB tag _g3_ast_originality_blocked stamped by
    # backend.alpha_originality.apply_to_alpha. Pillar lives at
    # metrics->>'pillar' (LLM-emit, set by hypothesis nodes). Use ->>
    # extraction + GROUP BY to avoid the @> path which would require a
    # GIN index that the alphas table doesn't have today.
    by_pillar: List[G3PillarBucketOut] = []
    try:
        pill_rows = (await db.execute(_text(
            "SELECT "
            "  COALESCE(metrics->>'pillar', 'unknown') AS pillar, "
            "  COUNT(*) FILTER (WHERE (metrics->>'_g3_ast_originality_blocked')::text = 'true') AS blocked, "
            "  COUNT(*) AS total "
            "FROM alphas "
            "WHERE created_at > now() - (:days || ' day')::interval "
            "  AND metrics ? '_g3_verdict' "
            "GROUP BY 1 "
            "ORDER BY total DESC"
        ), {"days": str(int(days))})).all()
        for pillar, blocked, total in pill_rows:
            t = int(total or 0)
            b = int(blocked or 0)
            by_pillar.append(G3PillarBucketOut(
                pillar=str(pillar or "unknown"),
                blocked=b,
                total=t,
                block_rate=round((b / t), 4) if t > 0 else 0.0,
            ))
    except Exception:
        pass

    return G3OriginalityStatsOut(
        flags=flags,
        mode=mode,
        threshold=threshold,
        window_days=int(days),
        total_candidates=total_candidates,
        blocked_candidates=blocked_candidates,
        block_rate=block_rate,
        distance_histogram=distance_histogram,
        top_neighbors=top_neighbors_out,
        by_pillar=by_pillar,
    )


# =============================================================================
# R8 hierarchical RAG telemetry (2026-05-18) — KB shape visibility
# =============================================================================
# Unlike R1a/R1b which write to dedicated log tables, R8 does NOT persist
# per-query telemetry — layer_hits is in the RAGResult dataclass but
# ephemeral. The actionable signal for operators is the KB's *shape*:
# how many SUCCESS_PATTERN / FAILURE_PITFALL entries exist, decayed
# split, pillar diversity, R5-rankable share. A thin KB → no hits in
# higher RAG layers regardless of dispatch flag state.


class R8EntryTypeBucketOut(BaseModel):
    entry_type: str
    active_count: int
    decayed_count: int


class R8PillarBucketOut(BaseModel):
    pillar: str
    entry_count: int


class R8KbShapeOut(BaseModel):
    flags: Dict[str, bool]
    entry_types: List[R8EntryTypeBucketOut]
    pillars: List[R8PillarBucketOut]
    total_active: int
    total_decayed: int
    success_pattern_active: int
    failure_pitfall_active: int
    # R8-v2 #2 R5 ranking coverage signal.
    # Semantic (post-1470c6e HIGH #4 fix): COUNT(DISTINCT expression_hash)
    # in r1a_attribution_log where r5_composite_score IS NOT NULL. Answers
    # "is R5 producing enough data for L2 ranking to be effective?" The
    # prior KB-JOIN semantic was silently broken (see ops.py:1703 comment)
    # so the field name retains "_success_count" for API-shape stability
    # despite no longer touching knowledge_entries; new field name like
    # `r5_evaluated_expression_count` would be cleaner but breaks frontend.
    r5_rankable_success_count: int


@router.get("/r8/kb-shape", response_model=R8KbShapeOut)
async def r8_kb_shape(
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> R8KbShapeOut:
    """R8 hierarchical RAG KB shape — operator visibility into corpus depth.

    Aggregates ``knowledge_entries`` over the full active table:
      - per-entry_type counts with active vs decayed split (Q9 dual-filter
        semantic: SUCCESS-side queries exclude decayed, FAILURE-side
        include them; operator needs to see both)
      - per-pillar entry_count distribution (L1 pillar layer matches against
        this — a missing pillar means L1 is effectively dead for that bucket)
      - R5-rankable signal — distinct expressions evaluated by R5 (count
        of unique expression_hash in r1a_attribution_log with non-null
        r5_composite_score) — operator confirms R5 produces enough data
        for L2 ranking before flipping ENABLE_R5_L2_RANKING ON. (Field
        name r5_rankable_success_count is legacy; semantic shifted in
        1470c6e HIGH #4 fix away from a broken KB JOIN.)
      - flag state for ENABLE_HIERARCHICAL_RAG + ENABLE_R5_L2_RANKING

    Use BEFORE flipping ENABLE_HIERARCHICAL_RAG default ON (R8-v2 #6
    canary) to confirm the KB has enough depth for hierarchical layers
    to actually match.
    """
    from sqlalchemy import text as _text
    from backend.config import settings as _stg

    # (Retired ENABLE_R5_L2_RANKING 2026-05-19 — subsumed into main switch.)
    flags = {
        "ENABLE_HIERARCHICAL_RAG": bool(getattr(_stg, "ENABLE_HIERARCHICAL_RAG", False)),
    }

    # Per-entry_type with decayed split. meta_data->>'decayed' is the
    # canonical marker (R8 layer_0 / layer_2 both filter by it).
    et_rows = (await db.execute(_text(
        "SELECT entry_type, "
        "       COUNT(*) FILTER (WHERE NOT (meta_data @> '{\"decayed\":\"true\"}'::jsonb)) AS active, "
        "       COUNT(*) FILTER (WHERE meta_data @> '{\"decayed\":\"true\"}'::jsonb) AS decayed "
        "FROM knowledge_entries "
        "WHERE is_active = true "
        "GROUP BY entry_type "
        "ORDER BY active DESC"
    ))).all()

    entry_types: List[R8EntryTypeBucketOut] = []
    total_active = 0
    total_decayed = 0
    success_active = 0
    failure_active = 0
    for et, a, d in et_rows:
        a_int = int(a or 0)
        d_int = int(d or 0)
        entry_types.append(R8EntryTypeBucketOut(
            entry_type=et or "unknown",
            active_count=a_int,
            decayed_count=d_int,
        ))
        total_active += a_int
        total_decayed += d_int
        if et == "SUCCESS_PATTERN":
            success_active = a_int
        elif et == "FAILURE_PITFALL":
            failure_active = a_int

    # Per-pillar distribution (only active rows — decayed ones don't
    # participate in L1 matching). COALESCE NULL pillar → 'none' bucket so
    # operator sees how many entries lack a pillar tag (RAG L1 won't reach
    # those — backfill candidates).
    pillar_rows = (await db.execute(_text(
        "SELECT COALESCE(meta_data->>'pillar', 'none') AS pillar, COUNT(*) AS n "
        "FROM knowledge_entries "
        "WHERE is_active = true "
        "  AND NOT (meta_data @> '{\"decayed\":\"true\"}'::jsonb) "
        "GROUP BY pillar "
        "ORDER BY n DESC"
    ))).all()
    pillars = [
        R8PillarBucketOut(
            pillar=p or "none",
            entry_count=int(n or 0),
        )
        for p, n in pillar_rows
    ]

    # R5-rankable distinct expressions — Review HIGH #4 fix (2026-05-18):
    # the prior JOIN of r1a_attribution_log.expression_hash =
    # knowledge_entries.pattern_hash was silently broken — the two hashes
    # are derived from DIFFERENT inputs (expression alone vs
    # pattern+region+dataset_id concat) AND truncated to different lengths
    # (sha256[:64] vs sha256[:32]). They never matched in production →
    # r5_rankable_success_count was permanently 0.
    #
    # Replacement signal: count distinct expressions that R5 judged in
    # r1a_attribution_log. Semantic shift from "KB SUCCESS with R5 data"
    # to "expressions with R5 data" — but it directly answers the operator
    # question "is R5 producing enough data for L2 ranking?" without
    # needing the broken cross-table join. The R8-v2 #2 L2 ranking lookup
    # in fetch_r5_avg_scores uses the same r1a-side groupby anyway.
    r5_count_row = (await db.execute(_text(
        "SELECT COUNT(DISTINCT expression_hash) "
        "FROM r1a_attribution_log "
        "WHERE r5_composite_score IS NOT NULL "
        "  AND expression_hash IS NOT NULL"
    ))).scalar()

    return R8KbShapeOut(
        flags=flags,
        entry_types=entry_types,
        pillars=pillars,
        total_active=total_active,
        total_decayed=total_decayed,
        success_pattern_active=success_active,
        failure_pitfall_active=failure_active,
        r5_rankable_success_count=int(r5_count_row or 0),
    )


# =============================================================================
# CoSTEER deploy-gate recommendation (2026-05-18) — synthesize the trio
# =============================================================================
# Mirrors the cascade-deprecation/readiness verdict pattern: aggregate the
# raw telemetry signals into a single ranked next-action recommendation
# so operators don't have to mentally combine 4 endpoints when deciding
# which R1a/R1b/R5/R8 flag to flip next. NOT a hard gate — operator still
# decides; this is an evidence-based hint.


class DeployRecommendationOut(BaseModel):
    ready_flags_to_flip: List[str]
    next_action: str
    blockers: List[str]                  # plain-English reasons a flag isn't ready yet
    signals: Dict[str, float]            # raw KPIs the recommendation was based on
    current_flag_state: Dict[str, bool]
    window_days: int


# Plan §10 deploy gate thresholds — keep in one place so a future plan
# revision can adjust them without sprinkling magic numbers.
_DEPLOY_GATES = {
    "r1a_non_unknown_pct_min": 0.40,         # plan §1.7 mid-point revised
    "r1a_total_min": 50,                     # min sample size before any KPI is trustworthy
    "r8_success_active_min": 100,            # corpus depth for hierarchical RAG
    "r8_pillar_diversity_min": 3,            # ≥ 3 non-empty non-'none' pillars
    "r8_r5_rankable_min": 30,                # R5 re-rank sample size
    "r1b_retry_pass_rate_min": 0.15,         # plan §10 — 7d obs / ≥15% success
    "r1b_mutate_pass_rate_min": 0.10,        # plan §10
    "r1b_retry_attempts_min": 50,            # plan §10 — ≥ 50 retries before promote
    "r1b_mutate_attempts_min": 30,           # plan §10 — ≥ 30 mutations
    "r1b_chain_max_depth_min": 1,            # > 1 confirms R1b.3-v2 chain growing
}


def _count_attempts(rows: List[Dict[str, Any]], attempt_type: str) -> int:
    return sum(int(r.get("count", 0)) for r in rows if r.get("attempt_type") == attempt_type)


@router.get("/costeer/deploy-recommendation", response_model=DeployRecommendationOut)
async def costeer_deploy_recommendation(
    days: int = 7,
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> DeployRecommendationOut:
    """Synthesize R1a/R1b/R8 telemetry into next-action recommendation.

    Reads the same SQL the underlying telemetry endpoints read (no
    HTTP-self-call) so this is a single round-trip from the operator's
    UI. Returns a ranked list of flags the metrics support flipping
    next + plain-English blockers + the raw signals so the operator
    can audit the recommendation.

    This endpoint is advisory — it never changes flag state itself.
    """
    from sqlalchemy import text as _text
    from backend.config import settings as _stg

    # --- 1. Current flag state ---
    state = {
        "ENABLE_R1A_HOOK": bool(getattr(_stg, "ENABLE_R1A_HOOK", False)),
        "ENABLE_LLM_JUDGE": bool(getattr(_stg, "ENABLE_LLM_JUDGE", False)),
        "ENABLE_HIERARCHICAL_RAG": bool(getattr(_stg, "ENABLE_HIERARCHICAL_RAG", False)),
        "ENABLE_R1B_RETRY_LOOP": bool(getattr(_stg, "ENABLE_R1B_RETRY_LOOP", False)),
        "ENABLE_R1B_HYPOTHESIS_MUTATE": bool(getattr(_stg, "ENABLE_R1B_HYPOTHESIS_MUTATE", False)),
        "ENABLE_R1B_FAILURE_TREE": bool(getattr(_stg, "ENABLE_R1B_FAILURE_TREE", False)),
        "ENABLE_R1B_TYPED_PIPELINE": bool(getattr(_stg, "ENABLE_R1B_TYPED_PIPELINE", False)),
        "ENABLE_R1B_DAG_RETRY_REWARD": bool(getattr(_stg, "ENABLE_R1B_DAG_RETRY_REWARD", False)),
    }

    # --- 2. R1a non_unknown_pct + total in window ---
    r1a_rows = (await db.execute(_text(
        "SELECT COALESCE(attribution, 'null') AS attr, COUNT(*) AS n "
        "FROM r1a_attribution_log "
        "WHERE created_at > now() - (:days || ' day')::interval "
        "GROUP BY attr"
    ), {"days": str(int(days))})).all()
    r1a_total = sum(int(n or 0) for _, n in r1a_rows)
    r1a_non_null = sum(int(n or 0) for attr, n in r1a_rows if attr and attr != "null")
    r1a_actionable = sum(
        int(n or 0) for attr, n in r1a_rows
        if attr in ("hypothesis", "implementation", "both")
    )
    r1a_non_unknown_pct = (r1a_actionable / r1a_non_null) if r1a_non_null > 0 else 0.0

    # --- 3. R8 KB shape ---
    r8_kb_row = (await db.execute(_text(
        "SELECT "
        "  COUNT(*) FILTER (WHERE entry_type='SUCCESS_PATTERN' AND NOT (meta_data @> '{\"decayed\":\"true\"}'::jsonb)) AS succ, "
        "  COUNT(DISTINCT meta_data->>'pillar') FILTER ("
        "    WHERE NOT (meta_data @> '{\"decayed\":\"true\"}'::jsonb) "
        "      AND meta_data->>'pillar' IS NOT NULL "
        "      AND meta_data->>'pillar' != 'none') AS pillar_diversity "
        "FROM knowledge_entries WHERE is_active=true"
    ))).one()
    r8_succ_active = int(r8_kb_row[0] or 0)
    r8_pillars = int(r8_kb_row[1] or 0)

    # Review HIGH #4 fix (2026-05-18) — see r8_kb_shape for full rationale.
    # Old JOIN was silently broken (different hash algos + truncation).
    # Replacement: count distinct expressions with R5 data in r1a side
    # only — directly answers "is R5 producing enough data for L2
    # ranking?" without the cross-table mismatch.
    r5_rankable_row = (await db.execute(_text(
        "SELECT COUNT(DISTINCT expression_hash) "
        "FROM r1a_attribution_log "
        "WHERE r5_composite_score IS NOT NULL "
        "  AND expression_hash IS NOT NULL"
    ))).scalar()
    r5_rankable = int(r5_rankable_row or 0)

    # --- 4. R1b retry + mutate stats + chain depth ---
    r1b_rows = (await db.execute(_text(
        "SELECT attempt_type, COALESCE(outcome,'unknown') AS outcome, COUNT(*) AS n "
        "FROM r1b_retry_log "
        "WHERE created_at > now() - (:days || ' day')::interval "
        "GROUP BY attempt_type, outcome"
    ), {"days": str(int(days))})).all()
    r1b_dicts = [
        {"attempt_type": at, "outcome": oc, "count": int(n or 0)}
        for at, oc, n in r1b_rows
    ]
    retry_pass = sum(d["count"] for d in r1b_dicts if d["attempt_type"] == "retry_impl" and d["outcome"] == "pass")
    retry_fail = sum(d["count"] for d in r1b_dicts if d["attempt_type"] == "retry_impl" and d["outcome"] == "fail")
    mutate_pass = sum(d["count"] for d in r1b_dicts if d["attempt_type"] == "mutate_hyp" and d["outcome"] == "pass")
    mutate_fail = sum(d["count"] for d in r1b_dicts if d["attempt_type"] == "mutate_hyp" and d["outcome"] == "fail")
    retry_pass_rate = retry_pass / (retry_pass + retry_fail) if (retry_pass + retry_fail) > 0 else 0.0
    mutate_pass_rate = mutate_pass / (mutate_pass + mutate_fail) if (mutate_pass + mutate_fail) > 0 else 0.0
    retry_attempts = _count_attempts(r1b_dicts, "retry_impl")
    mutate_attempts = _count_attempts(r1b_dicts, "mutate_hyp")

    max_depth_row = (await db.execute(_text(
        "SELECT COALESCE(MAX(r1b_mutation_depth), 0) FROM hypotheses"
    ))).scalar()
    chain_max_depth = int(max_depth_row or 0)

    # --- 5. Build ready list + blockers ---
    g = _DEPLOY_GATES
    ready: List[str] = []
    blockers: List[str] = []

    def _check(name: str, condition: bool, blocker: str) -> None:
        if state.get(name):
            return
        if condition:
            ready.append(name)
        else:
            blockers.append(blocker)

    _check(
        "ENABLE_R1A_HOOK",
        r1a_total >= g["r1a_total_min"],
        f"R1A: only {r1a_total} samples in window (need ≥{g['r1a_total_min']})",
    )
    _check(
        "ENABLE_HIERARCHICAL_RAG",
        r8_succ_active >= g["r8_success_active_min"] and r8_pillars >= g["r8_pillar_diversity_min"],
        f"R8: SUCCESS_PATTERN active={r8_succ_active} (need ≥{g['r8_success_active_min']}) / "
        f"pillar diversity={r8_pillars} (need ≥{g['r8_pillar_diversity_min']})",
    )
    # (Retired ENABLE_R5_L2_RANKING gate 2026-05-19 — subsumed into hierarchical
    # RAG main switch. r5_rankable count remains exposed at r8_r5_rankable_success
    # for forensic visibility.)
    _check(
        "ENABLE_R1B_RETRY_LOOP",
        state["ENABLE_R1A_HOOK"] and r1a_non_unknown_pct >= g["r1a_non_unknown_pct_min"]
        and r1a_total >= g["r1a_total_min"],
        f"R1b retry: R1A flag={state['ENABLE_R1A_HOOK']} / non_unknown_pct={r1a_non_unknown_pct:.2f}"
        f" (need ≥{g['r1a_non_unknown_pct_min']}) / R1A total {r1a_total}<{g['r1a_total_min']}",
    )
    _check(
        "ENABLE_R1B_HYPOTHESIS_MUTATE",
        state["ENABLE_R1B_RETRY_LOOP"]
        and retry_attempts >= g["r1b_retry_attempts_min"]
        and retry_pass_rate >= g["r1b_retry_pass_rate_min"],
        f"R1b mutate: retry flag={state['ENABLE_R1B_RETRY_LOOP']} / "
        f"retry attempts={retry_attempts}<{g['r1b_retry_attempts_min']} or "
        f"pass rate={retry_pass_rate:.3f}<{g['r1b_retry_pass_rate_min']}",
    )
    _check(
        "ENABLE_R1B_FAILURE_TREE",
        state["ENABLE_R1B_HYPOTHESIS_MUTATE"]
        and mutate_attempts >= g["r1b_mutate_attempts_min"]
        and mutate_pass_rate >= g["r1b_mutate_pass_rate_min"],
        f"R1b failure_tree: mutate flag={state['ENABLE_R1B_HYPOTHESIS_MUTATE']} / "
        f"mutate attempts={mutate_attempts}<{g['r1b_mutate_attempts_min']} or "
        f"pass rate={mutate_pass_rate:.3f}<{g['r1b_mutate_pass_rate_min']}",
    )
    _check(
        "ENABLE_R1B_DAG_RETRY_REWARD",
        state["ENABLE_R1B_RETRY_LOOP"] and chain_max_depth > g["r1b_chain_max_depth_min"],
        f"DAG retry reward: chain max_depth={chain_max_depth} (need >{g['r1b_chain_max_depth_min']})",
    )

    # next_action picks the first ready flag (deploy order matters per plan §10)
    if ready:
        next_action = f"Flip {ready[0]} via PATCH /ops/flags/{ready[0]} — gates met."
    elif blockers:
        next_action = f"No flag ready. Top blocker: {blockers[0]}"
    else:
        next_action = "All eligible flags already ON or no gates apply. Hold."

    signals = {
        "r1a_total_in_window": float(r1a_total),
        "r1a_non_unknown_pct": round(r1a_non_unknown_pct, 4),
        "r8_success_pattern_active": float(r8_succ_active),
        "r8_pillar_diversity": float(r8_pillars),
        "r8_r5_rankable_success": float(r5_rankable),
        "r1b_retry_pass_rate": round(retry_pass_rate, 4),
        "r1b_mutate_pass_rate": round(mutate_pass_rate, 4),
        "r1b_retry_attempts": float(retry_attempts),
        "r1b_mutate_attempts": float(mutate_attempts),
        "r1b_chain_max_depth": float(chain_max_depth),
    }

    return DeployRecommendationOut(
        ready_flags_to_flip=ready,
        next_action=next_action,
        blockers=blockers,
        signals=signals,
        current_flag_state=state,
        window_days=int(days),
    )


# =============================================================================
# R8 query-level telemetry (2026-05-18) — per-call layer hit rates
# =============================================================================
# Aggregates r8_query_log written by query_hierarchical when ENABLE_R8_QUERY_LOG
# is ON. Complements /ops/r8/kb-shape (corpus snapshot) with runtime
# fall-through stats — operator confirms hierarchical RAG actually reaches
# higher layers before promoting ENABLE_HIERARCHICAL_RAG to default-ON.


class R8QueryStatsOut(BaseModel):
    flags: Dict[str, bool]
    total_queries: int
    cache_hit_rate: float                # cache_hit=true / total
    failure_tree_elevation_rate: float   # had_failure_tree_elevation=true / total
    layer_hit_rates: Dict[str, float]    # {L0_exact, L1_pillar, L2_family, L3_field} → ratio of queries that touched that layer
    by_region: Dict[str, int]            # region → query count
    window_days: int


@router.get("/r8/query-stats", response_model=R8QueryStatsOut)
async def r8_query_stats(
    days: int = 7,
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> R8QueryStatsOut:
    """R8 per-query telemetry — layer hit rates + cache effectiveness.

    Aggregates ``r8_query_log`` over the last ``days`` window. Use to
    confirm runtime layer fall-through patterns before promoting
    ENABLE_HIERARCHICAL_RAG default-ON: healthy deploy shows L0+L1
    dominant (high specificity hits) with L2/L3 as fall-through tail.
    If L3 dominates that's a KB-shape signal (corpus too thin for
    higher layers).

    Returns zero rates when ENABLE_R8_QUERY_LOG flag was OFF in the
    window (no rows written). Cache hit rate semantic = "any layer in
    the query served from Redis cache" (closure counter in
    query_hierarchical._layer_call, commit d8ed47f). Extend to per-
    layer breakdown via a layer_hits_from_cache JSONB column if
    operator demand arises.
    """
    from sqlalchemy import text as _text
    from backend.config import settings as _stg

    # (Retired ENABLE_HIERARCHICAL_RAG_CACHE 2026-05-19 — subsumed into main switch.)
    flags = {
        "ENABLE_HIERARCHICAL_RAG": bool(getattr(_stg, "ENABLE_HIERARCHICAL_RAG", False)),
        "ENABLE_R8_QUERY_LOG": bool(getattr(_stg, "ENABLE_R8_QUERY_LOG", False)),
    }

    # Aggregate: total + cache hit + failure_tree_elevation + per-layer
    # presence count. layer_hits is JSONB; (layer_hits->'L0_exact')::int > 0
    # counts as a touch. COALESCE handles NULL layer_hits.
    row = (await db.execute(_text(
        "SELECT "
        "  COUNT(*) AS total, "
        "  COUNT(*) FILTER (WHERE cache_hit = true) AS cache, "
        "  COUNT(*) FILTER (WHERE had_failure_tree_elevation = true) AS elev, "
        "  COUNT(*) FILTER (WHERE COALESCE((layer_hits->>'L0_exact')::int, 0) > 0) AS l0, "
        "  COUNT(*) FILTER (WHERE COALESCE((layer_hits->>'L1_pillar')::int, 0) > 0) AS l1, "
        "  COUNT(*) FILTER (WHERE COALESCE((layer_hits->>'L2_family')::int, 0) > 0) AS l2, "
        "  COUNT(*) FILTER (WHERE COALESCE((layer_hits->>'L3_field')::int, 0) > 0) AS l3 "
        "FROM r8_query_log "
        "WHERE created_at > now() - (:days || ' day')::interval"
    ), {"days": str(int(days))})).one()

    total, cache, elev, l0, l1, l2, l3 = row
    total_int = int(total or 0)
    cache_rate = round(int(cache or 0) / total_int, 4) if total_int > 0 else 0.0
    elev_rate = round(int(elev or 0) / total_int, 4) if total_int > 0 else 0.0
    layer_rates = {
        "L0_exact": round(int(l0 or 0) / total_int, 4) if total_int > 0 else 0.0,
        "L1_pillar": round(int(l1 or 0) / total_int, 4) if total_int > 0 else 0.0,
        "L2_family": round(int(l2 or 0) / total_int, 4) if total_int > 0 else 0.0,
        "L3_field": round(int(l3 or 0) / total_int, 4) if total_int > 0 else 0.0,
    }

    # Per-region breakdown — operator sees if one region dominates.
    region_rows = (await db.execute(_text(
        "SELECT COALESCE(region, 'none') AS r, COUNT(*) AS n "
        "FROM r8_query_log "
        "WHERE created_at > now() - (:days || ' day')::interval "
        "GROUP BY r ORDER BY n DESC"
    ), {"days": str(int(days))})).all()
    by_region = {(r or "none"): int(n or 0) for r, n in region_rows}

    return R8QueryStatsOut(
        flags=flags,
        total_queries=total_int,
        cache_hit_rate=cache_rate,
        failure_tree_elevation_rate=elev_rate,
        layer_hit_rates=layer_rates,
        by_region=by_region,
        window_days=int(days),
    )


# =============================================================================
# Phase 4 Sprint 3 B5 — R8-v3 cognitive-layer telemetry (2026-05-20)
# =============================================================================
# Aggregates alpha.metrics['_cognitive_layer_used'] over the trailing window.
# Stamped by node_evaluate when ENABLE_COGNITIVE_LAYER_PROMPT was on at
# hypothesis time. Surfaces per-layer fire count + PASS rate to inform
# (a) flip COGNITIVE_LAYER_SELECT_MODE 'round_robin'→'bandit' once stats
# are seeded, and (b) future BanditState seed via offline cron.


class CognitiveLayerStat(BaseModel):
    layer_id: str
    fired_count: int
    pass_count: int
    fail_count: int
    pass_rate: float


class CognitiveLayerStatsOut(BaseModel):
    flags: Dict[str, bool]
    total_stamped_alphas: int
    by_layer: List[CognitiveLayerStat]
    window_days: int


@router.get("/r8-v3/cognitive-layer-stats", response_model=CognitiveLayerStatsOut)
async def r8v3_cognitive_layer_stats(
    days: int = 7,
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> CognitiveLayerStatsOut:
    """R8-v3 per-layer PASS/FAIL distribution.

    Aggregates ``alpha.metrics->>'_cognitive_layer_used'`` over the
    trailing ``days`` window. Use to confirm that
    ENABLE_COGNITIVE_LAYER_PROMPT is actually stamping (non-zero
    total_stamped_alphas) and to seed the bandit state before flipping
    COGNITIVE_LAYER_SELECT_MODE to 'bandit'.

    Returns zero stats when the flag was OFF in the window (no stamps)
    OR when the DB dialect is not Postgres (the query uses JSONB
    operators ``?`` + ``->>`` which are Postgres-only — F12 review
    fix: degrade gracefully rather than throw on dev SQLite).
    """
    from sqlalchemy import text as _text
    from backend.config import settings as _stg

    flags = {
        "ENABLE_COGNITIVE_LAYER_PROMPT": bool(
            getattr(_stg, "ENABLE_COGNITIVE_LAYER_PROMPT", False)
        ),
    }

    # F12 review fix: SQLite (dev) lacks JSONB key-existence operator.
    # Return an empty payload rather than crash so dev workflows stay
    # green; operator runs the real telemetry on Postgres.
    dialect_name = db.bind.dialect.name if db.bind is not None else "unknown"
    if dialect_name != "postgresql":
        return CognitiveLayerStatsOut(
            flags=flags,
            total_stamped_alphas=0,
            by_layer=[],
            window_days=int(days),
        )

    rows = (await db.execute(_text("""
        SELECT
          metrics->>'_cognitive_layer_used' AS layer_id,
          COUNT(*) AS fired,
          COUNT(*) FILTER (WHERE quality_status IN ('PASS', 'PASS_PROVISIONAL')) AS passed,
          COUNT(*) FILTER (WHERE quality_status = 'FAIL') AS failed
        FROM alphas
        WHERE created_at > now() - (:days || ' day')::interval
          AND metrics ? '_cognitive_layer_used'
          AND metrics->>'_cognitive_layer_used' <> ''
        GROUP BY metrics->>'_cognitive_layer_used'
        ORDER BY fired DESC
    """), {"days": str(int(days))})).all()

    by_layer: List[CognitiveLayerStat] = []
    total = 0
    for layer_id, fired, passed, failed in rows:
        fired_i = int(fired or 0)
        passed_i = int(passed or 0)
        failed_i = int(failed or 0)
        total += fired_i
        rate = round(passed_i / fired_i, 4) if fired_i > 0 else 0.0
        by_layer.append(CognitiveLayerStat(
            layer_id=str(layer_id),
            fired_count=fired_i,
            pass_count=passed_i,
            fail_count=failed_i,
            pass_rate=rate,
        ))

    return CognitiveLayerStatsOut(
        flags=flags,
        total_stamped_alphas=total,
        by_layer=by_layer,
        window_days=int(days),
    )


# =============================================================================
# Phase 4 Sprint 3 A5.1 G10 — distilled logic library (2026-05-20)
# =============================================================================
# /ops/g10/logic-library lists recent distilled_logic_library rows, filtered
# by region / pillar / active(retired_at IS NULL) for operator inspection
# + Sprint 4 PR2 retrieval validation.


class G10DistilledLogicEntry(BaseModel):
    id: int
    logic_text: str
    pillar: Optional[str]
    region: str
    distilled_at_week: Optional[datetime] = None
    source_alpha_count: int
    llm_cost_usd: Optional[float]
    similarity_jaccard_to_prev_week: Optional[float]
    llm_model: Optional[str]
    is_active: bool


class G10LogicLibraryOut(BaseModel):
    flags: Dict[str, bool]
    total_active: int
    total_retired: int
    weekly_total_cost_usd: float
    by_region: Dict[str, int]
    entries: List[G10DistilledLogicEntry]
    window_days: int


@router.get("/g10/logic-library", response_model=G10LogicLibraryOut)
async def g10_logic_library(
    days: int = 28,
    region: Optional[str] = None,
    pillar: Optional[str] = None,
    active_only: bool = True,
    limit: int = 100,
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> G10LogicLibraryOut:
    """G10 logic library: list distilled-logic rows within ``days``.

    Filters:
      - ``region``  : narrow to one region
      - ``pillar``  : narrow to one pillar
      - ``active_only`` : default True — exclude retired rows (Sprint 4
        PR2 retires superseded rows)

    Aggregates the same window for:
      - total_active / total_retired across all (region, pillar)
      - weekly_total_cost_usd across active rows in window
      - by_region row count
    """
    from sqlalchemy import text as _text
    from backend.config import settings as _stg

    flags = {
        "ENABLE_G10_LOGIC_DISTILL": bool(
            getattr(_stg, "ENABLE_G10_LOGIC_DISTILL", False)
        ),
    }

    where_clauses = ["created_at > now() - (:days || ' day')::interval"]
    params: Dict[str, Any] = {"days": str(int(days)), "limit": int(limit)}
    if region:
        where_clauses.append("region = :region")
        params["region"] = region
    if pillar:
        where_clauses.append("pillar = :pillar")
        params["pillar"] = pillar
    if active_only:
        where_clauses.append("retired_at IS NULL")
    where_sql = " AND ".join(where_clauses)

    rows = (await db.execute(_text(f"""
        SELECT
          id, logic_text, pillar, region, distilled_at_week,
          source_alpha_ids, llm_cost_usd,
          similarity_jaccard_to_prev_week, llm_model, retired_at
        FROM distilled_logic_library
        WHERE {where_sql}
        ORDER BY distilled_at_week DESC, id DESC
        LIMIT :limit
    """), params)).all()

    entries: List[G10DistilledLogicEntry] = []
    for (
        row_id, logic_text, p, r, week, source_ids,
        cost, sim, model, retired_at,
    ) in rows:
        if isinstance(source_ids, list):
            src_count = len(source_ids)
        else:
            src_count = 0
        entries.append(G10DistilledLogicEntry(
            id=int(row_id),
            logic_text=str(logic_text),
            pillar=str(p) if p else None,
            region=str(r),
            distilled_at_week=week,
            source_alpha_count=src_count,
            llm_cost_usd=float(cost) if cost is not None else None,
            similarity_jaccard_to_prev_week=float(sim) if sim is not None else None,
            llm_model=str(model) if model else None,
            is_active=retired_at is None,
        ))

    # Window-level aggregates (independent of active_only / pillar filter
    # so operator sees the full picture).
    agg_row = (await db.execute(_text("""
        SELECT
          COUNT(*) FILTER (WHERE retired_at IS NULL) AS active,
          COUNT(*) FILTER (WHERE retired_at IS NOT NULL) AS retired,
          COALESCE(SUM(llm_cost_usd) FILTER (WHERE retired_at IS NULL), 0.0) AS cost_sum
        FROM distilled_logic_library
        WHERE created_at > now() - (:days || ' day')::interval
    """), {"days": str(int(days))})).one()

    by_region_rows = (await db.execute(_text("""
        SELECT region, COUNT(*) AS n
        FROM distilled_logic_library
        WHERE created_at > now() - (:days || ' day')::interval
          AND retired_at IS NULL
        GROUP BY region
        ORDER BY n DESC
    """), {"days": str(int(days))})).all()

    return G10LogicLibraryOut(
        flags=flags,
        total_active=int(agg_row[0] or 0),
        total_retired=int(agg_row[1] or 0),
        weekly_total_cost_usd=round(float(agg_row[2] or 0.0), 4),
        by_region={str(r): int(n) for r, n in by_region_rows},
        entries=entries,
        window_days=int(days),
    )


# =============================================================================
# Phase 3 R9 — simulation cache telemetry (2026-05-18)
# =============================================================================
# Reads simulation_cache table aggregates. hit/miss has no dedicated counter;
# we infer from access_count distribution (first write = miss, every +1 = hit
# that saved one BRAIN call). saved_brain_calls = SUM(access_count) - COUNT(*).


class R9CacheRegionStat(BaseModel):
    region: str
    universe: str
    entries: int
    accesses: int
    saved_brain_calls: int


class R9CacheStatsOut(BaseModel):
    flags: Dict[str, bool]
    total_cached_rows: int
    rows_in_window: int
    total_accesses_lifetime: int
    saved_brain_calls: int
    hit_rate_approx: float
    avg_accesses_per_entry: float
    success_rate: float
    ttl_days: int
    expired_rows: int
    by_region: List[R9CacheRegionStat]
    window_days: int


@router.get("/r9/cache-stats", response_model=R9CacheStatsOut)
async def r9_cache_stats(
    days: int = 7,
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> R9CacheStatsOut:
    """R9 simulation cache telemetry — reuse-rate + savings approximation.

    No dedicated hit/miss counter exists in R9 — we infer from the
    ``access_count`` distribution on ``simulation_cache``. Per the model
    (``backend/models/simulation_cache.py``), ``access_count`` defaults to
    1 on insert (the initial write) and is incremented by 1 on every
    subsequent hit:

    - ``access_count = 1`` rows: a write that has never been re-hit
    - ``access_count > 1`` rows: re-hit at least once
    - **saved_brain_calls = SUM(access_count) - COUNT(*)** (each +1 over
      the initial write is a hit that bypassed BRAIN)

    .. warning::

       ``hit_rate_approx`` is NOT a traditional hit/total ratio. It is the
       *fraction of cache entries reused at least once*
       (``COUNT(access_count > 1) / COUNT(*)``). An entry hit 1000 times
       and one hit 1 time both count equally toward the numerator. For
       true reuse depth use ``avg_accesses_per_entry``. The field name is
       kept for API stability; the frontend label is "缓存复用率".

       If a future cache refactor changes ``access_count`` init/increment
       semantics, ``saved_brain_calls`` and ``hit_rate_approx`` will both
       silently drift — re-derive both formulas.

    Healthy R9 deploy: ``hit_rate_approx >= 0.3`` (≥30% of entries reused
    within TTL window) and ``avg_accesses_per_entry >= 1.5`` (cache reuse
    across tasks/rounds). ``expired_rows > 0`` is normal — entries past
    ``SIMULATION_CACHE_TTL_DAYS`` are filtered out by ``get_cached`` but
    physically retained until a future eviction sweep.

    ``window_days`` only affects ``rows_in_window`` (recently-added rows)
    — lifetime stats span the full table since TTL filtering happens at
    read time, not on writes.
    """
    from sqlalchemy import text as _text
    from backend.config import settings as _stg

    flags = {
        "ENABLE_SIMULATION_CACHE": bool(getattr(_stg, "ENABLE_SIMULATION_CACHE", False)),
    }
    ttl_days = int(getattr(_stg, "SIMULATION_CACHE_TTL_DAYS", 14))

    overall = (await db.execute(_text(
        "SELECT "
        "  COUNT(*) AS total_rows, "
        "  COALESCE(SUM(access_count), 0) AS total_acc, "
        "  COUNT(*) FILTER (WHERE access_count > 1) AS hit_multi, "
        "  COUNT(*) FILTER (WHERE success = true) AS success_count, "
        "  COUNT(*) FILTER (WHERE cached_at > now() - (:days || ' day')::interval) AS rows_in_win, "
        "  COUNT(*) FILTER (WHERE cached_at < now() - (:ttl || ' day')::interval) AS expired "
        "FROM simulation_cache"
    ), {"days": str(int(days)), "ttl": str(ttl_days)})).one()

    total_rows, total_acc, hit_multi, success_count, rows_in_win, expired = overall
    total_int = int(total_rows or 0)
    total_acc_int = int(total_acc or 0)
    saved = max(total_acc_int - total_int, 0)
    hit_rate = round(int(hit_multi or 0) / total_int, 4) if total_int > 0 else 0.0
    avg_acc = round(total_acc_int / total_int, 4) if total_int > 0 else 0.0
    success_rate = round(int(success_count or 0) / total_int, 4) if total_int > 0 else 0.0

    region_rows = (await db.execute(_text(
        "SELECT region, universe, "
        "  COUNT(*) AS entries, "
        "  COALESCE(SUM(access_count), 0) AS accesses "
        "FROM simulation_cache "
        "GROUP BY region, universe "
        "ORDER BY accesses DESC "
        "LIMIT 20"
    ))).all()
    by_region = [
        R9CacheRegionStat(
            region=r or "?",
            universe=u or "?",
            entries=int(e or 0),
            accesses=int(a or 0),
            saved_brain_calls=max(int(a or 0) - int(e or 0), 0),
        )
        for r, u, e, a in region_rows
    ]

    return R9CacheStatsOut(
        flags=flags,
        total_cached_rows=total_int,
        rows_in_window=int(rows_in_win or 0),
        total_accesses_lifetime=total_acc_int,
        saved_brain_calls=saved,
        hit_rate_approx=hit_rate,
        avg_accesses_per_entry=avg_acc,
        success_rate=success_rate,
        ttl_days=ttl_days,
        expired_rows=int(expired or 0),
        by_region=by_region,
        window_days=int(days),
    )


# =============================================================================
# Phase 2 R5 — LLM Judge cost + agreement telemetry (2026-05-18)
# =============================================================================
# Complements /ops/r1a/telemetry which already reports
# r5_agrees_r1a_pct / r5_avg_composite_score / r5_total_cost_usd / r5_sample_size.
# This endpoint adds the R5-internal metrics: per-judge cost, c1/c2 align rates,
# c1↔c2 internal agreement (the real critic-disagreement signal), error rate,
# cost outlier, and composite-score distribution buckets.
#
# Schema reused: backend/models/r1a_attribution.py R1aAttributionLog has 10
# R5 columns (r5_c1_aligned/confidence/reason, r5_c2_*, r5_composite_score,
# r5_agrees_r1a, r5_hook_error, r5_cost_usd). No pillar/region columns on
# r1a_log, so this endpoint is pillar-agnostic (operator wants pillar split
# can JOIN hypothesis later if demand emerges).


class R5CompositeBucket(BaseModel):
    bucket: str       # "0.7-1.0" | "0.5-0.7" | "0.0-0.5"
    count: int


class R5JudgeStatsOut(BaseModel):
    flags: Dict[str, bool]

    total_judges_run: int           # rows with r5_composite_score IS NOT NULL
    total_attempts: int             # judges_run + hook_errors (denominator for error_rate)
    error_count: int                # r5_hook_error IS NOT NULL
    error_rate: float

    total_cost_usd: float
    avg_cost_per_judge: float
    max_cost_per_judge: float

    c1_align_rate: float            # r5_c1_aligned='true' / r5_c1_aligned IS NOT NULL
    c2_align_rate: float
    c1_avg_confidence: float
    c2_avg_confidence: float
    c1_c2_internal_agreement: float # COUNT(c1=c2) / COUNT(both not null) — real critic agreement

    avg_composite_score: float
    composite_score_buckets: List[R5CompositeBucket]

    healthy_gates: Dict[str, float]
    is_healthy: bool

    window_days: int


@router.get("/r5/judge-stats", response_model=R5JudgeStatsOut)
async def r5_judge_stats(
    days: int = 7,
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> R5JudgeStatsOut:
    """R5 LLM judge internal telemetry — cost-per-judge + c1/c2 alignment.

    Healthy R5 deploy: ``avg_cost_per_judge <= $0.010`` (deploy GO gate),
    ``c1_c2_internal_agreement >= 0.6`` (two critics genuinely independent
    but mostly consistent — if 1.0 the second critic is redundant; if <0.5
    they're disagreeing more than chance and the composite score is noise),
    ``error_rate <= 0.05`` (LLM call failures rare), and
    ``total_judges_run >= 30`` for stable averages.

    Per-region / per-pillar splits intentionally omitted — r1a_attribution_log
    carries neither column. To add per-pillar would require expression_hash
    JOIN through alphas → hypothesis; defer until operator requests it.
    """
    from sqlalchemy import text as _text
    from backend.config import settings as _stg

    flags = {
        "ENABLE_LLM_JUDGE": bool(getattr(_stg, "ENABLE_LLM_JUDGE", False)),
    }

    row = (await db.execute(_text(
        "SELECT "
        "  COUNT(*) FILTER (WHERE r5_composite_score IS NOT NULL) AS judges, "
        "  COUNT(*) FILTER (WHERE r5_hook_error IS NOT NULL) AS errs, "
        "  COALESCE(SUM(r5_cost_usd), 0.0) AS total_cost, "
        "  COALESCE(AVG(r5_cost_usd), 0.0) AS avg_cost, "
        "  COALESCE(MAX(r5_cost_usd), 0.0) AS max_cost, "
        "  COUNT(*) FILTER (WHERE r5_c1_aligned = 'true') AS c1_true, "
        "  COUNT(*) FILTER (WHERE r5_c1_aligned IS NOT NULL) AS c1_total, "
        "  COUNT(*) FILTER (WHERE r5_c2_aligned = 'true') AS c2_true, "
        "  COUNT(*) FILTER (WHERE r5_c2_aligned IS NOT NULL) AS c2_total, "
        "  COALESCE(AVG(r5_c1_confidence), 0.0) AS c1_conf, "
        "  COALESCE(AVG(r5_c2_confidence), 0.0) AS c2_conf, "
        "  COUNT(*) FILTER (WHERE r5_c1_aligned IS NOT NULL AND r5_c2_aligned IS NOT NULL) AS both_present, "
        "  COUNT(*) FILTER (WHERE r5_c1_aligned IS NOT NULL AND r5_c1_aligned = r5_c2_aligned) AS both_agree, "
        "  COALESCE(AVG(r5_composite_score), 0.0) AS avg_score, "
        "  COUNT(*) FILTER (WHERE r5_composite_score >= 0.7) AS hi, "
        "  COUNT(*) FILTER (WHERE r5_composite_score >= 0.5 AND r5_composite_score < 0.7) AS mid, "
        "  COUNT(*) FILTER (WHERE r5_composite_score >= 0.0 AND r5_composite_score < 0.5) AS lo "
        "FROM r1a_attribution_log "
        "WHERE created_at > now() - (:days || ' day')::interval"
    ), {"days": str(int(days))})).one()

    (judges, errs, total_cost, avg_cost, max_cost,
     c1_true, c1_total, c2_true, c2_total,
     c1_conf, c2_conf,
     both_present, both_agree,
     avg_score, hi, mid, lo) = row

    judges_int = int(judges or 0)
    errs_int = int(errs or 0)
    total_attempts = judges_int + errs_int
    error_rate = round(errs_int / total_attempts, 4) if total_attempts > 0 else 0.0

    c1_total_int = int(c1_total or 0)
    c2_total_int = int(c2_total or 0)
    c1_rate = round(int(c1_true or 0) / c1_total_int, 4) if c1_total_int > 0 else 0.0
    c2_rate = round(int(c2_true or 0) / c2_total_int, 4) if c2_total_int > 0 else 0.0

    both_int = int(both_present or 0)
    internal_agree = round(int(both_agree or 0) / both_int, 4) if both_int > 0 else 0.0

    avg_cost_f = round(float(avg_cost or 0.0), 6)
    healthy = (
        avg_cost_f <= 0.010
        and internal_agree >= 0.60
        and error_rate <= 0.05
        and judges_int >= 30
    )

    return R5JudgeStatsOut(
        flags=flags,
        total_judges_run=judges_int,
        total_attempts=total_attempts,
        error_count=errs_int,
        error_rate=error_rate,
        total_cost_usd=round(float(total_cost or 0.0), 4),
        avg_cost_per_judge=avg_cost_f,
        max_cost_per_judge=round(float(max_cost or 0.0), 6),
        c1_align_rate=c1_rate,
        c2_align_rate=c2_rate,
        c1_avg_confidence=round(float(c1_conf or 0.0), 4),
        c2_avg_confidence=round(float(c2_conf or 0.0), 4),
        c1_c2_internal_agreement=internal_agree,
        avg_composite_score=round(float(avg_score or 0.0), 4),
        composite_score_buckets=[
            R5CompositeBucket(bucket="0.7-1.0", count=int(hi or 0)),
            R5CompositeBucket(bucket="0.5-0.7", count=int(mid or 0)),
            R5CompositeBucket(bucket="0.0-0.5", count=int(lo or 0)),
        ],
        healthy_gates={
            "avg_cost_per_judge_max": 0.010,
            "c1_c2_internal_agreement_min": 0.60,
            "error_rate_max": 0.05,
            "min_judges_run": 30,
        },
        is_healthy=healthy,
        window_days=int(days),
    )


# =============================================================================
# Phase 2 R6 — DAG trace telemetry (2026-05-18)
# =============================================================================
# R6 DAG lives in experiment_runs.runtime_state['dag'] JSONB (not a separate
# table). Top-level keys node_count + max_depth_seen are cheap aggregates;
# we deliberately don't iterate the nodes dict for status breakdown — that
# would require jsonb_each per row. Operators wanting per-node detail can
# pull a single run via /api/v1/runs/{run_id} (existing) and inspect the
# raw runtime_state.dag JSONB.


class R6RunSummary(BaseModel):
    run_id: int
    task_id: Optional[int] = None
    node_count: int
    max_depth: int
    root_id: Optional[str] = None
    current_selection: Optional[str] = None
    created_at: Optional[str] = None


class R6DepthBucket(BaseModel):
    depth: int
    run_count: int


class R6DagStatsOut(BaseModel):
    flags: Dict[str, bool]
    total_runs_with_dag: int
    total_nodes_across_runs: int
    avg_nodes_per_run: float
    max_node_count: int
    max_depth_observed: int
    depth_distribution: List[R6DepthBucket]
    recent_runs: List[R6RunSummary]
    window_days: int


@router.get("/r6/dag-stats", response_model=R6DagStatsOut)
async def r6_dag_stats(
    days: int = 7,
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> R6DagStatsOut:
    """R6 DAG trace telemetry — node count + depth distribution per run.

    Reads ``experiment_runs.runtime_state->'dag'`` JSONB. Healthy R6
    deployment: ``avg_nodes_per_run`` between 5 and 40 (too few = bandit
    not exploring; too many = pruning broken), ``max_depth_observed`` >= 3
    (DAG actually multi-level not just root + flat children), and depth
    distribution showing some spread (mining diverse expression families).

    ``window_days`` filters by ExperimentRun.started_at. Top-level JSONB
    keys (``node_count``, ``max_depth_seen``) are populated/maintained by
    backend/agents/graph/dag_state.py — empty/legacy runs (pre-R6 or
    ENABLE_DAG_TRACE=False) have no ``dag`` sub-key and are filtered out.
    """
    from sqlalchemy import text as _text
    from backend.config import settings as _stg

    flags = {
        "ENABLE_DAG_TRACE": bool(getattr(_stg, "ENABLE_DAG_TRACE", False)),
    }

    overall = (await db.execute(_text(
        "SELECT "
        "  COUNT(*) AS runs_with_dag, "
        "  COALESCE(SUM((runtime_state->'dag'->>'node_count')::int), 0) AS total_nodes, "
        "  COALESCE(MAX((runtime_state->'dag'->>'node_count')::int), 0) AS max_nodes, "
        "  COALESCE(MAX((runtime_state->'dag'->>'max_depth_seen')::int), 0) AS max_depth "
        "FROM experiment_runs "
        "WHERE started_at > now() - (:days || ' day')::interval "
        "  AND runtime_state ? 'dag' "
        "  AND runtime_state->'dag' ? 'node_count'"
    ), {"days": str(int(days))})).one()

    runs_with_dag, total_nodes, max_nodes, max_depth_global = overall
    runs_int = int(runs_with_dag or 0)
    total_nodes_int = int(total_nodes or 0)
    avg_nodes = round(total_nodes_int / runs_int, 2) if runs_int > 0 else 0.0

    depth_rows = (await db.execute(_text(
        "SELECT (runtime_state->'dag'->>'max_depth_seen')::int AS d, "
        "       COUNT(*) AS n "
        "FROM experiment_runs "
        "WHERE started_at > now() - (:days || ' day')::interval "
        "  AND runtime_state ? 'dag' "
        "  AND runtime_state->'dag' ? 'max_depth_seen' "
        "GROUP BY d "
        "ORDER BY d ASC"
    ), {"days": str(int(days))})).all()
    depth_distribution = [
        R6DepthBucket(depth=int(d or 0), run_count=int(n or 0))
        for d, n in depth_rows
    ]

    recent_rows = (await db.execute(_text(
        "SELECT id, task_id, "
        "  (runtime_state->'dag'->>'node_count')::int AS nc, "
        "  COALESCE((runtime_state->'dag'->>'max_depth_seen')::int, 0) AS md, "
        "  runtime_state->'dag'->>'root_id' AS root, "
        "  runtime_state->'dag'->>'current_selection' AS sel, "
        "  to_char(started_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS ts "
        "FROM experiment_runs "
        "WHERE started_at > now() - (:days || ' day')::interval "
        "  AND runtime_state ? 'dag' "
        "  AND runtime_state->'dag' ? 'node_count' "
        "ORDER BY started_at DESC "
        "LIMIT 20"
    ), {"days": str(int(days))})).all()
    recent = [
        R6RunSummary(
            run_id=int(rid),
            task_id=int(tid) if tid is not None else None,
            node_count=int(nc or 0),
            max_depth=int(md or 0),
            root_id=root or None,
            current_selection=sel or None,
            created_at=ts or None,
        )
        for rid, tid, nc, md, root, sel, ts in recent_rows
    ]

    return R6DagStatsOut(
        flags=flags,
        total_runs_with_dag=runs_int,
        total_nodes_across_runs=total_nodes_int,
        avg_nodes_per_run=avg_nodes,
        max_node_count=int(max_nodes or 0),
        max_depth_observed=int(max_depth_global or 0),
        depth_distribution=depth_distribution,
        recent_runs=recent,
        window_days=int(days),
    )


# =============================================================================
# G5 Phase A follow-up — trajectory crossover telemetry (2026-05-19)
# =============================================================================
# Reads g5_crossover_log (per-call) + outcome_alpha_ids reverse JOIN
# (back-filled by _incremental_save_alphas when offspring INSERT).
# Healthy deploy: ENABLE_G5_CROSSOVER=True AND total_crossover_calls > 0
# (LLM 真在 produce offspring) AND offspring_pass_rate > 0 (≥1 offspring
# 真 PASS — 证明 crossover 不只是 LLM hallucination)。


class G5StrategyBucket(BaseModel):
    strategy: str
    calls: int
    avg_offspring_count: float
    outcome_pass_count: int


class G5PillarPairBucket(BaseModel):
    pillar_pair: str   # e.g. "momentum→value"
    calls: int
    outcome_pass_count: int


class G5RecentEvent(BaseModel):
    id: int
    task_id: Optional[int] = None
    round_idx: Optional[int] = None
    parent_a_alpha_id: Optional[int] = None
    parent_b_alpha_id: Optional[int] = None
    offspring_count: int = 0
    outcome_pass_count: Optional[int] = None
    llm_cost_usd: Optional[float] = None
    created_at: Optional[str] = None


class G5CrossoverStatsOut(BaseModel):
    flags: Dict[str, bool]
    window_days: int
    total_crossover_calls: int
    total_offspring: int
    total_offspring_referenced_alphas: int
    offspring_pass_count: int
    offspring_pass_rate: float
    avg_offspring_per_call: float
    per_strategy: List[G5StrategyBucket]
    per_pillar_pair: List[G5PillarPairBucket]
    recent_events: List[G5RecentEvent]
    healthy_gates: Dict[str, float]
    is_healthy: bool


@router.get("/g5/crossover-stats", response_model=G5CrossoverStatsOut)
async def g5_crossover_stats(
    days: int = 7,
    top_n: int = 20,
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> G5CrossoverStatsOut:
    """G5 Phase A follow-up — trajectory crossover telemetry.

    Reads g5_crossover_log over the last ``days`` window + reverse JOIN
    alphas via outcome_alpha_ids JSONB (back-filled by _incremental_save
    _alphas). Six aggregates:
      1. Headline: total_crossover_calls, total_offspring, outcome PASS
         count + rate
      2. avg_offspring_per_call (LLM productivity — 0 implies prompt is
         too restrictive; 3+ implies LLM ignores top_k cap)
      3. per_strategy: counts + avg_offspring_count + outcome_pass per
         combination_strategy (5 strategies: weighted_sum /
         sequential_filter / cross_sectional_confirm / wrapper_graft /
         difference_filter) — shows which strategy LLM picks + which
         actually produces PASS
      4. per_pillar_pair: counts + outcome_pass for each
         "pillar_a→pillar_b" combination — shows which cross-pillar
         pairings are productive
      5. recent_events: top-N most recent calls (chronological newest first)
      6. Healthy gate: flag ON + total_calls > 0 + offspring_pass_rate > 0
    """
    from sqlalchemy import text as _text
    from backend.config import settings as _stg

    flags = {
        "ENABLE_G5_CROSSOVER": bool(getattr(_stg, "ENABLE_G5_CROSSOVER", False)),
    }

    head = (await db.execute(_text(
        "SELECT "
        "  COUNT(*) AS total_calls, "
        "  COALESCE(SUM(offspring_count), 0) AS total_offspring, "
        "  COALESCE(SUM(jsonb_array_length(COALESCE(outcome_alpha_ids, '[]'::jsonb))), 0) AS total_outcome_alphas, "
        "  COALESCE(SUM(outcome_pass_count), 0) AS total_pass "
        "FROM g5_crossover_log "
        "WHERE created_at > now() - (:days || ' day')::interval"
    ), {"days": str(int(days))})).one()
    total_calls = int(head[0] or 0)
    total_offspring = int(head[1] or 0)
    total_outcome = int(head[2] or 0)
    total_pass = int(head[3] or 0)
    pass_rate = round(total_pass / total_outcome, 4) if total_outcome > 0 else 0.0
    avg_offspring = round(total_offspring / total_calls, 2) if total_calls > 0 else 0.0

    # per_strategy: needs to unpack offspring_expressions JSONB — each entry
    # carries its own combination_strategy. Use jsonb_array_elements to
    # explode, then GROUP BY the strategy text.
    strat_rows = (await db.execute(_text(
        "WITH expanded AS ("
        "  SELECT id, "
        "         (elem->>'combination_strategy') AS strategy, "
        "         offspring_count, "
        "         outcome_pass_count "
        "  FROM g5_crossover_log, "
        "       jsonb_array_elements(COALESCE(offspring_expressions, '[]'::jsonb)) AS elem "
        "  WHERE created_at > now() - (:days || ' day')::interval "
        ") "
        "SELECT COALESCE(strategy, '(unspecified)') AS s, "
        "       COUNT(DISTINCT id) AS calls, "
        "       COALESCE(AVG(offspring_count), 0.0) AS avg_off, "
        "       COALESCE(SUM(outcome_pass_count), 0) AS pass_ct "
        "FROM expanded "
        "GROUP BY s "
        "ORDER BY calls DESC"
    ), {"days": str(int(days))})).all()
    per_strategy = [
        G5StrategyBucket(
            strategy=s or "(unspecified)",
            calls=int(c or 0),
            avg_offspring_count=round(float(ao or 0.0), 2),
            outcome_pass_count=int(pc or 0),
        )
        for s, c, ao, pc in strat_rows
    ]

    # per_pillar_pair: from parent_a_pillar / parent_b_pillar columns
    pillar_rows = (await db.execute(_text(
        "SELECT "
        "  COALESCE(parent_a_pillar, '?') || '→' || COALESCE(parent_b_pillar, '?') AS pair, "
        "  COUNT(*) AS n, "
        "  COALESCE(SUM(outcome_pass_count), 0) AS pass_ct "
        "FROM g5_crossover_log "
        "WHERE created_at > now() - (:days || ' day')::interval "
        "GROUP BY pair "
        "ORDER BY n DESC"
    ), {"days": str(int(days))})).all()
    per_pillar = [
        G5PillarPairBucket(
            pillar_pair=p or "?→?",
            calls=int(n or 0),
            outcome_pass_count=int(pc or 0),
        )
        for p, n, pc in pillar_rows
    ]

    # recent_events: top-N newest crossover events
    recent_rows = (await db.execute(_text(
        "SELECT id, task_id, round_idx, parent_a_alpha_id, parent_b_alpha_id, "
        "       offspring_count, outcome_pass_count, llm_cost_usd, "
        "       to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS ts "
        "FROM g5_crossover_log "
        "WHERE created_at > now() - (:days || ' day')::interval "
        "ORDER BY created_at DESC "
        "LIMIT :lim"
    ), {"days": str(int(days)), "lim": int(top_n)})).all()
    recent_events = [
        G5RecentEvent(
            id=int(rid),
            task_id=int(tid) if tid is not None else None,
            round_idx=int(ri) if ri is not None else None,
            parent_a_alpha_id=int(pa) if pa is not None else None,
            parent_b_alpha_id=int(pb) if pb is not None else None,
            offspring_count=int(oc or 0),
            outcome_pass_count=int(opc) if opc is not None else None,
            llm_cost_usd=float(cost) if cost is not None else None,
            created_at=ts or None,
        )
        for rid, tid, ri, pa, pb, oc, opc, cost, ts in recent_rows
    ]

    healthy = (
        bool(flags["ENABLE_G5_CROSSOVER"])
        and total_calls > 0
        and pass_rate > 0
    )

    return G5CrossoverStatsOut(
        flags=flags,
        window_days=int(days),
        total_crossover_calls=total_calls,
        total_offspring=total_offspring,
        total_offspring_referenced_alphas=total_outcome,
        offspring_pass_count=total_pass,
        offspring_pass_rate=pass_rate,
        avg_offspring_per_call=avg_offspring,
        per_strategy=per_strategy,
        per_pillar_pair=per_pillar,
        recent_events=recent_events,
        healthy_gates={
            "min_total_calls": 1.0,
            "min_offspring_pass_rate": 0.0,
        },
        is_healthy=healthy,
    )


# =============================================================================
# G8 Phase A follow-up — hypothesis forest telemetry (2026-05-19)
# =============================================================================
# Reads hypotheses table (forest pool) + alphas.metrics (reverse attribution
# via _g8_forest_referenced_ids stamp written by _incremental_save_alphas).
# Healthy deploy: ENABLE_HYPOTHESIS_FOREST_REUSE=True AND eligible_count > 0
# (forest has qualified rows to surface) AND reference_count > 0 (≥1 alpha
# generated under a forest-referenced context, i.e. the prompt block actually
# influenced production). reference_pass_rate is descriptive — Phase B
# decides whether to harden into a gate.


class ForestEntry(BaseModel):
    hypothesis_id: int
    statement: str
    pillar: Optional[str] = None
    region: str
    sharpe_avg: Optional[float] = None
    pass_count: int = 0
    alpha_count: int = 0
    status: Optional[str] = None
    times_referenced: int = 0


class ForestPillarBreakdown(BaseModel):
    pillar: str
    eligible_count: int
    avg_sharpe: float
    total_pass: int


class HypothesisForestOut(BaseModel):
    flags: Dict[str, bool]
    window_days: int
    region: Optional[str] = None
    eligible_count: int
    total_referenced_alphas: int
    reference_pass_count: int
    reference_pass_rate: float
    top_entries: List[ForestEntry]
    pillar_breakdown: List[ForestPillarBreakdown]
    healthy_gates: Dict[str, float]
    is_healthy: bool


@router.get("/hypothesis/forest", response_model=HypothesisForestOut)
async def hypothesis_forest(
    region: Optional[str] = None,
    days: int = 7,
    top_n: int = 10,
    min_pass_count: int = 2,
    min_sharpe_avg: float = 1.0,
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> HypothesisForestOut:
    """G8 Phase A follow-up — hypothesis forest telemetry.

    Reads three things:
      1. Eligible forest pool: PROMOTED/ACTIVE hypotheses in the region
         (or all regions when ``region`` omitted) with pass_count ≥
         ``min_pass_count`` AND sharpe_avg ≥ ``min_sharpe_avg``. These are
         what HypothesisService.fetch_cross_task_promoted surfaces to LLM.
      2. Reverse attribution: count alphas in the last ``days`` window
         whose ``metrics['_g8_forest_referenced_ids']`` is non-empty —
         the prompt-block having actual influence on production.
      3. Per-pillar breakdown of the forest pool for ops to see which
         pillars are over- / under-represented in the reference pool.

    Healthy gate (Phase A descriptive only):
      - flag ON
      - eligible_count > 0 (pool actually has qualifying rows)
      - total_referenced_alphas > 0 (prompt block reaching alpha persistence)
    """
    from sqlalchemy import text as _text
    from backend.config import settings as _stg

    flags = {
        "ENABLE_HYPOTHESIS_FOREST_REUSE": bool(
            getattr(_stg, "ENABLE_HYPOTHESIS_FOREST_REUSE", False)
        ),
    }

    where_region = "AND region = :region" if region else ""

    head = (await db.execute(_text(
        "SELECT COUNT(*) AS n "
        "FROM hypotheses "
        "WHERE is_active = TRUE "
        "  AND status IN ('ACTIVE', 'PROMOTED') "
        "  AND pass_count >= :min_pass "
        "  AND sharpe_avg IS NOT NULL "
        "  AND sharpe_avg >= :min_sharpe "
        f"  {where_region}"
    ), {
        "min_pass": int(min_pass_count),
        "min_sharpe": float(min_sharpe_avg),
        "region": region,
    })).one()
    eligible_count = int(head[0] or 0)

    top_rows = (await db.execute(_text(
        "SELECT id, statement, pillar, region, sharpe_avg, pass_count, "
        "       alpha_count, status "
        "FROM hypotheses "
        "WHERE is_active = TRUE "
        "  AND status IN ('ACTIVE', 'PROMOTED') "
        "  AND pass_count >= :min_pass "
        "  AND sharpe_avg IS NOT NULL "
        "  AND sharpe_avg >= :min_sharpe "
        f"  {where_region} "
        "ORDER BY sharpe_avg DESC, pass_count DESC, updated_at DESC "
        "LIMIT :lim"
    ), {
        "min_pass": int(min_pass_count),
        "min_sharpe": float(min_sharpe_avg),
        "region": region,
        "lim": int(top_n),
    })).all()

    times_ref_map: Dict[int, int] = {}
    if top_rows:
        ids = [int(r[0]) for r in top_rows]
        for hid in ids:
            cnt_row = (await db.execute(_text(
                "SELECT COUNT(*) FROM alphas "
                "WHERE created_at > now() - (:days || ' day')::interval "
                "  AND metrics ? '_g8_forest_referenced_ids' "
                "  AND metrics->'_g8_forest_referenced_ids' @> :hid_json"
            ), {
                "days": str(int(days)),
                "hid_json": f"[{hid}]",
            })).one()
            times_ref_map[hid] = int(cnt_row[0] or 0)

    top_entries = [
        ForestEntry(
            hypothesis_id=int(hid),
            statement=(stmt or "")[:200],
            pillar=pillar,
            region=reg,
            sharpe_avg=float(sh) if sh is not None else None,
            pass_count=int(pc or 0),
            alpha_count=int(ac or 0),
            status=st,
            times_referenced=times_ref_map.get(int(hid), 0),
        )
        for hid, stmt, pillar, reg, sh, pc, ac, st in top_rows
    ]

    refed = (await db.execute(_text(
        "SELECT "
        "  COUNT(*) AS total, "
        "  COUNT(*) FILTER (WHERE quality_status IN ('PASS','PASS_PROVISIONAL')) AS ok "
        "FROM alphas "
        "WHERE created_at > now() - (:days || ' day')::interval "
        "  AND metrics ? '_g8_forest_referenced_ids' "
        f"  {('AND region = :region' if region else '')}"
    ), {"days": str(int(days)), "region": region})).one()
    total_ref = int(refed[0] or 0)
    pass_ref = int(refed[1] or 0)
    pass_rate_ref = round(pass_ref / total_ref, 4) if total_ref > 0 else 0.0

    pillar_rows = (await db.execute(_text(
        "SELECT COALESCE(pillar, '(none)') AS p, "
        "       COUNT(*) AS n, "
        "       COALESCE(AVG(sharpe_avg), 0.0) AS avg_sh, "
        "       COALESCE(SUM(pass_count), 0) AS tot_pass "
        "FROM hypotheses "
        "WHERE is_active = TRUE "
        "  AND status IN ('ACTIVE', 'PROMOTED') "
        "  AND pass_count >= :min_pass "
        "  AND sharpe_avg IS NOT NULL "
        "  AND sharpe_avg >= :min_sharpe "
        f"  {where_region} "
        "GROUP BY p "
        "ORDER BY n DESC"
    ), {
        "min_pass": int(min_pass_count),
        "min_sharpe": float(min_sharpe_avg),
        "region": region,
    })).all()
    pillar_breakdown = [
        ForestPillarBreakdown(
            pillar=p,
            eligible_count=int(n or 0),
            avg_sharpe=round(float(avg_sh or 0.0), 4),
            total_pass=int(tot or 0),
        )
        for p, n, avg_sh, tot in pillar_rows
    ]

    healthy = (
        bool(flags["ENABLE_HYPOTHESIS_FOREST_REUSE"])
        and eligible_count > 0
        and total_ref > 0
    )

    return HypothesisForestOut(
        flags=flags,
        window_days=int(days),
        region=region,
        eligible_count=eligible_count,
        total_referenced_alphas=total_ref,
        reference_pass_count=pass_ref,
        reference_pass_rate=pass_rate_ref,
        top_entries=top_entries,
        pillar_breakdown=pillar_breakdown,
        healthy_gates={
            "min_eligible_count": 1.0,
            "min_total_referenced": 1.0,
        },
        is_healthy=healthy,
    )


# =============================================================================
# G2 Phase A — per-call LLM cost telemetry (2026-05-19)
# =============================================================================
# Reads llm_call_log written by cost_tracker.flush_round_async at round
# boundary. Healthy deploy: ENABLE_COST_TELEMETRY=True AND total_calls > 0
# (采集实际生效) AND error_rate < 0.10 (LLM API/provider 健康)。avg_cost_per_call
# 故意不卡硬上限 — Phase A 是描述性,operator 用此 endpoint 建 cost 基线再
# 决定 Phase C cost-ceiling 值。pillar 维度仅在 mining_agent begin_round
# 注入 strategy.regime 时非空,因此当前主要分布在 momentum / value / 等
# regime 标签下;未来 Phase B 可改成真 hypothesis pillar(需要 round 内事后
# 回填,推下一 PR)。


class CostByGroup(BaseModel):
    label: str
    calls: int
    tokens_total: int
    cost_usd: float
    avg_latency_ms: float
    success_rate: float


class CostHourBucket(BaseModel):
    hour_utc: str
    calls: int
    tokens_total: int
    cost_usd: float


class CostTaskRow(BaseModel):
    task_id: Optional[int] = None
    calls: int
    tokens_total: int
    cost_usd: float


class CostTelemetryOut(BaseModel):
    flags: Dict[str, bool]
    window_days: int
    total_calls: int
    successful_calls: int
    failed_calls: int
    error_rate: float
    total_tokens: int
    total_cost_usd: float
    avg_cost_per_call: float
    avg_tokens_per_call: float
    by_model: List[CostByGroup]
    by_node_key: List[CostByGroup]
    by_pillar: List[CostByGroup]
    top_tasks_by_cost: List[CostTaskRow]
    hourly_last_24h: List[CostHourBucket]
    healthy_gates: Dict[str, float]
    is_healthy: bool


@router.get("/cost/telemetry", response_model=CostTelemetryOut)
async def cost_telemetry(
    days: int = 7,
    top_n: int = 10,
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> CostTelemetryOut:
    """G2 Phase A cost telemetry — per-call LLM cost across all callers.

    Aggregates ``llm_call_log`` over the last ``days`` window. Covers普通
    round (hypothesis / code_gen / self_correct / distill / mutate) +
    R1b retry/mutate + macro narrative batch + R5 judge + any future LLM
    caller — same path goes through ``LLMService.call``.

    Healthy gate (Phase A, descriptive):
      * ENABLE_COST_TELEMETRY ON
      * total_calls > 0 (flag is actually capturing)
      * error_rate <= 0.10 (LLM provider healthy)

    avg_cost_per_call intentionally has no upper bound here — Phase A is
    establishing the baseline, not enforcing it. Phase C (≥7d observation
    later) introduces COST_CEILING_USD_PER_TASK_DAY with auto-throttling.
    """
    from sqlalchemy import text as _text
    from backend.config import settings as _stg

    flags = {
        "ENABLE_COST_TELEMETRY": bool(getattr(_stg, "ENABLE_COST_TELEMETRY", False)),
    }

    # Headline aggregate
    head = (await db.execute(_text(
        "SELECT "
        "  COUNT(*) AS calls, "
        "  COUNT(*) FILTER (WHERE success = true) AS ok, "
        "  COUNT(*) FILTER (WHERE success = false) AS bad, "
        "  COALESCE(SUM(tokens_total), 0) AS toks, "
        "  COALESCE(SUM(cost_usd), 0.0) AS cost "
        "FROM llm_call_log "
        "WHERE created_at > now() - (:days || ' day')::interval"
    ), {"days": str(int(days))})).one()
    calls, ok, bad, toks, cost = head
    calls_int = int(calls or 0)
    ok_int = int(ok or 0)
    bad_int = int(bad or 0)
    toks_int = int(toks or 0)
    cost_f = float(cost or 0.0)
    error_rate = round(bad_int / calls_int, 4) if calls_int > 0 else 0.0
    avg_cost = round(cost_f / calls_int, 6) if calls_int > 0 else 0.0
    avg_toks = round(toks_int / calls_int, 2) if calls_int > 0 else 0.0

    # by_model — group across all calls
    model_rows = (await db.execute(_text(
        "SELECT model, COUNT(*) AS n, COALESCE(SUM(tokens_total),0) AS toks, "
        "       COALESCE(SUM(cost_usd),0.0) AS cost, "
        "       COALESCE(AVG(latency_ms),0.0) AS lat, "
        "       COUNT(*) FILTER (WHERE success=true) AS ok "
        "FROM llm_call_log "
        "WHERE created_at > now() - (:days || ' day')::interval "
        "GROUP BY model ORDER BY cost DESC"
    ), {"days": str(int(days))})).all()
    by_model = [
        CostByGroup(
            label=m or "(unknown)",
            calls=int(n or 0),
            tokens_total=int(tt or 0),
            cost_usd=round(float(c or 0.0), 4),
            avg_latency_ms=round(float(lat or 0.0), 1),
            success_rate=round(int(okm or 0) / int(n or 1), 4),
        )
        for m, n, tt, c, lat, okm in model_rows
    ]

    node_rows = (await db.execute(_text(
        "SELECT COALESCE(node_key,'(none)') AS nk, COUNT(*) AS n, "
        "       COALESCE(SUM(tokens_total),0) AS toks, "
        "       COALESCE(SUM(cost_usd),0.0) AS cost, "
        "       COALESCE(AVG(latency_ms),0.0) AS lat, "
        "       COUNT(*) FILTER (WHERE success=true) AS ok "
        "FROM llm_call_log "
        "WHERE created_at > now() - (:days || ' day')::interval "
        "GROUP BY nk ORDER BY cost DESC"
    ), {"days": str(int(days))})).all()
    by_node_key = [
        CostByGroup(
            label=nk,
            calls=int(n or 0),
            tokens_total=int(tt or 0),
            cost_usd=round(float(c or 0.0), 4),
            avg_latency_ms=round(float(lat or 0.0), 1),
            success_rate=round(int(okm or 0) / int(n or 1), 4),
        )
        for nk, n, tt, c, lat, okm in node_rows
    ]

    pillar_rows = (await db.execute(_text(
        "SELECT COALESCE(pillar,'(none)') AS p, COUNT(*) AS n, "
        "       COALESCE(SUM(tokens_total),0) AS toks, "
        "       COALESCE(SUM(cost_usd),0.0) AS cost, "
        "       COALESCE(AVG(latency_ms),0.0) AS lat, "
        "       COUNT(*) FILTER (WHERE success=true) AS ok "
        "FROM llm_call_log "
        "WHERE created_at > now() - (:days || ' day')::interval "
        "GROUP BY p ORDER BY cost DESC"
    ), {"days": str(int(days))})).all()
    by_pillar = [
        CostByGroup(
            label=p,
            calls=int(n or 0),
            tokens_total=int(tt or 0),
            cost_usd=round(float(c or 0.0), 4),
            avg_latency_ms=round(float(lat or 0.0), 1),
            success_rate=round(int(okm or 0) / int(n or 1), 4),
        )
        for p, n, tt, c, lat, okm in pillar_rows
    ]

    task_rows = (await db.execute(_text(
        "SELECT task_id, COUNT(*) AS n, "
        "       COALESCE(SUM(tokens_total),0) AS toks, "
        "       COALESCE(SUM(cost_usd),0.0) AS cost "
        "FROM llm_call_log "
        "WHERE created_at > now() - (:days || ' day')::interval "
        "  AND task_id IS NOT NULL "
        "GROUP BY task_id ORDER BY cost DESC LIMIT :lim"
    ), {"days": str(int(days)), "lim": int(top_n)})).all()
    top_tasks = [
        CostTaskRow(
            task_id=int(tid) if tid is not None else None,
            calls=int(n or 0),
            tokens_total=int(tt or 0),
            cost_usd=round(float(c or 0.0), 4),
        )
        for tid, n, tt, c in task_rows
    ]

    hour_rows = (await db.execute(_text(
        "SELECT to_char(date_trunc('hour', created_at AT TIME ZONE 'UTC'), "
        "       'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS h, "
        "       COUNT(*) AS n, "
        "       COALESCE(SUM(tokens_total),0) AS toks, "
        "       COALESCE(SUM(cost_usd),0.0) AS cost "
        "FROM llm_call_log "
        "WHERE created_at > now() - interval '24 hours' "
        "GROUP BY h ORDER BY h ASC"
    ))).all()
    hourly = [
        CostHourBucket(
            hour_utc=h or "",
            calls=int(n or 0),
            tokens_total=int(tt or 0),
            cost_usd=round(float(c or 0.0), 4),
        )
        for h, n, tt, c in hour_rows
    ]

    healthy = (
        bool(flags["ENABLE_COST_TELEMETRY"])
        and calls_int > 0
        and error_rate <= 0.10
    )

    return CostTelemetryOut(
        flags=flags,
        window_days=int(days),
        total_calls=calls_int,
        successful_calls=ok_int,
        failed_calls=bad_int,
        error_rate=error_rate,
        total_tokens=toks_int,
        total_cost_usd=round(cost_f, 4),
        avg_cost_per_call=avg_cost,
        avg_tokens_per_call=avg_toks,
        by_model=by_model,
        by_node_key=by_node_key,
        by_pillar=by_pillar,
        top_tasks_by_cost=top_tasks,
        hourly_last_24h=hourly,
        healthy_gates={
            "error_rate_max": 0.10,
            "min_total_calls": 1.0,
        },
        is_healthy=healthy,
    )


# =============================================================================
# Pitfall classifier telemetry — follow-up Major #2 from negative-knowledge
# KB pollution fix. Aggregates classifier_call_log: one row per LLM-supplied
# pitfall the `_classify_pitfall_error_type` helper saw, with resolved
# category or NULL (noise drop). Operators use this to confirm the helper
# is actually firing post-deploy, watch drop rate trend, and surface
# top noise error_type strings for keyword tuning.


class ClassifierBreakdownRow(BaseModel):
    label: str
    total: int
    noise_drops: int
    threshold_stamps: int
    robustness_stamps: int
    static_finding_stamps: int
    drop_rate: float


class ClassifierTopDroppedRow(BaseModel):
    error_type: str
    count: int


class ClassifierStatsOut(BaseModel):
    window_days: int
    headline: ClassifierBreakdownRow
    by_region: List[ClassifierBreakdownRow]
    by_day: List[ClassifierBreakdownRow]
    top_dropped_error_types: List[ClassifierTopDroppedRow]


def _build_classifier_row(
    label: str, total: int, drops: int, t: int, r: int, s: int
) -> ClassifierBreakdownRow:
    return ClassifierBreakdownRow(
        label=label,
        total=total,
        noise_drops=drops,
        threshold_stamps=t,
        robustness_stamps=r,
        static_finding_stamps=s,
        drop_rate=round(drops / total, 4) if total > 0 else 0.0,
    )


@router.get("/classifier/stats", response_model=ClassifierStatsOut)
async def classifier_stats(
    days: int = 7,
    top_n: int = 20,
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> ClassifierStatsOut:
    """Pitfall-classifier drop-rate + category breakdown."""
    from sqlalchemy import text as _text

    window = f"now() - (:days || ' day')::interval"
    _category_filters = (
        "COUNT(*) FILTER (WHERE resolved_category IS NULL) AS drops, "
        "COUNT(*) FILTER (WHERE resolved_category = 'threshold') AS t, "
        "COUNT(*) FILTER (WHERE resolved_category = 'robustness') AS r, "
        "COUNT(*) FILTER (WHERE resolved_category = 'static_finding') AS s "
    )

    head = (await db.execute(_text(
        f"SELECT COUNT(*) AS n, {_category_filters} "
        f"FROM classifier_call_log WHERE created_at > {window}"
    ), {"days": str(int(days))})).one()
    headline = _build_classifier_row(
        "all", int(head[0] or 0), int(head[1] or 0),
        int(head[2] or 0), int(head[3] or 0), int(head[4] or 0),
    )

    region_rows = (await db.execute(_text(
        f"SELECT COALESCE(region, '(none)') AS lbl, COUNT(*) AS n, {_category_filters} "
        f"FROM classifier_call_log WHERE created_at > {window} "
        f"GROUP BY lbl ORDER BY n DESC"
    ), {"days": str(int(days))})).all()
    by_region = [
        _build_classifier_row(lbl, int(n or 0), int(d or 0), int(t or 0), int(r or 0), int(s or 0))
        for lbl, n, d, t, r, s in region_rows
    ]

    day_rows = (await db.execute(_text(
        f"SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS d, "
        f"COUNT(*) AS n, {_category_filters} "
        f"FROM classifier_call_log WHERE created_at > {window} "
        f"GROUP BY d ORDER BY d ASC"
    ), {"days": str(int(days))})).all()
    by_day = [
        _build_classifier_row(d or "", int(n or 0), int(dr or 0), int(t or 0), int(r or 0), int(s or 0))
        for d, n, dr, t, r, s in day_rows
    ]

    top_rows = (await db.execute(_text(
        f"SELECT COALESCE(error_type, '(none)') AS et, COUNT(*) AS n "
        f"FROM classifier_call_log "
        f"WHERE created_at > {window} AND resolved_category IS NULL "
        f"GROUP BY et ORDER BY n DESC LIMIT :lim"
    ), {"days": str(int(days)), "lim": int(top_n)})).all()
    top_dropped = [
        ClassifierTopDroppedRow(error_type=et, count=int(n or 0))
        for et, n in top_rows
    ]

    return ClassifierStatsOut(
        window_days=int(days),
        headline=headline,
        by_region=by_region,
        by_day=by_day,
        top_dropped_error_types=top_dropped,
    )


# =============================================================================
# G1 Phase A direction-bandit telemetry (2026-05-19)
# =============================================================================
# Aggregates the dedicated ``direction_bandit_log`` table over a configurable
# window. The bandit's CURRENT posterior lives in mining_tasks.config
# JSONB (per-task) — this endpoint exposes the cumulative cross-task signal:
#
#   - per-arm pulls + reward (which arm Thompson keeps picking?)
#   - per-segment activity (which (region, dataset_category, failure_pattern)
#     cells the bandit is actually seeing — sparse segments stay cold and
#     fall back to global prior)
#   - per-arm PASS rate from joining direction_bandit_log → alphas via
#     metrics['_direction_bandit_recommended_arm'] (G1 Phase A stamp)
#   - rough regret = mean_reward(best arm) - mean_reward(actual selections)
#
# Phase A semantics: bandit is in shadow-soft mode (recommendation only —
# LLM gets a prompt hint, may override). This telemetry feeds the Phase
# 1 R2/Q7 GO gate per plan §1.9: "≥ 1 segment with ≥ 10 selects" + reward
# spread between arms > noise. Phase B/C may promote to a hard driving
# signal once those gates are met.


class DirectionBanditArmStatOut(BaseModel):
    arm: str
    pulls: int
    avg_observed_reward: float
    sample_size_for_reward: int  # rows with non-NULL observed_reward
    cold_start_pulls: int        # pulls made from global prior (segment was cold)
    pass_rate: Optional[float] = None  # joined from alphas table on the metric stamp
    pass_sample_size: int = 0


class DirectionBanditSegmentStatOut(BaseModel):
    segment_id: str
    region: Optional[str] = None
    dataset_category: Optional[str] = None
    failure_pattern: Optional[str] = None
    total_pulls: int
    distinct_arms: int


class DirectionBanditTelemetryOut(BaseModel):
    flags: Dict[str, bool]
    window_days: int
    total_log_rows: int
    distinct_tasks: int
    distinct_segments: int
    by_arm: List[DirectionBanditArmStatOut]
    by_segment: List[DirectionBanditSegmentStatOut]
    best_arm: Optional[str] = None  # by avg_observed_reward (sample_size > 0)
    best_arm_avg_reward: Optional[float] = None
    approx_regret: Optional[float] = None  # mean(best) - mean(actual)
    # Phase 1 R2/Q7 GO-gate readiness signal — at least one segment with
    # ≥ DIRECTION_BANDIT_GO_GATE_MIN_PULLS observed selects.
    go_gate_min_pulls: int
    go_gate_segments_ready: int
    is_healthy: bool


@router.get(
    "/direction-bandit/telemetry",
    response_model=DirectionBanditTelemetryOut,
)
async def direction_bandit_telemetry(
    days: int = 7,
    top_segments: int = 10,
    _token: str = Depends(_require_ops_token),
    db: AsyncSession = Depends(get_db),
) -> DirectionBanditTelemetryOut:
    """G1 Phase A direction-bandit telemetry — per-arm pull/reward/PASS-rate.

    Aggregates ``direction_bandit_log`` over the last ``days`` window plus
    a one-shot PASS-rate join against ``alphas.metrics``\\->>'_direction_bandit
    _recommended_arm' (G1 Phase A stamp from persistence node).

    Healthy gate (Phase A, descriptive):
      * ``ENABLE_DIRECTION_BANDIT`` flag ON
      * total_log_rows > 0 (the off-policy log is actually capturing)
      * At least one segment with ≥ 10 pulls (Phase 1 GO-gate signal)

    Phase A is observation-only — this endpoint never blocks task execution.
    Operator uses the regret + per-arm reward spread to decide whether to
    promote to Phase B/C (hard driving). Plan ref: §1.9 GO-gate.
    """
    from sqlalchemy import text as _text
    from backend.config import settings as _stg

    flags = {
        "ENABLE_DIRECTION_BANDIT": bool(
            getattr(_stg, "ENABLE_DIRECTION_BANDIT", False)
        ),
    }
    go_gate_min = int(
        getattr(_stg, "DIRECTION_BANDIT_GO_GATE_MIN_PULLS", 10)
    )

    # 1. Headline — total rows, distinct tasks, distinct segments
    head = (await db.execute(_text(
        "SELECT COUNT(*) AS rows, "
        "       COUNT(DISTINCT task_id) AS tasks, "
        "       COUNT(DISTINCT segment_id) AS segs "
        "FROM direction_bandit_log "
        "WHERE created_at > now() - (:days || ' day')::interval"
    ), {"days": str(int(days))})).one()
    rows_total, distinct_tasks, distinct_segs = head
    rows_total = int(rows_total or 0)
    distinct_tasks = int(distinct_tasks or 0)
    distinct_segs = int(distinct_segs or 0)

    # 2. Per-arm — pulls, avg observed_reward (over non-NULL only — round 1's
    #    NULL reward must NOT drag the average down), cold-start fraction.
    arm_rows = (await db.execute(_text(
        "SELECT selected_arm, "
        "       COUNT(*) AS pulls, "
        "       COALESCE(AVG(observed_reward) FILTER (WHERE observed_reward IS NOT NULL), 0.0) AS avg_r, "
        "       COUNT(*) FILTER (WHERE observed_reward IS NOT NULL) AS sample, "
        "       COUNT(*) FILTER (WHERE cold_start = 'true') AS cold_pulls "
        "FROM direction_bandit_log "
        "WHERE created_at > now() - (:days || ' day')::interval "
        "GROUP BY selected_arm "
        "ORDER BY pulls DESC"
    ), {"days": str(int(days))})).all()

    # 3. Per-arm PASS rate — UNION ALL of PASS-path (alphas.metrics JSONB key
    #    stamped by G1 Phase A) and FAIL-path (alpha_failures.bandit_arm_
    #    recommended column stamped by G1 follow-up). The previous SQL was
    #    PASS-only which gave a half-blind posterior (Phase B GO gate could
    #    not be calibrated because denominator was missing fails). Now
    #    denominator = PASS + FAIL on the same arm = true Bayesian sample.
    #    LEFT joins both sources so an arm appearing in only one table is
    #    still counted.
    pass_rate_map: Dict[str, tuple] = {}  # arm -> (pass_n, total_n)
    try:
        pass_rows = (await db.execute(_text(
            "WITH arm_outcomes AS ( "
            "  SELECT metrics->>'_direction_bandit_recommended_arm' AS arm, "
            "         (quality_status IN ('PASS','PASS_PROVISIONAL'))::int AS is_pass "
            "  FROM alphas "
            "  WHERE created_at > now() - (:days || ' day')::interval "
            "    AND metrics ? '_direction_bandit_recommended_arm' "
            "  UNION ALL "
            "  SELECT bandit_arm_recommended AS arm, "
            "         0 AS is_pass "
            "  FROM alpha_failures "
            "  WHERE created_at > now() - (:days || ' day')::interval "
            "    AND bandit_arm_recommended IS NOT NULL "
            ") "
            "SELECT arm, COUNT(*) AS n, SUM(is_pass) AS p "
            "FROM arm_outcomes "
            "GROUP BY arm"
        ), {"days": str(int(days))})).all()
        for arm, n, p in pass_rows:
            if arm:
                pass_rate_map[str(arm)] = (int(p or 0), int(n or 0))
    except Exception:
        # If alphas/alpha_failures table missing / migration not applied /
        # G1 stamp keys not yet populated, gracefully report None per-arm.
        pass_rate_map = {}

    by_arm: List[DirectionBanditArmStatOut] = []
    best_arm: Optional[str] = None
    best_avg: float = -1.0
    weighted_actual: float = 0.0
    total_sample: int = 0
    for arm, pulls, avg_r, sample, cold_pulls in arm_rows:
        arm_str = str(arm or "")
        sample_int = int(sample or 0)
        avg_r_f = round(float(avg_r or 0.0), 6)
        pulls_int = int(pulls or 0)
        cold_int = int(cold_pulls or 0)
        pass_info = pass_rate_map.get(arm_str)
        pass_rate: Optional[float] = None
        pass_n_total: int = 0
        if pass_info and pass_info[1] > 0:
            pass_rate = round(pass_info[0] / pass_info[1], 4)
            pass_n_total = pass_info[1]
        by_arm.append(DirectionBanditArmStatOut(
            arm=arm_str,
            pulls=pulls_int,
            avg_observed_reward=avg_r_f,
            sample_size_for_reward=sample_int,
            cold_start_pulls=cold_int,
            pass_rate=pass_rate,
            pass_sample_size=pass_n_total,
        ))
        if sample_int > 0:
            weighted_actual += avg_r_f * sample_int
            total_sample += sample_int
            if avg_r_f > best_avg:
                best_avg = avg_r_f
                best_arm = arm_str

    best_arm_avg_reward: Optional[float] = None
    approx_regret: Optional[float] = None
    if best_arm is not None and total_sample > 0:
        best_arm_avg_reward = round(best_avg, 6)
        actual_avg = weighted_actual / total_sample
        approx_regret = round(max(0.0, best_avg - actual_avg), 6)

    # 4. Top segments by activity — useful to see whether the bandit is
    #    seeing diverse contexts or hammering one (region, dataset, failure)
    #    cell. We expose distinct_arms per segment to spot mono-arm segments.
    seg_rows = (await db.execute(_text(
        "SELECT segment_id, "
        "       MAX(region) AS region, "
        "       MAX(dataset_category) AS dscat, "
        "       MAX(failure_pattern) AS fp, "
        "       COUNT(*) AS pulls, "
        "       COUNT(DISTINCT selected_arm) AS distinct_arms "
        "FROM direction_bandit_log "
        "WHERE created_at > now() - (:days || ' day')::interval "
        "GROUP BY segment_id "
        "ORDER BY pulls DESC "
        "LIMIT :lim"
    ), {"days": str(int(days)), "lim": int(top_segments)})).all()
    by_segment = [
        DirectionBanditSegmentStatOut(
            segment_id=str(sid or ""),
            region=region,
            dataset_category=dscat,
            failure_pattern=fp,
            total_pulls=int(pulls or 0),
            distinct_arms=int(distinct_arms or 0),
        )
        for sid, region, dscat, fp, pulls, distinct_arms in seg_rows
    ]

    # 5. GO gate — how many segments have crossed the min-pulls threshold
    #    (Phase 1 plan §1.9 R2/Q7 GO gate signal). Computed separately so it
    #    isn't capped by top_segments LIMIT.
    gate_row = (await db.execute(_text(
        "SELECT COUNT(*) FROM ("
        "  SELECT segment_id FROM direction_bandit_log "
        "  WHERE created_at > now() - (:days || ' day')::interval "
        "  GROUP BY segment_id "
        "  HAVING COUNT(*) >= :minp"
        ") sq"
    ), {"days": str(int(days)), "minp": int(go_gate_min)})).scalar_one_or_none()
    gate_ready = int(gate_row or 0)

    healthy = (
        bool(flags["ENABLE_DIRECTION_BANDIT"])
        and rows_total > 0
        and gate_ready >= 1
    )

    return DirectionBanditTelemetryOut(
        flags=flags,
        window_days=int(days),
        total_log_rows=rows_total,
        distinct_tasks=distinct_tasks,
        distinct_segments=distinct_segs,
        by_arm=by_arm,
        by_segment=by_segment,
        best_arm=best_arm,
        best_arm_avg_reward=best_arm_avg_reward,
        approx_regret=approx_regret,
        go_gate_min_pulls=go_gate_min,
        go_gate_segments_ready=gate_ready,
        is_healthy=healthy,
    )


# =============================================================================
# Phase 15-D PR2 cascade drain — DELETED post tier-system removal (2026-05-18)
# =============================================================================
# The /cascade-deprecation/drain endpoint (and its sibling /readiness above)
# read/wrote the dropped task.mining_mode column. With cascade permanently
# retired and the column gone, both endpoints are deleted entirely.
