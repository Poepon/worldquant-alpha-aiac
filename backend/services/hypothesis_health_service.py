"""Hypothesis Health Check Service (P1-C, second half).

来源: docs/alphagbm_skills_research_2026-05-15.md skill `investment-thesis`.

Daily Celery beat at 08:30 Asia/Shanghai (after 08:00 alpha-health-check)
evaluates 5 structured triggers over ACTIVE/PROMOTED hypotheses and, on
a fired trigger, calls the LLM to grade the thesis 0-100 and emit an
``ai_feedback`` + ``recommended_action``. The result is persisted onto
the hypothesis row (``is_triggered``, ``trigger_detail``, ``thesis_score``,
``ai_feedback``) plus an audit row in ``hypothesis_status_transitions`` on
the False → True edge.

Five triggers:

  T1 dropped_sharpe              — current AVG vs ``baseline_metrics`` AVG
  T2 no_pass_in_n_rounds         — N consecutive tested rounds with 0 PASS
  T3 pass_rate_drop              — recent vs early pass-rate slope
  T4 attribution_hypothesis_dominant — hypothesis-attribution share >= cfg
  T5 stale_alphas                — most alphas haven't been refreshed lately

All triggers are SOFT — they do NOT stop sampling (SFX-16 invariant).
Sampling stop requires the separate ``is_active=False`` regime-freeze.

Key invariants (per plan):
- MFX-1: T1 baseline_metrics and current both use AVG with n_alphas>=3.
- MFX-2: "tested_rounds" = alpha_count + flip_alpha_count > 0
  (flip-PASS still counts as testing the hypothesis but flip products
  are tracked separately by V-27.71).
- MFX-5: trigger_detail concat uses PG ``||`` JSONB op when bound to
  Postgres, Python merge otherwise (sqlite test fallback).
- MFX-6: recommended_action normalised via Pydantic field_validator;
  schema-invalid LLM responses fall back with status
  ``fallback_schema_invalid`` (distinct from call-failed).
- SFX-9: aggregates use real-time JOIN, never read
  ``Hypothesis.sharpe_avg`` (refresh_stats can be stale).
- SFX-10: ``hypothesis_status_transitions`` row written ONLY on the
  False → True ``is_triggered`` edge.
- SFX-13: ``last_thesis_score_status.startswith('fallback')`` selects
  the 4h backoff path vs the 24h gate for successful runs.
"""
from __future__ import annotations

import asyncio
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Literal, Optional

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import case, func, select

from backend.config import settings
from backend.services.alpha_health_service import (
    SH_TZ,
    _iso,
    _safe_num,
    _to_utc_aware,
)


# ---------------------------------------------------------------------------
# Trigger primitives (pure functions — no DB)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tested_rounds(stats) -> bool:
    """MFX-2: a round is "tested" when alpha_count + flip_alpha_count > 0.

    Both real attempts AND flip-retry products count as having exercised
    the hypothesis. Only retryable transient-BRAIN-failure attempts
    (V-27.61) are excluded (they're tracked separately in
    ``retryable_count`` and don't indicate the hypothesis was actually
    tested).
    """
    return (
        int(getattr(stats, "alpha_count", 0) or 0)
        + int(getattr(stats, "flip_alpha_count", 0) or 0)
    ) > 0


@dataclass
class TriggerConfig:
    """Tunables for the five triggers — all loaded from ``settings``
    so monkeypatching for tests works (the canonical import path is
    ``from backend.config import settings`` at module-top)."""

    dropped_sharpe_orange_pct: float
    dropped_sharpe_red_pct: float
    nopass_n_rounds: int
    passrate_drop_pct: float
    passrate_window: int
    attr_window: int
    attr_share: float
    stale_share: float

    @classmethod
    def from_settings(cls, s) -> "TriggerConfig":
        return cls(
            dropped_sharpe_orange_pct=s.TRIGGER_DROPPED_SHARPE_PCT,
            dropped_sharpe_red_pct=s.TRIGGER_DROPPED_SHARPE_RED_PCT,
            nopass_n_rounds=s.TRIGGER_NOPASS_N_ROUNDS,
            passrate_drop_pct=s.TRIGGER_PASS_RATE_DROP_PCT,
            passrate_window=s.TRIGGER_PASS_RATE_WINDOW,
            attr_window=s.TRIGGER_ATTR_HYPOTHESIS_WINDOW,
            attr_share=s.TRIGGER_ATTR_HYPOTHESIS_SHARE,
            stale_share=s.TRIGGER_STALE_SHARE,
        )


