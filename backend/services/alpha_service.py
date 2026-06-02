"""
Alpha Service - Business logic for alpha management

Provides methods for:
- Listing and filtering alphas
- Alpha details and trace retrieval
- Human feedback submission
- Statistics and aggregations
"""

import logging
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
from dataclasses import dataclass
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func, and_

from backend.services.base import BaseService
from backend.repositories import AlphaRepository
from backend.protocols.repository_protocol import PaginationParams, PaginatedResult
from backend.models import Alpha, AlphaStatusTransition, TraceStep

logger = logging.getLogger("services.alpha")


@dataclass
class AlphaListFilters:
    """Filters for alpha listing."""
    region: Optional[str] = None
    quality_status: Optional[str] = None
    human_feedback: Optional[str] = None
    dataset_id: Optional[str] = None
    task_id: Optional[int] = None
    # Submit-state filter (server-side so pagination/total stay honest):
    #   submitted   → date_submitted IS NOT NULL
    #   submittable → can_submit IS TRUE AND date_submitted IS NULL
    #   rejected    → can_submit IS FALSE
    #   unchecked   → can_submit IS NULL
    # None/other → no submit-state constraint.
    submit_state: Optional[str] = None
    # Delay setting filter (0 = delay-0 native, 1 = delay-1; other values rare)
    delay: Optional[int] = None
    # Expression substring search (case-insensitive ILIKE)
    expression_search: Optional[str] = None
    # IS metric range filters (None = no bound)
    min_sharpe: Optional[float] = None
    max_sharpe: Optional[float] = None
    min_fitness: Optional[float] = None
    max_fitness: Optional[float] = None
    min_turnover: Optional[float] = None
    max_turnover: Optional[float] = None
    min_returns: Optional[float] = None
    max_returns: Optional[float] = None


# Sort key mapping: external name -> SQLAlchemy column attribute. Keeps the
# API stable while DB column names ("is_*") remain implementation detail.
_SORT_COLUMN_MAP = {
    "sharpe": "is_sharpe",
    "fitness": "is_fitness",
    "turnover": "is_turnover",
    "returns": "is_returns",
    "drawdown": "is_drawdown",
    "created_at": "date_created",
    "date_created": "date_created",
    "id": "id",
    "region": "region",
    "quality_status": "quality_status",
}


@dataclass
class AlphaListItem:
    """Simplified alpha for list views."""
    id: int
    alpha_id: Optional[str]
    type: str
    name: Optional[str]
    expression: str
    region: Optional[str]
    dataset_id: Optional[str]
    quality_status: str
    human_feedback: str
    sharpe: Optional[float]
    returns: Optional[float]
    turnover: Optional[float]
    drawdown: Optional[float]
    margin: Optional[float]
    fitness: Optional[float]
    created_at: Optional[datetime]
    self_corr: Optional[float] = None
    self_corr_source: Optional[str] = None
    # 2026-05-19: surface submit-state to AlphaList UI so 已提交/可提交/
    # 不可提交/未检 tag 不再永远 fallback 到 "未检"。Frontend
    # `AlphaList.jsx:78-83` 已经按 date_submitted + can_submit 过滤,但响应
    # 体之前漏了这俩字段。
    date_submitted: Optional[datetime] = None
    can_submit: Optional[bool] = None


@dataclass
class AlphaDetail:
    """Full alpha details."""
    id: int
    alpha_id: Optional[str]
    task_id: Optional[int]
    expression: str
    hypothesis: Optional[str]
    logic_explanation: Optional[str]
    region: Optional[str]
    universe: Optional[str]
    dataset_id: Optional[str]
    fields_used: List[str]
    operators_used: List[str]
    status: str
    quality_status: str
    human_feedback: str
    feedback_comment: Optional[str]
    metrics: Dict[str, Any]
    is_metrics: Dict[str, Any]
    os_metrics: Dict[str, Any]
    created_at: Optional[datetime]
    date_submitted: Optional[datetime]
    can_submit: Optional[bool]


