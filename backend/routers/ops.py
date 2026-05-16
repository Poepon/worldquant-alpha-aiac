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
    """Affected-entries subset — only entries whose row currently lists
    bad ops AND would have been (or was) deactivated.

    Returns the same row schema as ``affected_entries`` in /latest. We
    don't separately hit the DB — the monitor's md already tells us
    which rows were flagged; the task is the source of truth for the
    "deactivated" decision (knowledge_entries.is_active toggling).
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