@dataclass
class TriggerHit:
    """One trigger firing — JSONB-serialised via dataclasses.asdict()
    when persisted to ``Hypothesis.trigger_detail``."""

    type: str
    threshold: float
    observed: float
    window_rounds: Optional[int]
    severity: str       # "yellow" | "orange" | "red"
    reason: str
    hit_at: str         # iso8601 utc


@dataclass
class HypothesisAggregates:
    """Real-time JOIN aggregates for one hypothesis (SFX-9 — never read
    ``Hypothesis.sharpe_avg``, which is the cached denormalized value
    that ``refresh_stats`` may have left stale)."""

    hypothesis_id: int
    related_alpha_count: int                  # PASS + PASS_PROVISIONAL in scope
    current_sharpe_avg: Optional[float]
    current_pass_rate: Optional[float]
    stale_share: Optional[float]              # fraction of in-scope alphas with stale snapshot
    recent_rounds: list                       # HypothesisRoundStats ascending
    baseline_metrics: Optional[Dict[str, Any]]


# ---------------------------------------------------------------------------
# T1 — dropped_sharpe
# ---------------------------------------------------------------------------


def evaluate_dropped_sharpe(
    aggs: HypothesisAggregates, cfg: TriggerConfig,
) -> Optional[TriggerHit]:
    """Compare current AVG(is_sharpe over PASS+PROV alphas) vs the
    ``baseline_metrics.sharpe_avg`` frozen at first PROMOTED stamp time.

    MFX-1 symmetry: both sides are AVG over the same scope; n_seed (the
    PASS count at stamp time) must be >= 3 to avoid small-sample false
    positives.
    """
    if aggs.baseline_metrics is None:
        return None
    n_seed = int((aggs.baseline_metrics or {}).get("n_alphas", 0) or 0)
    if n_seed < 3:
        return None
    base = _safe_num((aggs.baseline_metrics or {}).get("sharpe_avg"))
    cur = _safe_num(aggs.current_sharpe_avg)
    if base is None or cur is None:
        return None
    if abs(base) < 1e-9:
        return None  # baseline ~0 — delta_pct would explode
    delta_pct = (cur - base) / abs(base) * 100.0
    if delta_pct > cfg.dropped_sharpe_orange_pct:
        return None  # not severe enough (threshold is negative)
    sev = "red" if delta_pct <= cfg.dropped_sharpe_red_pct else "orange"
    return TriggerHit(
        type="dropped_sharpe_pct",
        threshold=cfg.dropped_sharpe_orange_pct,
        observed=round(delta_pct, 1),
        window_rounds=None,
        severity=sev,
        reason=f"sharpe_down_{abs(int(delta_pct))}pct_vs_baseline",
        hit_at=_now_iso(),
    )


# ---------------------------------------------------------------------------
# T2 — no_pass_in_n_rounds
# ---------------------------------------------------------------------------


def evaluate_no_pass_in_n_rounds(
    aggs: HypothesisAggregates, cfg: TriggerConfig,
) -> Optional[TriggerHit]:
    """Fires when the last N rounds all have ``pass_count == 0`` AND
    each round was actually tested (alpha_count + flip_alpha_count > 0).

    The "actually tested" guard (V-27.68) is critical: a hypothesis that
    received 0 alphas because the orchestrator skipped it is NOT the same
    as one that received alphas and produced no PASSes.
    """
    N = cfg.nopass_n_rounds
    rounds = aggs.recent_rounds[-N:]
    if len(rounds) < N:
        return None
    if not all(_tested_rounds(r) for r in rounds):
        return None
    if any((int(getattr(r, "pass_count", 0) or 0)) > 0 for r in rounds):
        return None
    return TriggerHit(
        type="no_pass_in_n_rounds",
        threshold=float(N),
        observed=0.0,
        window_rounds=N,
        severity="orange",
        reason=f"no_pass_in_{N}_consecutive_tested_rounds",
        hit_at=_now_iso(),
    )