class AlphaService(BaseService):
    """
    Service for alpha-related operations.
    
    Provides a clean interface for alpha management,
    abstracting database operations from routers.
    """
    
    def __init__(self, db: AsyncSession):
        super().__init__(db)
        self.alpha_repo = AlphaRepository(db)
    
    # =========================================================================
    # List Operations
    # =========================================================================
    
    async def list_alphas(
        self,
        filters: AlphaListFilters,
        sort_by: str = "date_created",
        sort_order: str = "desc",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[List[AlphaListItem], int]:
        """
        List alphas with filtering and sorting.
        
        Args:
            filters: Filter criteria
            sort_by: Column to sort by
            sort_order: 'asc' or 'desc'
            limit: Maximum results
            offset: Pagination offset
            
        Returns:
            Tuple of (items, total_count)
        """
        # Build query
        query = select(Alpha)
        count_query = select(func.count()).select_from(Alpha)
        
        # Apply filters
        if filters.region:
            query = query.where(Alpha.region == filters.region)
            count_query = count_query.where(Alpha.region == filters.region)
        
        if filters.quality_status:
            query = query.where(Alpha.quality_status == filters.quality_status)
            count_query = count_query.where(Alpha.quality_status == filters.quality_status)
        
        if filters.human_feedback:
            query = query.where(Alpha.human_feedback == filters.human_feedback)
            count_query = count_query.where(Alpha.human_feedback == filters.human_feedback)
        
        if filters.dataset_id:
            query = query.where(Alpha.dataset_id == filters.dataset_id)
            count_query = count_query.where(Alpha.dataset_id == filters.dataset_id)
        
        if filters.task_id:
            query = query.where(Alpha.task_id == filters.task_id)
            count_query = count_query.where(Alpha.task_id == filters.task_id)

        if filters.delay is not None:
            query = query.where(Alpha.delay == filters.delay)
            count_query = count_query.where(Alpha.delay == filters.delay)

        if filters.expression_search:
            pattern = f"%{filters.expression_search}%"
            query = query.where(Alpha.expression.ilike(pattern))
            count_query = count_query.where(Alpha.expression.ilike(pattern))

        # Submit-state filter — applied to BOTH the page query and the count
        # query so the reported total matches the rows actually returned. The
        # frontend used to do this client-side over the current page only,
        # which made the pagination total lie.
        submit_cond = None
        if filters.submit_state == "submitted":
            submit_cond = Alpha.date_submitted.isnot(None)
        elif filters.submit_state == "submittable":
            submit_cond = and_(
                Alpha.can_submit.is_(True), Alpha.date_submitted.is_(None)
            )
        elif filters.submit_state == "rejected":
            submit_cond = Alpha.can_submit.is_(False)
        elif filters.submit_state == "unchecked":
            submit_cond = Alpha.can_submit.is_(None)
        if submit_cond is not None:
            query = query.where(submit_cond)
            count_query = count_query.where(submit_cond)

        # Numeric range filters on IS metrics
        for column, lo, hi in (
            (Alpha.is_sharpe,   filters.min_sharpe,   filters.max_sharpe),
            (Alpha.is_fitness,  filters.min_fitness,  filters.max_fitness),
            (Alpha.is_turnover, filters.min_turnover, filters.max_turnover),
            (Alpha.is_returns,  filters.min_returns,  filters.max_returns),
        ):
            if lo is not None:
                query = query.where(column >= lo)
                count_query = count_query.where(column >= lo)
            if hi is not None:
                query = query.where(column <= hi)
                count_query = count_query.where(column <= hi)

        # Get total count
        total = (await self.db.execute(count_query)).scalar() or 0

        # Apply sorting via the public sort-key map. Unknown keys fall back to
        # date_created to avoid leaking arbitrary column access.
        sort_attr = _SORT_COLUMN_MAP.get(sort_by, "date_created")
        sort_column = getattr(Alpha, sort_attr, Alpha.date_created)
        if sort_order.lower() == "desc":
            query = query.order_by(sort_column.desc().nullslast())
        else:
            query = query.order_by(sort_column.asc().nullsfirst())
        
        query = query.limit(limit).offset(offset)
        
        result = await self.db.execute(query)
        alphas = result.scalars().all()
        
        # Convert to list items
        items = [self._to_list_item(a) for a in alphas]

        return items, total

    async def get_alpha_stats(self, region: Optional[str] = None) -> Dict[str, Any]:
        """Aggregate counts for the Alpha-list summary strip.

        Returns total + per-quality_status breakdown + submit-state buckets.
        Optionally scoped to a single region so the strip can track the
        region the user is filtering on. These are independent of the list's
        own metric/expression filters by design — the strip is an at-a-glance
        portfolio overview, not a reflection of the active table query.
        """
        base_conds = [Alpha.region == region] if region else []

        def _count(*conds):
            q = select(func.count()).select_from(Alpha)
            for c in (*base_conds, *conds):
                q = q.where(c)
            return q

        status_q = select(Alpha.quality_status, func.count()).select_from(Alpha)
        for c in base_conds:
            status_q = status_q.where(c)
        status_q = status_q.group_by(Alpha.quality_status)
        # Coalesce NULL quality_status into the PENDING bucket. ACCUMULATE
        # rather than dict-overwrite: a NULL group and a literal "PENDING"
        # group are distinct rows here, and a plain `{key: count}` comprehension
        # would let the second silently clobber the first and drop a count.
        by_status: Dict[str, int] = {}
        for status, cnt in (await self.db.execute(status_q)).all():
            key = status or "PENDING"
            by_status[key] = by_status.get(key, 0) + cnt

        # total == sum over every status group (NULL group included), so derive
        # it instead of issuing a separate COUNT(*) round-trip.
        total = sum(by_status.values())

        submitted = (
            await self.db.execute(_count(Alpha.date_submitted.isnot(None)))
        ).scalar() or 0
        submittable = (
            await self.db.execute(
                _count(Alpha.can_submit.is_(True), Alpha.date_submitted.is_(None))
            )
        ).scalar() or 0
        rejected = (
            await self.db.execute(_count(Alpha.can_submit.is_(False)))
        ).scalar() or 0
        unchecked = (
            await self.db.execute(_count(Alpha.can_submit.is_(None)))
        ).scalar() or 0

        return {
            "total": total,
            "by_status": by_status,
            "submitted": submitted,
            "submittable": submittable,
            "rejected": rejected,
            "unchecked": unchecked,
        }

    def _to_list_item(self, alpha: Alpha) -> AlphaListItem:
        """Convert Alpha model to AlphaListItem."""
        expression = alpha.expression or "N/A"
        if len(expression) > 100:
            expression = expression[:100] + "..."
        
        margin = None
        if alpha.is_metrics and isinstance(alpha.is_metrics, dict):
            margin = alpha.is_metrics.get("margin")

        # V-26.77 follow-up #3: surface locally-measured self_corr + its
        # provenance so list views can tag alphas with the BRAIN/local/unknown
        # trust source (see CorrelationService.get_with_fallback). The value
        # is None when source not in {local, brain} per the writing contract
        # in agents/graph/nodes/evaluation.py.
        self_corr = None
        self_corr_source = None
        if alpha.metrics and isinstance(alpha.metrics, dict):
            self_corr = alpha.metrics.get("_self_corr")
            self_corr_source = alpha.metrics.get("_self_corr_source")

        return AlphaListItem(
            id=alpha.id,
            alpha_id=alpha.alpha_id,
            type=alpha.type or "REGULAR",
            name=alpha.name,
            expression=expression,
            region=alpha.region,
            dataset_id=alpha.dataset_id,
            quality_status=alpha.quality_status or "PENDING",
            human_feedback=alpha.human_feedback or "NONE",
            sharpe=alpha.is_sharpe,
            returns=alpha.is_returns,
            turnover=alpha.is_turnover,
            drawdown=alpha.is_drawdown,
            margin=margin,
            fitness=alpha.is_fitness,
            created_at=alpha.date_created or alpha.created_at,
            self_corr=self_corr,
            self_corr_source=self_corr_source,
            date_submitted=alpha.date_submitted,
            can_submit=alpha.can_submit,
        )
    
    # =========================================================================
    # Get Operations
    # =========================================================================
    
    async def get_alpha(self, alpha_id: int) -> Optional[AlphaDetail]:
        """
        Get detailed alpha information.
        
        Args:
            alpha_id: Database ID of the alpha
            
        Returns:
            AlphaDetail or None if not found
        """
        alpha = await self.alpha_repo.get_by_id(alpha_id)
        if not alpha:
            return None
        
        return self._to_detail(alpha)
    
    async def refresh_can_submit(
        self,
        alpha_pk: int,
        brain_adapter=None,
    ) -> Optional[Dict[str, Any]]:
        """Re-fetch BRAIN GET /alphas/{id}, recompute can_submit, persist.

        Writes:
          - alphas.can_submit (top-level column for fast filter)
          - metrics._brain_failed_checks (list of compact FAIL items)
          - metrics._brain_pending_checks (list of compact PENDING items)
          - metrics._brain_can_submit (mirror of can_submit, kept for legacy
            consumers that already read this field — see evaluation.py line 592)

        BRAIN unreachable / empty response → return None (no overwrite).
        Caller is responsible for owning the BrainAdapter lifecycle (passes it
        in to keep this method test-friendly).

        Returns dict {can_submit, failed_checks, pending_checks} on success,
        None if the alpha is missing alpha_id or BRAIN call failed.
        """
        from backend.can_submit import compute_can_submit

        alpha = await self.alpha_repo.get_by_id(alpha_pk)
        if not alpha or not alpha.alpha_id:
            return None

        if brain_adapter is None:
            from backend.adapters.brain_adapter import BrainAdapter
            async with BrainAdapter() as ba:
                detail = await ba.get_alpha(alpha.alpha_id)
        else:
            detail = await brain_adapter.get_alpha(alpha.alpha_id)

        if not detail:
            return None

        # V-26.77 follow-up #3: pipe the locally-measured self_corr through so
        # PENDING BRAIN SELF_CORRELATION can't whitewash an alpha that we
        # already know is correlated with the OS pool. Only trusted sources
        # (local cache / BRAIN /correlations/SELF) participate — `unknown`
        # cache-miss values fall back to BRAIN's verdict.
        existing_metrics = alpha.metrics or {}
        ok, failed, pending = compute_can_submit(
            detail,
            local_self_corr=existing_metrics.get("_self_corr"),
            local_self_corr_source=existing_metrics.get("_self_corr_source"),
        )
        if ok is None:
            return None

        # V-27.140: SQL in-place JSONB merge instead of read-modify-write of
        # the whole column. Two workers each doing dict(alpha.metrics) → edit
        # → reassign would have the later commit clobber the earlier one's
        # unrelated keys (IQC backfill, evaluation, …). `metrics || patch`
        # is an atomic shallow merge — only the three _brain_* keys are
        # touched, every other key survives concurrent writers.
        from sqlalchemy import cast as _sql_cast, update as _sql_update
        from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB
        from backend.models import Alpha as _Alpha

        _patch = {
            "_brain_can_submit": ok,
            "_brain_failed_checks": failed,
            "_brain_pending_checks": pending,
        }
        await self.db.execute(
            _sql_update(_Alpha)
            .where(_Alpha.id == alpha_pk)
            .values(
                can_submit=ok,
                metrics=_Alpha.metrics.op("||")(_sql_cast(_patch, _PG_JSONB)),
            )
        )
        await self.db.commit()

        return {
            "can_submit": ok,
            "failed_checks": failed,
            "pending_checks": pending,
        }

    async def submit_alpha(
        self,
        alpha_pk: int,
        brain_adapter=None,
    ) -> Dict[str, Any]:
        """Submit an alpha to BRAIN for evaluation.

        Pre-flight gates (mirror scripts/submit_alpha.py — submit is
        irreversible and burns BRAIN quota, so every gate runs before the
        POST):
          1. alpha exists and has a BRAIN alpha_id
          2. not already submitted (date_submitted IS NULL)
          3. can_submit is True
          4. self_corr precheck — refuse when locally/BRAIN-measured corr
             >= 0.7 (BRAIN would reject; submitting wastes a slot). An
             "unknown" precheck is inconclusive and does NOT block — the
             submit proceeds and BRAIN makes the final call.

        Concurrency (V-27.123): a Redis lock (submit_lock:{alpha_id},
        SET NX EX) — NOT a DB row lock — serialises concurrent submits of
        the same alpha. The loser gets a fast, non-blocking rejection
        instead of queueing on a row lock held across the BRAIN HTTP
        round-trip (which would starve the DB connection pool). Under the
        lock the date_submitted gate is re-checked, so a submit that landed
        just before the lock was acquired is still caught.

        On BRAIN success, stamps alpha.date_submitted and refreshes the
        portfolio-skeleton cache so the mining loop stops re-generating the
        just-submitted shape.

        Returns {submitted: bool, reason: str, self_corr?, self_corr_source?,
        brain?} — submitted=False with a human-readable reason on any gate
        failure or BRAIN rejection. Never raises for gate failures.
        """
        from sqlalchemy import select

        from backend.models import Alpha

        alpha = (
            await self.db.execute(select(Alpha).where(Alpha.id == alpha_pk))
        ).scalar_one_or_none()
        if not alpha:
            return {"submitted": False, "reason": "alpha not found"}
        if not alpha.alpha_id:
            return {"submitted": False, "reason": "alpha has no BRAIN alpha_id"}
        if alpha.date_submitted is not None:
            return {
                "submitted": False,
                "reason": f"already submitted at {alpha.date_submitted}",
            }
        if alpha.can_submit is not True:
            # V-27.127: can_submit=False may be a STALE verdict — a self_corr
            # demote that has since dropped below threshold should not block
            # submit forever. If the ONLY failing checks are self-correlation
            # (LOCAL_SELF_CORRELATION / SELF_CORRELATION), defer to gate-4's
            # LIVE precheck below instead of hard-blocking here. Any non-
            # self-corr FAIL still hard-blocks. can_submit is None ("no BRAIN
            # signal", not "tested & stale") is NOT overridable.
            from backend.config import settings as _cfg
            _failed = (alpha.metrics or {}).get("_brain_failed_checks") or []
            _self_corr_names = {"LOCAL_SELF_CORRELATION", "SELF_CORRELATION"}
            _only_self_corr = bool(_failed) and all(
                isinstance(f, dict) and f.get("name") in _self_corr_names
                for f in _failed
            )
            _override = (
                alpha.can_submit is False
                and _only_self_corr
                and getattr(_cfg, "SUBMIT_GATE_LIVE_SELF_CORR_OVERRIDE", True)
            )
            if not _override:
                return {
                    "submitted": False,
                    "reason": f"can_submit={alpha.can_submit} — must be True before submit",
                }
            logger.info(
                f"[submit_alpha] V-27.127 can_submit=False but only self-corr "
                f"checks failed — deferring to gate-4 live precheck for "
                f"alpha {alpha_pk}"
            )
        # V-27.139: a missing region would otherwise be precheck'd against
        # the USA OS pool, producing a meaningless corr for a non-USA alpha.
        # Refuse rather than guess.
        if not alpha.region:
            return {
                "submitted": False,
                "reason": "alpha region 缺失，无法做 self_corr precheck — 拒绝提交",
            }

        from contextlib import AsyncExitStack

        # V-27.151: AsyncExitStack so a BrainAdapter __aenter__ failure isn't
        # followed by an __aexit__ on a half-initialised adapter.
        async with AsyncExitStack() as stack:
            if brain_adapter is None:
                from backend.adapters.brain_adapter import BrainAdapter
                brain_adapter = await stack.enter_async_context(BrainAdapter())

            # V-27.123: Redis lock — concurrent submits of the same alpha get
            # a fast non-blocking rejection instead of queueing on a DB row
            # lock held across the BRAIN HTTP round-trip. Redis down →
            # degrade to best-effort (proceed without the lock).
            _lock_key = f"submit_lock:{alpha.alpha_id}"
            _redis = None
            try:
                _redis = await brain_adapter._get_slot_redis()
                _got_lock = bool(await _redis.set(
                    _lock_key, str(alpha_pk), nx=True, ex=300,
                ))
            except Exception as _lock_e:
                logger.warning(
                    f"[submit_alpha] redis lock unavailable, proceeding "
                    f"without it: {_lock_e}"
                )
                _redis = None
                _got_lock = True
            if not _got_lock:
                return {
                    "submitted": False,
                    "reason": "another submit for this alpha is already in progress",
                }

            try:
                # Re-check date_submitted under the lock — the previous lock
                # holder may have just finished and stamped it.
                await self.db.refresh(alpha)
                if alpha.date_submitted is not None:
                    return {
                        "submitted": False,
                        "reason": f"already submitted at {alpha.date_submitted}",
                    }

                from backend.services.correlation_service import (
                    CorrelationService,
                    CorrSource,
                )
                # P3-Brain plan §6.4:self_corr cache 命中跳过 BRAIN /correlations/SELF
                # 重试。key submit:self_corr_passed:{id} 二态 "1"(R4-C6:避免
                # float(corr) parse fragility — None/"nan" 时抛)。TTL 300s 允许
                # PROD-corr PENDING 重试窗口快速重提交,避免每次都浪费 self_corr 调用。
                _self_corr_skip = False
                _self_corr_cache_key = f"submit:self_corr_passed:{alpha.alpha_id}"
                if _redis is not None:
                    try:
                        _cached = await _redis.get(_self_corr_cache_key)
                        if _cached == "1":
                            _self_corr_skip = True
                            logger.info(
                                f"[submit_alpha] self_corr cache hit ({_self_corr_cache_key}), "
                                f"skipping BRAIN /correlations/SELF"
                            )
                    except Exception as _cache_e:
                        logger.debug(f"[submit_alpha] self_corr cache read failed: {_cache_e}")

                if not _self_corr_skip:
                    corr_svc = CorrelationService(brain_adapter)
                    corr, src = await corr_svc.get_with_fallback(
                        alpha.alpha_id, region=alpha.region
                    )
                    # V-27.126 followup: BRAIN_PENDING means the corr job is still
                    # computing (corr is None). Distinct from UNKNOWN ("could not
                    # measure") — here we genuinely will know soon, so refuse now
                    # and let the caller retry rather than submitting blind into a
                    # possibly-high corr that would waste the slot.
                    if src == CorrSource.BRAIN_PENDING:
                        return {
                            "submitted": False,
                            "reason": (
                                "self_corr 仍在 BRAIN 侧计算中(corr pending)— "
                                "稍后重试"
                            ),
                            "self_corr_source": src,
                            "retryable": True,
                        }
                    # src=UNKNOWN → corr is None → inconclusive, do NOT block.
                    if src != CorrSource.UNKNOWN and corr is not None and corr >= 0.7:
                        return {
                            "submitted": False,
                            "reason": (
                                f"self_corr {corr:.3f} >= 0.7 ({src}) — BRAIN would "
                                f"reject; submitting would waste a slot"
                            ),
                            "self_corr": corr,
                            "self_corr_source": src,
                        }
                    # self_corr 通过 — 写 cache 让 PENDING 重试时跳过 BRAIN 调用
                    if _redis is not None:
                        try:
                            await _redis.setex(_self_corr_cache_key, 300, "1")
                        except Exception as _cache_e:
                            logger.debug(f"[submit_alpha] self_corr cache write failed: {_cache_e}")

                # P3-Brain plan §6.2:Consultant 模式第 3 门 PROD correlation gate
                # (User 模式跳过 — endpoint 选择类能力走全局 flag,plan §14)。
                # AUTH_DENIED → 自动 revert flag(安全网,plan §6.3)。
                from backend.config import settings as _cfg
                if _cfg.ENABLE_BRAIN_CONSULTANT_MODE:
                    prod = await brain_adapter.check_correlation_with_poll(
                        alpha.alpha_id, "PROD",
                        max_polls=3, poll_interval=5.0,
                    )
                    if prod["status"] == "AUTH_DENIED":
                        await self._auto_revert_consultant_mode(
                            "BRAIN PROD-corr 返回 403 — 账号未实际授权 Consultant"
                        )
                        return {
                            "submitted": False,
                            "reason": "Consultant 模式已自动回退到 USER(BRAIN 拒绝 PROD-corr)",
                            "retryable": False,
                        }
                    if prod["status"] == "PENDING":
                        return {
                            "submitted": False,
                            "reason": "PROD-corr 计算中(BRAIN 异步)— 稍后重试",
                            "retryable": True,
                        }
                    _prod_max = prod.get("data", {}).get("max")
                    if _prod_max is None:
                        return {
                            "submitted": False,
                            "reason": "PROD-corr 无 max 字段(BRAIN 响应异常)",
                            "retryable": True,
                        }
                    if _prod_max >= 0.7:
                        return {
                            "submitted": False,
                            "reason": f"prod_corr_max={_prod_max:.3f} >= 0.7 — BRAIN would reject",
                            "prod_corr_max": _prod_max,
                        }

                result = await brain_adapter.submit_alpha(alpha.alpha_id)
                if not result.get("success"):
                    body = result.get("body")
                    msg = (
                        (body.get("message") or body.get("error") or str(body))
                        if isinstance(body, dict)
                        else str(body)
                    )
                    return {
                        "submitted": False,
                        "reason": f"BRAIN rejected (status {result.get('status_code')}): {msg}",
                        "brain": result,
                    }

                # success — stamp date_submitted. V-27.127 followup: the
                # alphas.date_submitted column is TIMESTAMP WITHOUT TIME ZONE,
                # so a tz-aware value trips asyncpg's naive/aware subtraction
                # error. Use a naive UTC timestamp (matches the column + the
                # ExperimentRun.finished_at convention elsewhere). This path
                # was previously unreachable for can_submit!=True alphas —
                # V-27.127's gate-3 override now lets them through.
                from datetime import datetime
                alpha.date_submitted = datetime.utcnow()
                await self.db.commit()

                # Post-submit: refresh portfolio-skeleton cache (DB-only,
                # ~10ms) so the T1 strategy prompt stops nudging the LLM
                # toward the shape we just submitted. Non-fatal.
                try:
                    from backend.agents.seed_pool.portfolio_skeletons import (
                        refresh_portfolio_from_db,
                    )
                    await refresh_portfolio_from_db(region=alpha.region)
                except Exception as e:
                    logger.warning(f"[submit_alpha] skeleton cache refresh failed: {e}")

                return {"submitted": True, "reason": "ok", "brain": result}
            finally:
                # V-27.123 followup: CAS release, not a blind DELETE. The
                # BRAIN submit poll (max_polls=60) can run long enough to
                # approach the 300s lock TTL — past expiry another worker
                # may hold a fresh lock under the same key, and a blind
                # delete would evict *their* lock. Lua check-and-delete
                # only removes the key if it still holds our token.
                if _redis is not None:
                    try:
                        await _redis.eval(
                            "if redis.call('get', KEYS[1]) == ARGV[1] then "
                            "return redis.call('del', KEYS[1]) else return 0 end",
                            1,
                            _lock_key,
                            str(alpha_pk),
                        )
                    except Exception:
                        pass

    async def _auto_revert_consultant_mode(self, reason: str) -> None:
        """Safety-net: BRAIN PROD-corr 返回 403 → 自动 clear ENABLE_BRAIN_CONSULTANT_MODE flag。

        用独立 AsyncSessionLocal session 调 clear_override(避免与当前 submit_alpha
        db transaction 嵌套 — FeatureFlagService.clear_override 有 @transactional 装饰
        会触发 InvalidRequestError "A transaction is already begun"。R4-M2 修复)。

        独立 commit 也保证:submit 失败 rollback 不会撤销这次 flag clear。
        失败不向上抛(safety-net 自身不能拖垮 submit 主路径)。
        """
        try:
            from backend.database import AsyncSessionLocal
            from backend.services.feature_flag_service import FeatureFlagService
            async with AsyncSessionLocal() as iso_db:
                flag_svc = FeatureFlagService(iso_db)
                await flag_svc.clear_override(
                    "ENABLE_BRAIN_CONSULTANT_MODE",
                    actor="system_auto_revert",
                    note=reason,
                )
            logger.error(f"[brain_role] Consultant 模式自动回退: {reason}")
        except Exception as ex:
            logger.error(f"[brain_role] auto-revert 自身失败 (ignored, submit 继续): {ex}")

    async def get_marginal_contribution(
        self,
        alpha_pk: int,
        competition: Optional[str] = None,
        team_id: Optional[str] = None,
        brain_adapter=None,
    ) -> Optional[Dict[str, Any]]:
        """Fetch BRAIN marginal performance (standalone vs merged into a scope).

        BRAIN endpoint:
          /{scope}/alphas/{alpha_id}/before-and-after-performance
        where scope ∈ {competitions/{competition}, teams/{team_id}, users/self}.

        Returns the raw BRAIN payload (stats.before/after, yearlyStats, pnl,
        partitionName) with an envelope adding our DB alpha_pk + the resolved
        scope for the frontend. None when alpha lacks brain alpha_id or BRAIN
        call fails — caller maps to 404 / 502.

        2026-05-26: the competition `score` RETURNED with the IQC2026S2 season
        (verified live under competitions/IQC2026S2). score.before/after is the
        team leaderboard rank score; deltas["score"] = after - before is surfaced
        for display + audit, but is DISPLAY-ONLY — it is not fed to the composite
        scorecard. Present only under a competition scope (team/users omit it).
        pnl/yearlyStats are {schema, records}-wrapped; pnl.records is still
        [date, beforePnL, afterPnL]. partitionName (e.g. "EQUITY:1") labels the
        partition.
        """
        alpha = await self.alpha_repo.get_by_id(alpha_pk)
        if not alpha or not alpha.alpha_id:
            return None

        if brain_adapter is None:
            from backend.adapters.brain_adapter import BrainAdapter
            async with BrainAdapter() as ba:
                payload = await ba.get_before_and_after_performance(
                    alpha.alpha_id,
                    competition=competition,
                    team_id=team_id,
                )
        else:
            payload = await brain_adapter.get_before_and_after_performance(
                alpha.alpha_id,
                competition=competition,
                team_id=team_id,
            )

        if not payload:
            return None

        # Compute deltas for quick UI display + the multi-dimensional analysis.
        # 2026-05-26: the competition `score` is back (IQC2026S2); its delta is
        # added below as a DISPLAY-ONLY signal. The composite scorecard still
        # scores only the standalone-vs-merged stats deltas across return AND risk
        # dimensions (see backend.marginal_analysis).
        import math as _math
        stats = payload.get("stats") or {}
        before = stats.get("before") or {}
        after = stats.get("after") or {}

        def _delta(k: str):
            b, a = before.get(k), after.get(k)
            if (isinstance(b, (int, float)) and not isinstance(b, bool) and _math.isfinite(b)
                    and isinstance(a, (int, float)) and not isinstance(a, bool) and _math.isfinite(a)):
                return round(a - b, 6)
            return None

        # Raw stats deltas (also drive the per-metric before→after cards in the UI).
        deltas = {
            "sharpe": _delta("sharpe"),
            "fitness": _delta("fitness"),
            "turnover": _delta("turnover"),
            "returns": _delta("returns"),
            "pnl": _delta("pnl"),
            "drawdown": _delta("drawdown"),
            "margin": _delta("margin"),
        }

        # Derived dimension for the analysis: the recent-year marginal-sharpe
        # trend (robustness / decay flag — drives the guardrail). pnl_norm and
        # Δmargin were dropped (collinear with returns / turnover); the alpha's
        # OWN absolute margin drives the economic gate instead (5bps / negative).
        from backend.marginal_analysis import (
            analyze_marginal_contribution,
            recent_yearly_sharpe_delta,
        )
        analysis_deltas = {
            **deltas,
            "recent_yearly_sharpe": recent_yearly_sharpe_delta(payload.get("yearlyStats")),
        }
        _alpha_margin = alpha.is_margin
        if _alpha_margin is None and isinstance(alpha.is_metrics, dict):
            _alpha_margin = alpha.is_metrics.get("margin")
        analysis = analyze_marginal_contribution(
            analysis_deltas, merged=after, baseline=before,
            alpha_margin=_alpha_margin, region=alpha.region,
        )

        # Competition `score` delta — lives at the payload TOP level (not stats).
        # score.before/after is the team leaderboard rank score, restored with the
        # IQC2026S2 season (2026-05-26); present only under a competition scope.
        # Surfaced in `deltas` for display + audit persistence, but DELIBERATELY
        # added AFTER analyze_marginal_contribution so it can never enter the
        # composite scorecard (display-only signal per 2026-05-26 decision).
        _score = payload.get("score") or {}
        _sb, _sa = _score.get("before"), _score.get("after")
        if (isinstance(_sb, (int, float)) and not isinstance(_sb, bool) and _math.isfinite(_sb)
                and isinstance(_sa, (int, float)) and not isinstance(_sa, bool) and _math.isfinite(_sa)):
            deltas["score"] = round(_sa - _sb, 6)

        return {
            "alpha_pk": alpha_pk,
            "alpha_brain_id": alpha.alpha_id,
            "scope": (
                f"competitions/{competition}" if competition
                else (f"teams/{team_id}" if team_id else "users/self")
            ),
            "partition_name": payload.get("partitionName"),
            "raw": payload,
            "deltas": deltas,
            "analysis": analysis,
        }

    async def get_alpha_by_brain_id(self, brain_alpha_id: str) -> Optional[AlphaDetail]:
        """
        Get alpha by BRAIN platform ID.

        Args:
            brain_alpha_id: BRAIN alpha ID string

        Returns:
            AlphaDetail or None if not found
        """
        alpha = await self.alpha_repo.get_by_alpha_id(brain_alpha_id)
        if not alpha:
            return None
        
        return self._to_detail(alpha)
    
    def _to_detail(self, alpha: Alpha) -> AlphaDetail:
        """Convert Alpha model to AlphaDetail."""
        return AlphaDetail(
            id=alpha.id,
            alpha_id=alpha.alpha_id,
            task_id=alpha.task_id,
            expression=alpha.expression,
            hypothesis=alpha.hypothesis,
            logic_explanation=alpha.logic_explanation,
            region=alpha.region,
            universe=alpha.universe,
            dataset_id=alpha.dataset_id,
            fields_used=alpha.fields_used or [],
            operators_used=alpha.operators_used or [],
            status=alpha.status or "created",
            quality_status=alpha.quality_status or "PENDING",
            human_feedback=alpha.human_feedback or "NONE",
            feedback_comment=alpha.feedback_comment,
            metrics=alpha.metrics or {},
            is_metrics=alpha.is_metrics or {},
            os_metrics=alpha.os_metrics or {},
            created_at=alpha.created_at,
            date_submitted=alpha.date_submitted,
            can_submit=alpha.can_submit,
        )
    
    # =========================================================================
    # Trace Operations
    # =========================================================================
    
    async def get_alpha_trace(self, alpha_id: int) -> Optional[Dict[str, Any]]:
        """
        Get the trace steps that generated an alpha.
        
        Args:
            alpha_id: Database ID of the alpha
            
        Returns:
            Dict with trace context or None if not found
        """
        alpha = await self.alpha_repo.get_by_id(alpha_id)
        if not alpha:
            return None
        
        if not alpha.trace_step_id:
            return {"message": "No trace step linked to this alpha"}
        
        # Get the trace step
        step_query = select(TraceStep).where(TraceStep.id == alpha.trace_step_id)
        step_result = await self.db.execute(step_query)
        step = step_result.scalar_one_or_none()
        
        if not step:
            return {"message": "Trace step not found"}
        
        # Get all related trace steps for context
        context_query = (
            select(TraceStep)
            .where(TraceStep.task_id == step.task_id)
            .where(TraceStep.step_order <= step.step_order)
            .order_by(TraceStep.step_order)
        )
        
        context_result = await self.db.execute(context_query)
        context_steps = context_result.scalars().all()
        
        return {
            "alpha_id": alpha_id,
            "trace_step_id": step.id,
            "task_id": step.task_id,
            "context": [
                {
                    "step_type": s.step_type,
                    "step_order": s.step_order,
                    "status": s.status,
                    "input": s.input_data,
                    "output": s.output_data,
                    "duration_ms": s.duration_ms,
                }
                for s in context_steps
            ],
        }
    
    # =========================================================================
    # Feedback Operations
    # =========================================================================
    
    async def submit_feedback(
        self,
        alpha_id: int,
        rating: str,
        comment: Optional[str] = None,
    ) -> bool:
        """
        Submit human feedback for an alpha.
        
        Args:
            alpha_id: Database ID of the alpha
            rating: 'LIKED' or 'DISLIKED'
            comment: Optional feedback comment
            
        Returns:
            True if feedback was submitted, False if alpha not found
        """
        if rating not in ["LIKED", "DISLIKED"]:
            raise ValueError("Rating must be LIKED or DISLIKED")
        
        # Check if alpha exists
        alpha = await self.alpha_repo.get_by_id(alpha_id)
        if not alpha:
            return False
        
        # Update feedback
        await self.db.execute(
            update(Alpha)
            .where(Alpha.id == alpha_id)
            .values(human_feedback=rating, feedback_comment=comment)
        )
        await self.commit()

        # W3: dispatch Voyager-style skill promotion to Celery worker.
        # Branch logic per plan R3 #1 modification:
        #   LIKED + PASS              → promote SUCCESS_PATTERN, +0.2 confidence
        #   LIKED + PASS_PROVISIONAL  → promote (lower confidence)
        #   LIKED + OPTIMIZE          → "user prefers this direction" hint
        #   LIKED + FAIL              → record only as direction signal
        #   DISLIKED                  → -0.15 confidence on existing pattern
        try:
            from backend.tasks import learn_from_alpha
            user_feedback_payload = {
                "rating": rating,
                "comment": comment,
                "quality_status": alpha.quality_status,
            }
            learn_from_alpha.delay(alpha_id, user_feedback=user_feedback_payload)
            logger.info(f"[AlphaService] HITL feedback dispatched: alpha={alpha_id} rating={rating}")
        except Exception as e:
            # Non-fatal: feedback row is already saved, learning is best-effort
            logger.warning(f"[AlphaService] failed to dispatch learn_from_alpha for {alpha_id}: {e}")

        return True
    
    # =========================================================================
    # Statistics
    # =========================================================================
    
    async def get_task_stats(self, task_id: int) -> Dict[str, Any]:
        """
        Get statistics for alphas in a task.
        
        Args:
            task_id: The task ID
            
        Returns:
            Statistics dict
        """
        return await self.alpha_repo.get_task_stats(task_id)
    
    async def get_region_distribution(self, task_id: Optional[int] = None) -> Dict[str, int]:
        """
        Get distribution of alphas by region.

        Args:
            task_id: Optional task filter

        Returns:
            Dict of region -> count
        """
        return await self.alpha_repo.get_region_distribution(task_id)

    # =========================================================================
    # Tier System — quality_status transition audit (PR2)
    # =========================================================================

    async def apply_quality_status_change(
        self,
        alpha_id: int,
        new_status: str,
        reason: str,
        source: str,
    ) -> bool:
        """Single-point writer for alpha.quality_status changes.

        Atomically updates alphas.quality_status and inserts an
        alpha_status_transitions row capturing the change. Wrapping both in
        one transaction guarantees the audit log can never miss a transition.

        Args:
            alpha_id: Database ID (NOT BRAIN alpha_id) of the alpha.
            new_status: Target QualityStatus value (PASS / PASS_PROVISIONAL /
                FAIL / OPTIMIZE / REJECT / PENDING).
            reason: Free-text human-readable explanation. Examples:
                "user manual review", "metrics drifted below threshold".
            source: Controlled enum identifying the code path that triggered
                the change. One of: "node_evaluate" / "daily_beat_kb" /
                "daily_beat_os" / "backfill" / "manual_api".

        Returns:
            True if a transition row was written, False if no-op (status
            unchanged or alpha not found).

        The session commit is the caller's responsibility — this method only
        flushes so the transition is visible within the same transaction.
        """
        alpha = await self.alpha_repo.get_by_id(alpha_id)
        if alpha is None:
            logger.warning(
                f"[AlphaService] apply_quality_status_change: alpha_id={alpha_id} not found"
            )
            return False
        if alpha.quality_status == new_status:
            return False  # no-op, don't pollute audit log

        transition = AlphaStatusTransition(
            alpha_id=alpha_id,
            old_status=alpha.quality_status,
            new_status=new_status,
            sharpe_at_transition=alpha.is_sharpe,
            reason=reason,
            source=source,
        )
        self.db.add(transition)
        alpha.quality_status = new_status
        await self.db.flush()
        logger.info(
            f"[AlphaService] alpha_id={alpha_id} {transition.old_status} -> {new_status} "
            f"(source={source}, reason={reason!r})"
        )
        return True

    async def upsert_alpha_pnl(self, alpha_db_id: int, pnl_series) -> int:
        """Full-refresh persist of an alpha's daily PnL into alpha_pnl (2026-05-24).

        ``pnl_series``: a date-indexed CUMULATIVE PnL Series, exactly as
        CorrelationService._fetch_pnl_series returns (BRAIN's recordset `pnl`
        column is cumulative). Stored per trade_date:
          - cumulative_pnl = the BRAIN cumulative value
          - pnl            = daily diff (NaN/first-day → NULL)

        Delete-then-insert: BRAIN returns the full backtest series on every
        fetch, so a non-empty fetch is the complete authoritative series and a
        full refresh is correct (no growth window to lose).

        EMPTY GUARD: an empty / failed fetch is a NO-OP — it never deletes
        existing rows, so a transient BRAIN rate-limit soft-fail cannot wipe
        stored PnL. The caller owns the commit (this only flushes). Returns the
        number of rows written (0 on empty).
        """
        if pnl_series is None or len(pnl_series) == 0:
            return 0
        import pandas as pd
        from sqlalchemy import delete as _sql_delete

        from backend.models import AlphaPnl

        daily = pnl_series.diff()
        await self.db.execute(
            _sql_delete(AlphaPnl).where(AlphaPnl.alpha_id == alpha_db_id)
        )
        rows = 0
        for ts, cum in pnl_series.items():
            if cum is None or pd.isna(cum):
                continue
            d = daily.get(ts)
            self.db.add(AlphaPnl(
                alpha_id=alpha_db_id,
                trade_date=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                pnl=(None if d is None or pd.isna(d) else float(d)),
                cumulative_pnl=float(cum),
            ))
            rows += 1
        await self.db.flush()
        return rows

    async def get_alpha_pnl_series(self, alpha_db_id: int) -> List[Dict[str, Any]]:
        """Ordered daily PnL series for an alpha from the alpha_pnl table.

        Returns a chronological list of {trade_date, pnl, cumulative_pnl};
        empty list when nothing has been persisted yet (mining/sync populate it).
        """
        from backend.models import AlphaPnl

        q = (
            select(AlphaPnl.trade_date, AlphaPnl.pnl, AlphaPnl.cumulative_pnl)
            .where(AlphaPnl.alpha_id == alpha_db_id)
            .order_by(AlphaPnl.trade_date.asc())
        )
        rows = (await self.db.execute(q)).all()
        return [
            {"trade_date": r[0], "pnl": r[1], "cumulative_pnl": r[2]}
            for r in rows
        ]

    # =========================================================================
    # Blueprint optimization (manual, 2026-06-03)
    # =========================================================================

    async def prepare_blueprint_optimization(
        self, alpha_id: int, *, budget_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Validate + preview a manual "optimize-from-blueprint" for one alpha.

        The HTTP layer (``POST /alphas/{id}/optimize``) calls this to decide
        whether to dispatch the Celery cycle. It is a pure read/validate — it
        does NOT enqueue Celery (the router owns ``.delay()``, mirroring
        ``sync_alphas``).

        Returns on success::

            {"ok": True, "budget": int, "n_variants": int,
             "variant_tags": [...], "message": str}

        or on rejection::

            {"ok": False, "code": "not_found"|"no_expression"|"in_flight",
             "message": str, ...}

        The actual cycle reuses the Stage A pipeline with
        ``trigger_source="manual"`` and runs INDEPENDENTLY of
        ``ENABLE_OPTIMIZATION_LOOP`` (the 6h-beat kill switch).
        """
        from backend.config import settings as _cfg
        from backend.models import OptimizationRun

        alpha = await self.get_by_id(Alpha, alpha_id)
        if alpha is None:
            return {
                "ok": False, "code": "not_found",
                "message": f"Alpha #{alpha_id} 不存在",
            }
        if not (alpha.expression or "").strip():
            return {
                "ok": False, "code": "no_expression",
                "message": f"Alpha #{alpha_id} 无表达式，无法优化",
            }

        # Concurrency guard (UX fast-path; the Celery task also holds a Redis
        # NX lock for the race-proof guarantee). Block only a RECENT still-open
        # cycle so a crashed-worker orphan row can't wedge re-triggers forever.
        # cycle_started_at is DB-clock naive-UTC (server_default func.now();
        # the PG session runs UTC per the repo convention), so comparing it to
        # datetime.utcnow() is consistent. Failure direction is safe: a clock
        # skew would only OVER-block (false 409), never let a live cycle slip.
        guard_minutes = int(getattr(_cfg, "OPT_MANUAL_INFLIGHT_MINUTES", 40))
        cutoff = datetime.utcnow() - timedelta(minutes=guard_minutes)
        inflight = (await self.db.execute(
            select(OptimizationRun.id)
            .where(
                OptimizationRun.parent_alpha_id == alpha_id,
                OptimizationRun.cycle_finished_at.is_(None),
                OptimizationRun.error.is_(None),
                OptimizationRun.cycle_started_at > cutoff,
            )
            .order_by(OptimizationRun.id.desc())
            .limit(1)
        )).scalar_one_or_none()
        if inflight is not None:
            return {
                "ok": False, "code": "in_flight", "opt_run_id": int(inflight),
                "message": (
                    f"Alpha #{alpha_id} 已有进行中的优化周期 (#{inflight})，"
                    f"请等待完成后再试"
                ),
            }

        # Resolve budget: clamp caller override into [1, MAX]; default covers
        # the full ~10-variant grid.
        default_budget = int(getattr(_cfg, "OPT_MANUAL_SIM_BUDGET", 16))
        max_budget = int(getattr(_cfg, "OPT_MANUAL_SIM_BUDGET_MAX", 30))
        budget = default_budget if budget_override is None else int(budget_override)
        budget = max(1, min(budget, max_budget))

        # Variant preview — call the Stage A generator on the (still-uncommitted)
        # ORM row. Pure / in-memory; no BRAIN/DB. Guarded so a generator hiccup
        # degrades to a generic count rather than failing the whole request.
        variant_tags: List[str] = []
        try:
            from backend.services.optimization.generators.settings_sweep import (
                SettingsSweepGenerator,
            )
            variants = await SettingsSweepGenerator().generate(alpha)
            variant_tags = [v.tag for v in variants]
        except Exception as ex:  # noqa: BLE001
            logger.warning(
                "blueprint-opt preview generate failed for alpha %s: %s",
                alpha_id, ex,
            )
        n_variants = len(variant_tags)

        return {
            "ok": True,
            "budget": budget,
            "n_variants": n_variants,
            "variant_tags": variant_tags,
            "message": (
                f"将对 Alpha #{alpha_id} 做 {n_variants or '最多 10'} 个设置变体"
                f"扫描（decay/窗口/中性化），消耗最多 {budget} 次 BRAIN 模拟；"
                f"胜出变体进入提交积压队列（不自动提交）。"
            ),
        }