# ---------------------------------------------------------------------------
# T3 — pass_rate_drop
# ---------------------------------------------------------------------------


def evaluate_pass_rate_drop(
    aggs: HypothesisAggregates, cfg: TriggerConfig,
) -> Optional[TriggerHit]:
    """Compare early-window pass-rate vs recent-window pass-rate.

    Pass-rate uses MFX-2 tested-rounds denominator (alpha_count +
    flip_alpha_count, NOT pass_count + flip_pass_count — V-27.71:
    flip-PASS is implementation rescue, not a hypothesis-level signal).
    """
    W = cfg.passrate_window
    if len(aggs.recent_rounds) < 2 * W:
        return None
    early = aggs.recent_rounds[:W]
    recent = aggs.recent_rounds[-W:]

    def _rate(rs):
        denom = sum(
            int(getattr(r, "alpha_count", 0) or 0)
            + int(getattr(r, "flip_alpha_count", 0) or 0)
            for r in rs
        )
        if denom == 0:
            return None
        # MFX-2 numerator: pass_count only (flip_pass excluded — see V-27.71)
        num = sum(int(getattr(r, "pass_count", 0) or 0) for r in rs)
        return num / denom

    e_rate = _rate(early)
    c_rate = _rate(recent)
    if e_rate is None or c_rate is None:
        return None
    if e_rate < 1e-6:
        return None  # nothing to compare against
    delta_pct = (c_rate - e_rate) / e_rate * 100.0
    if delta_pct > cfg.passrate_drop_pct:
        return None
    return TriggerHit(
        type="pass_rate_drop",
        threshold=cfg.passrate_drop_pct,
        observed=round(delta_pct, 1),
        window_rounds=W,
        severity="orange",
        reason=f"pass_rate_dropped_{abs(int(delta_pct))}pct",
        hit_at=_now_iso(),
    )


# ---------------------------------------------------------------------------
# T4 — attribution_hypothesis_dominant
# ---------------------------------------------------------------------------


def evaluate_attribution_hypothesis_dominant(
    aggs: HypothesisAggregates, cfg: TriggerConfig,
) -> Optional[TriggerHit]:
    """When the last W rounds with non-NULL attribution have hypothesis-
    attribution share >= cfg.attr_share, fire."""
    W = cfg.attr_window
    rounds = aggs.recent_rounds[-W:]
    if len(rounds) < W:
        return None
    typed = [r for r in rounds if getattr(r, "attribution", None)]
    if not typed:
        return None
    share = sum(
        1 for r in typed if getattr(r, "attribution", "") == "hypothesis"
    ) / len(typed)
    if share < cfg.attr_share:
        return None
    return TriggerHit(
        type="attribution_hypothesis_dominant",
        threshold=cfg.attr_share,
        observed=round(share, 2),
        window_rounds=W,
        severity="orange",
        reason=f"hypothesis_attr_{int(share * 100)}pct_in_{W}rounds",
        hit_at=_now_iso(),
    )


# ---------------------------------------------------------------------------
# T5 — stale_alphas
# ---------------------------------------------------------------------------


def evaluate_stale_alphas(
    aggs: HypothesisAggregates, cfg: TriggerConfig,
) -> Optional[TriggerHit]:
    """When fraction of in-scope alphas with stale snapshot exceeds the
    threshold, fire. Yellow severity because staleness is a data-pipeline
    concern, not (yet) a thesis concern."""
    if aggs.related_alpha_count == 0:
        return None
    if aggs.stale_share is None:
        return None
    if aggs.stale_share < cfg.stale_share:
        return None
    return TriggerHit(
        type="stale_alphas",
        threshold=cfg.stale_share,
        observed=round(aggs.stale_share, 2),
        window_rounds=None,
        severity="yellow",
        reason=f"stale_share_{int(aggs.stale_share * 100)}pct",
        hit_at=_now_iso(),
    )


# ---------------------------------------------------------------------------
# LLM scoring schema
# ---------------------------------------------------------------------------


_ACTION_LITERALS = ("continue", "monitor", "pivot", "abandon")


class LLMThesisScore(BaseModel):
    """Validated LLM response. MFX-6: ``recommended_action`` is normalised
    via a ``field_validator(mode='before')`` so common LLM whims like
    "Continue", "continue.", " ABANDON " all collapse to the canonical
    lowercase value — anything else raises ValidationError so the caller
    classifies the result as ``fallback_schema_invalid``."""

    model_config = ConfigDict(extra="ignore")

    thesis_score: int = Field(ge=0, le=100)
    ai_feedback: str
    recommended_action: str
    reasons: List[str]

    @field_validator("recommended_action", mode="before")
    @classmethod
    def _normalize_action(cls, v):
        if not isinstance(v, str):
            raise ValueError(f"recommended_action must be str, got {type(v)!r}")
        normalized = v.strip().rstrip(".").strip().lower()
        if normalized in _ACTION_LITERALS:
            return normalized
        raise ValueError(
            f"invalid recommended_action {v!r}; "
            f"expected one of {_ACTION_LITERALS}"
        )

    @field_validator("ai_feedback")
    @classmethod
    def _cap_feedback(cls, v):
        return (v or "")[:600]

    @field_validator("reasons")
    @classmethod
    def _cap_reasons(cls, v):
        if not v:
            return ["(no reasons)"]
        return [str(r)[:120] for r in v[:5]]


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------


def _load_system_prompt() -> str:
    """Hot-loaded from prompts.yaml — `hypothesis_scoring.system`."""
    from backend.agents.prompts.loader import get_prompt
    return get_prompt(
        "hypothesis_scoring",
        "system",
        default=(
            "Score the alpha-mining hypothesis 0-100 based on triggers and "
            "metrics. Output JSON: {thesis_score, ai_feedback, "
            "recommended_action, reasons}."
        ),
    )


def _build_user_prompt(h, aggs: HypothesisAggregates, hits: List[TriggerHit]) -> str:
    """Compact JSON payload — caps applied per SFX-12 (max rounds, max hits)."""
    max_rounds = settings.THESIS_SCORING_MAX_ROUNDS
    max_hits = settings.THESIS_SCORING_MAX_TRIGGER_HITS
    payload = {
        "hypothesis_id": h.id,
        "statement": (h.statement or "")[:600],
        "rationale": (h.rationale or "")[:400] if h.rationale else None,
        "region": h.region,
        "kind": h.kind,
        "status": h.status,
        "expected_signal": h.expected_signal,
        "confidence": h.confidence,
        "novelty": h.novelty,
        "metrics": {
            "current_sharpe_avg": aggs.current_sharpe_avg,
            "current_pass_rate": aggs.current_pass_rate,
            "related_alpha_count": aggs.related_alpha_count,
            "stale_share": aggs.stale_share,
        },
        "baseline_metrics": aggs.baseline_metrics,
        "recent_rounds": [
            {
                "round_index": getattr(r, "round_index", None),
                "alpha_count": getattr(r, "alpha_count", 0),
                "flip_alpha_count": getattr(r, "flip_alpha_count", 0),
                "pass_count": getattr(r, "pass_count", 0),
                "attribution": getattr(r, "attribution", None),
                "best_sharpe": getattr(r, "best_sharpe", None),
            }
            for r in aggs.recent_rounds[-max_rounds:]
        ],
        "triggers": [asdict(hit) for hit in hits[-max_hits:]],
    }
    return json.dumps(payload, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class HypothesisHealthService:
    """Orchestrates the daily hypothesis-health audit.

    Construction order (mirrors AlphaHealthService — see plan §SF6 for
    why category_resolver must be built FIRST and passed to
    BaselineProvider via its constructor):

        resolver = await AlphaHealthService.build_category_resolver(db)
        bp = BaselineProvider(category_resolver=resolver)
        llm = LLMService()
        svc = HypothesisHealthService(db, baseline_provider=bp,
                                      llm_service=llm)
        payload = await svc.run_full_check()

    Pure read path except for: ``Hypothesis.is_triggered`` / ``trigger_detail``
    / ``triggered_at`` / ``thesis_score`` / ``ai_feedback`` /
    ``thesis_score_history`` / ``last_thesis_score_at`` /
    ``last_thesis_score_status``, plus audit rows in
    ``hypothesis_status_transitions`` on False → True edges only.
    """

    SCOPE_STATUS = ("ACTIVE", "PROMOTED")
    ALPHA_SCOPE = ("PASS", "PASS_PROVISIONAL")

    def __init__(
        self,
        db,
        *,
        baseline_provider=None,
        llm_service=None,
    ):
        self.db = db
        self.bp = baseline_provider
        self.llm = llm_service
        self.cfg = TriggerConfig.from_settings(settings)
        self._token_used = 0

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------

    async def run_full_check(
        self, *, now_utc: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Run audit on all ACTIVE+PROMOTED hypotheses and return payload.

        ``now_utc`` is an injection point for tests so they don't need
        freezegun. Defaults to ``datetime.now(timezone.utc)``.
        """
        now_utc = now_utc or datetime.now(timezone.utc)
        hyps = await self._load_hypotheses_in_scope()
        aggs_map = await self._build_aggregates(hyps, now_utc)
        records: List[Dict[str, Any]] = []
        # P1-B fear-score fallback parity: one bad hypothesis must NOT crash
        # the daily batch (mirrors graded-score / dual-run / per-alpha try
        # patterns). Failures are counted and reported in the payload.
        self._failed = 0
        for h in hyps:
            aggs = aggs_map[h.id]
            try:
                hits = self._evaluate_all_triggers(aggs)
                llm_score, llm_status = None, None
                if hits and await self._can_call_llm(h, now_utc):
                    llm_score, llm_status = await self._score_with_llm_or_fallback(
                        h, aggs, hits,
                    )
                await self._persist_result(
                    h, aggs, hits, llm_score, llm_status, now_utc,
                )
                records.append(
                    self._record_row(h, aggs, hits, llm_score, llm_status)
                )
            except Exception as exc:
                self._failed += 1
                logger.warning(
                    f"[hyp_health] per-hypothesis eval crashed hid={h.id} "
                    f"({type(exc).__name__}: {exc}); skipping"
                )
                # roll back any partial writes from this hypothesis so the
                # next iteration starts clean (commit happens inside
                # _persist_result, so a partial commit can't leak — but a
                # mid-flight UPDATE before the commit can).
                try:
                    await self.db.rollback()
                except Exception:
                    pass
        return self._build_payload(records, now_utc)

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _load_hypotheses_in_scope(self):
        from backend.models import Hypothesis

        stmt = (
            select(Hypothesis)
            .where(Hypothesis.status.in_(self.SCOPE_STATUS))
            .order_by(Hypothesis.id.asc())
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def _build_aggregates(
        self, hyps, now_utc: datetime,
    ) -> Dict[int, HypothesisAggregates]:
        """SFX-9 + NTH-18: one query per aggregate, all IN-filtered by the
        hypothesis_id list — no N+1 over hypotheses.
        """
        from backend.models import Alpha, HypothesisRoundStats

        if not hyps:
            return {}
        hids = [h.id for h in hyps]

        # --- in-scope alpha aggregates ---
        agg_stmt = (
            select(
                Alpha.hypothesis_id,
                func.count(Alpha.id).label("cnt"),
                func.avg(Alpha.is_sharpe).label("sharpe_avg"),
                func.sum(
                    case(
                        (Alpha.quality_status == "PASS", 1),
                        else_=0,
                    )
                ).label("pass_count"),
            )
            .where(
                Alpha.hypothesis_id.in_(hids),
                Alpha.quality_status.in_(self.ALPHA_SCOPE),
            )
            .group_by(Alpha.hypothesis_id)
        )
        agg_rows = (await self.db.execute(agg_stmt)).all()
        cnt_map: Dict[int, int] = {}
        sharpe_map: Dict[int, Optional[float]] = {}
        passcnt_map: Dict[int, int] = {}
        for r in agg_rows:
            cnt_map[r.hypothesis_id] = int(r.cnt or 0)
            sharpe_map[r.hypothesis_id] = (
                float(r.sharpe_avg) if r.sharpe_avg is not None else None
            )
            passcnt_map[r.hypothesis_id] = int(r.pass_count or 0)

        # --- stale-share counts (alphas where metrics_snapshot_at age >=
        # STALE_RED_DAYS, or NULL).
        stale_threshold = settings.STALE_RED_DAYS
        cutoff = now_utc - timedelta(days=stale_threshold)
        stale_stmt = (
            select(
                Alpha.hypothesis_id,
                func.count(Alpha.id).label("stale_cnt"),
            )
            .where(
                Alpha.hypothesis_id.in_(hids),
                Alpha.quality_status.in_(self.ALPHA_SCOPE),
            )
            .where(
                (Alpha.metrics_snapshot_at.is_(None))
                | (Alpha.metrics_snapshot_at < cutoff)
            )
            .group_by(Alpha.hypothesis_id)
        )
        stale_rows = (await self.db.execute(stale_stmt)).all()
        stale_map = {r.hypothesis_id: int(r.stale_cnt or 0) for r in stale_rows}

        # --- recent round stats ---
        rounds_stmt = (
            select(HypothesisRoundStats)
            .where(HypothesisRoundStats.hypothesis_id.in_(hids))
            .order_by(
                HypothesisRoundStats.hypothesis_id.asc(),
                HypothesisRoundStats.round_index.asc(),
                HypothesisRoundStats.id.asc(),
            )
        )
        rounds_rows = list((await self.db.execute(rounds_stmt)).scalars().all())
        rounds_by_hid: Dict[int, list] = {}
        for r in rounds_rows:
            rounds_by_hid.setdefault(r.hypothesis_id, []).append(r)

        # --- assemble aggregates ---
        out: Dict[int, HypothesisAggregates] = {}
        for h in hyps:
            cnt = cnt_map.get(h.id, 0)
            pass_cnt = passcnt_map.get(h.id, 0)
            stale_cnt = stale_map.get(h.id, 0)
            stale_share = (stale_cnt / cnt) if cnt > 0 else None
            pass_rate = (pass_cnt / cnt) if cnt > 0 else None
            out[h.id] = HypothesisAggregates(
                hypothesis_id=h.id,
                related_alpha_count=cnt,
                current_sharpe_avg=sharpe_map.get(h.id),
                current_pass_rate=pass_rate,
                stale_share=stale_share,
                recent_rounds=rounds_by_hid.get(h.id, []),
                baseline_metrics=h.baseline_metrics,
            )
        return out

    # ------------------------------------------------------------------
    # Trigger evaluation
    # ------------------------------------------------------------------

    def _evaluate_all_triggers(
        self, aggs: HypothesisAggregates,
    ) -> List[TriggerHit]:
        hits: List[TriggerHit] = []
        for fn in (
            evaluate_dropped_sharpe,
            evaluate_no_pass_in_n_rounds,
            evaluate_pass_rate_drop,
            evaluate_attribution_hypothesis_dominant,
            evaluate_stale_alphas,
        ):
            hit = fn(aggs, self.cfg)
            if hit is not None:
                hits.append(hit)
        return hits

    # ------------------------------------------------------------------
    # LLM scoring
    # ------------------------------------------------------------------

    async def _can_call_llm(self, h, now_utc: datetime) -> bool:
        """Gating policy:

          - LLM disabled or feature flag off → no
          - per-run token budget exceeded → no
          - last call was ok within 24h → no (24h gate)
          - last call was fallback_* within LLM_SCORE_RETRY_BACKOFF_HOURS → no
            (SFX-13: status field, not ai_feedback prefix)
        """
        # (Retired ENABLE_LLM_THESIS_SCORE_ON_TRIGGER 2026-05-19 — hard-wired ON.)
        if self.llm is None:
            return False
        if self._token_used >= settings.THESIS_SCORE_PER_RUN_TOKEN_BUDGET:
            return False
        last = _to_utc_aware(h.last_thesis_score_at)
        if last is not None:
            status = h.last_thesis_score_status or ""
            backoff_h = (
                settings.LLM_SCORE_RETRY_BACKOFF_HOURS
                if status.startswith("fallback")
                else 24
            )
            if (now_utc - last).total_seconds() < backoff_h * 3600:
                return False
        return True

    async def _score_with_llm_or_fallback(
        self, h, aggs: HypothesisAggregates, hits: List[TriggerHit],
    ) -> tuple:
        """Run the LLM scoring with three-segment fallback (MFX-6):

          "ok"                       — LLM responded + parsed + validated
          "fallback_failed"          — LLM raised / empty / parse failed
          "fallback_schema_invalid"  — LLM parsed but schema validation failed

        Returns ``(LLMThesisScore, status_str)``. Always returns a usable
        score (even fallbacks emit a 50/100 neutral placeholder) so the
        persistence path can write something.
        """
        try:
            response = await self.llm.call(
                system_prompt=_load_system_prompt(),
                user_prompt=_build_user_prompt(h, aggs, hits),
                temperature=0.2,
                json_mode=True,
                max_tokens=1024,
            )
            self._token_used += int(getattr(response, "tokens_used", 0) or 0)
            if not getattr(response, "success", False):
                err = getattr(response, "error", None) or "no_error_set"
                raise RuntimeError(f"LLM call failed: {err}")
            parsed = getattr(response, "parsed", None)
            if not isinstance(parsed, dict):
                raise RuntimeError("LLM response has no parsed JSON object")
            # Pydantic validation — schema errors classified separately.
            try:
                score = LLMThesisScore(**parsed)
            except Exception as ve:
                logger.warning(
                    f"[hyp_health] schema-invalid LLM response hid={h.id}: {ve}"
                )
                return (
                    self._fallback_score(
                        f"schema invalid: {type(ve).__name__}"
                    ),
                    "fallback_schema_invalid",
                )
            return score, "ok"
        except Exception as e:
            logger.warning(
                f"[hyp_health] LLM scoring failed hid={h.id}: "
                f"{type(e).__name__}: {e}"
            )
            return (
                self._fallback_score(
                    f"LLM scoring failed: {type(e).__name__}"
                ),
                "fallback_failed",
            )

    def _fallback_score(self, why: str) -> LLMThesisScore:
        return LLMThesisScore(
            thesis_score=50,
            ai_feedback=(why or "fallback")[:600],
            recommended_action="continue",
            reasons=[why or "fallback"],
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _persist_result(
        self,
        h,
        aggs: HypothesisAggregates,
        hits: List[TriggerHit],
        llm_score: Optional[LLMThesisScore],
        llm_status: Optional[str],
        now_utc: datetime,
    ) -> None:
        """Apply mark_triggered (when hits) + update_thesis_score (when LLM
        emitted). Audit row only on False → True ``is_triggered`` edge
        (SFX-10). Commits at the end.

        SFX-9 consistency: the audit's ``sharpe_at_transition`` uses the
        FRESH ``aggs.current_sharpe_avg`` (real-time JOIN), not the
        denormalized ``h.sharpe_avg`` cache that ``refresh_stats`` may have
        left stale. The trigger reason and the recorded sharpe must agree.
        """
        from backend.models import HypothesisStatusTransition
        from backend.services.hypothesis_service import HypothesisService

        svc = HypothesisService(self.db)
        was_triggered = bool(h.is_triggered)
        if hits:
            await svc.mark_triggered(
                h.id, hits=hits, source="trigger_eval_beat",
            )
        if llm_score is not None and llm_status is not None:
            await svc.update_thesis_score(
                h.id, llm_score, scored_at=now_utc, status=llm_status,
            )
        if hits and not was_triggered:
            self.db.add(HypothesisStatusTransition(
                hypothesis_id=h.id,
                old_is_triggered=False,
                new_is_triggered=True,
                sharpe_at_transition=aggs.current_sharpe_avg,
                reason="; ".join(hit.reason for hit in hits)[:1000],
                source="trigger_eval_beat",
            ))
        await self.db.commit()

    # ------------------------------------------------------------------
    # Payload builders
    # ------------------------------------------------------------------

    def _record_row(
        self,
        h,
        aggs: HypothesisAggregates,
        hits: List[TriggerHit],
        llm_score: Optional[LLMThesisScore],
        llm_status: Optional[str],
    ) -> Dict[str, Any]:
        score_val = (
            int(llm_score.thesis_score) if llm_score is not None else None
        )
        return {
            "hypothesis_id": h.id,
            "region": h.region,
            "kind": h.kind,
            "status": h.status,
            "is_triggered": True if hits else bool(h.is_triggered),
            "triggers": [asdict(hit) for hit in hits],
            "current_sharpe_avg": aggs.current_sharpe_avg,
            "current_pass_rate": aggs.current_pass_rate,
            "related_alpha_count": aggs.related_alpha_count,
            "stale_share": aggs.stale_share,
            "baseline_metrics": aggs.baseline_metrics,
            "thesis_score": score_val,
            "llm_status": llm_status,
            "recommended_action": (
                llm_score.recommended_action if llm_score is not None else None
            ),
            "ai_feedback": (
                llm_score.ai_feedback if llm_score is not None else None
            ),
            "health_band": _band_from_score(score_val, bool(hits)),
        }

    def _build_payload(
        self, records: List[Dict[str, Any]], now_utc: datetime,
    ) -> Dict[str, Any]:
        sh_now = now_utc.astimezone(SH_TZ)
        by_band = {"GREEN": 0, "YELLOW": 0, "ORANGE": 0, "RED": 0, "CRITICAL": 0}
        for r in records:
            band = r.get("health_band", "GREEN")
            by_band[band] = by_band.get(band, 0) + 1
        triggered_count = sum(1 for r in records if r["is_triggered"])
        threshold = settings.THESIS_SCORE_DUMP_THRESHOLD

        # NTH-17: truncate — only dump non-GREEN OR thesis_score < threshold.
        def _keep(r):
            if r.get("health_band") != "GREEN":
                return True
            ts = r.get("thesis_score")
            return ts is not None and ts < threshold

        dumped = sorted(
            [r for r in records if _keep(r)],
            key=lambda r: (
                r.get("thesis_score") if r.get("thesis_score") is not None else 100,
                r["hypothesis_id"],
            ),
        )
        return {
            "report_date": sh_now.strftime("%Y-%m-%d"),
            "generated_at": sh_now.isoformat(),
            "scope": list(self.SCOPE_STATUS),
            "config": {
                "triggers": asdict(self.cfg),
                "trigger_detail_max_entries": settings.TRIGGER_DETAIL_MAX_ENTRIES,
                "stale_red_days": settings.STALE_RED_DAYS,
                "llm_token_budget_per_run": settings.THESIS_SCORE_PER_RUN_TOKEN_BUDGET,
                "llm_backoff_hours": settings.LLM_SCORE_RETRY_BACKOFF_HOURS,
                "thesis_score_dump_threshold": threshold,
            },
            "totals": {
                "checked": len(records),
                "by_band": by_band,
                "triggered_count": triggered_count,
                "failed_count": getattr(self, "_failed", 0),
            },
            "hypotheses": dumped,
            "llm_token_used": self._token_used,
        }


# ---------------------------------------------------------------------------
# Score → band (used by payload builder)
# ---------------------------------------------------------------------------


def _band_from_score(score: Optional[int], any_trigger_hit: bool) -> str:
    """Map (thesis_score, has_trigger) → health band for the payload.

    Mirrors the alpha-health-check 5-band logic but at the hypothesis
    level. GREEN requires no triggers AND no LLM call (or score >= 85).
    """
    if score is None:
        return "ORANGE" if any_trigger_hit else "GREEN"
    if score >= 85:
        return "YELLOW" if any_trigger_hit else "GREEN"
    if score >= 70:
        return "YELLOW"
    if score >= 50:
        return "ORANGE"
    if score >= 30:
        return "RED"
    return "CRITICAL"
