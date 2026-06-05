"""
Evaluation nodes for LangGraph workflow.

Enhanced with hypothesis-implementation alignment checking:
- Verifies implementations correctly reflect hypotheses
- Attributes failures to hypothesis vs implementation
- Filters knowledge based on attribution confidence

Contains:
- node_simulate: Batch simulate alphas on BRAIN platform
- node_evaluate: Evaluate alpha quality using multi-objective scoring
"""

import asyncio
import hashlib
import math
import time
import random
from typing import Dict, List, Optional, Tuple
from loguru import logger
from langchain_core.runnables import RunnableConfig

from backend.agents.graph.state import MiningState
from backend.agents.graph.nodes.base import (
    record_trace,
    _debug_log,
    EXPERIMENT_TRACKING_ENABLED,
    get_current_experiment,
)


# ---------------------------------------------------------------------------
# Phase 4 PR0.6 — sentinel attribution lookup (Sprint 0, 2026-05-19)
# ---------------------------------------------------------------------------
# Extracted to a top-level helper so the F-T1 integration test (post-S0-B
# review) can drive it with an in-memory aiosqlite session (db_session
# fixture). Production callers pass db=None which opens a fresh
# AsyncSessionLocal (mirrors the original inline behavior).


async def _pr06_lookup_mutated_hypothesis_ids(
    hypothesis_ids,
    db=None,
):
    """Return ``set[int]`` of hypothesis_ids whose ``r1b_mutation_depth >= 1``.

    Args:
        hypothesis_ids: iterable of hypothesis ids to filter
        db: optional AsyncSession. None → open AsyncSessionLocal (production).

    Soft-fail: any error returns empty set + logs debug. Never raises.
    """
    from sqlalchemy import select as _sel
    from backend.models import Hypothesis as _PR06Hyp

    _hyp_ids = {hid for hid in hypothesis_ids if hid is not None}
    if not _hyp_ids:
        return set()

    async def _query(session):
        stmt = _sel(_PR06Hyp.id).where(
            _PR06Hyp.id.in_(_hyp_ids),
            _PR06Hyp.r1b_mutation_depth >= 1,
        )
        rows = (await session.execute(stmt)).all()
        return {r[0] for r in rows}

    try:
        if db is not None:
            return await _query(db)
        from backend.database import AsyncSessionLocal
        async with AsyncSessionLocal() as _pr06_db:
            return await _query(_pr06_db)
    except Exception as ex:  # noqa: BLE001
        logger.debug(
            "[PR0.6 stamp] R1b mutation lookup failed (non-fatal): %s", ex,
        )
        return set()
from backend.adapters.brain_adapter import BrainAdapter
from backend.config import settings
from backend.agents.prompts import (
    quick_alignment_check,
    determine_attribution_heuristic,
)
from backend.alpha_routing import route_alpha_action, RoutingDecision
from backend.alpha_scoring import (
    calculate_alpha_score,
    should_optimize,
    get_failed_tests,
    evaluate_with_brain_checks,
)
from backend.services.correlation_service import CorrelationService, CorrSource
from backend.multi_fidelity_eval import RobustnessGate
# P2 review fix (2026-05-16): _quota_guard_async moved to lazy import inside
# the robustness block at L2064. Top-level import was the SOLE remaining
# `backend.tasks` top-level import in backend/agents/, closing the
# backend.agents ↔ backend.tasks cycle. Pattern matches generation.py:351,
# persistence.py:458, validation.py:479.
import redis.asyncio as _rb_redis_aio


# =============================================================================
# Helpers
# =============================================================================


def _eval_thresholds(
    *,
    delay: int = 1,
    sharpe_submit_min_override: Optional[float] = None,
) -> Dict:
    """Single flat threshold dict — delegates to Settings.eval_thresholds(delay)
    for the per-delay band (2026-05-28 fix: delay-0 has stricter BRAIN gates —
    sharpe>=2.0/fit>=1.3/sub-univ>=0.81 vs delay-1's 1.5/1.2/0.2 — confirmed
    empirically on alpha 15621).

    delay defaults to 1 for legacy callers that don't yet thread delay; new
    mining/sync callers MUST pass ``delay`` from MiningState.delay or
    Alpha.delay so a delay-0 alpha isn't judged on delay-1 thresholds.

    brain_role_snapshot override (P3-Brain) is still respected: when a running
    task carries an elevated sharpe_min in its startup snapshot, that wins over
    the band's default sharpe_min so flag flips don't re-judge mid-round alphas.
    The override applies to whichever band (delay-0 or delay-1) is selected.
    """
    band = settings.eval_thresholds(delay)
    # Both branches use max(band_default, ...) so the delay-aware band acts as
    # a FLOOR — a weaker override (e.g. a delay-0 task's Consultant snapshot
    # of 1.58, frozen from the delay-1-centric property) cannot lower the
    # delay-0 band's 2.0 BRAIN gate. The original "override always wins"
    # semantics was correct when only delay-1 was supported (override was
    # always ≥ band); it's wrong now that delay-0 has a stricter band.
    if sharpe_submit_min_override is not None:
        band["sharpe_min"] = max(band["sharpe_min"], float(sharpe_submit_min_override))
    else:
        eff = settings.effective_sharpe_submit_min_for(delay)
        band["sharpe_min"] = max(band["sharpe_min"], eff)
    return band


def _unpack_eval_thresholds(tier_cfg: Dict) -> Dict:
    """Unpack a (possibly regime-adjusted) ``_eval_thresholds()`` dict into the
    flat numeric threshold bundle the verdict logic consumes.

    Feature 1 (2026-05-24): extracted verbatim from node_evaluate's inline
    mapping (was evaluation.py:1803-1824) so the runtime evaluator AND the
    /alphas/sync reconciliation derive the same band thresholds from a single
    source of truth.

    Returns exactly the 11 NUMERIC keys read by compute_verdict_from_signals:
    sharpe_min / fitness_min / turnover_min / turnover_max / max_correlation /
    prov_sharpe_min / prov_fitness_min / prov_turnover_min / prov_turnover_max /
    score_pass_threshold / score_optimize_threshold.

    NOTE (Feature 1 T1): `check_self_corr` / `check_concentrated` (read straight
    from tier_cfg) and `corr_check_threshold` (read straight from settings) are
    deliberately NOT produced here — node_evaluate keeps reading those inline so
    this helper has a stable, verdict-only contract. The golden test asserts the
    output key set is exactly these 11.

    Depends on `settings` (non-pure): the `or settings.MAX_CORRELATION` /
    SCORE_*_THRESHOLD fallbacks mirror the original inline behavior byte-for-byte.
    The provisional fallbacks are order-dependent — prov_*_min fall back to the
    already-resolved main-band scalar — so the main-band values are computed first.
    """
    sharpe_min = tier_cfg["sharpe_min"]
    fitness_min = tier_cfg["fitness_min"]
    turnover_min = tier_cfg["turnover_min"]
    turnover_max = tier_cfg["turnover_max"]
    max_correlation = tier_cfg.get("self_corr_max") or getattr(settings, "MAX_CORRELATION", 0.7)
    prov_cfg = tier_cfg.get("provisional") or {}
    prov_sharpe_min = prov_cfg.get("sharpe_min", sharpe_min)
    prov_fitness_min = prov_cfg.get("fitness_min", 0.6)
    # V-26.80: symmetric provisional turnover band — lower bound defaults to the
    # regular turnover_min unless the tier override supplies its own.
    prov_turnover_min = prov_cfg.get("turnover_min", turnover_min)
    prov_turnover_max = prov_cfg.get("turnover_max", 0.85)
    score_pass_threshold = tier_cfg.get("score_pass", getattr(settings, "SCORE_PASS_THRESHOLD", 0.8))
    score_optimize_threshold = tier_cfg.get("score_optimize", getattr(settings, "SCORE_OPTIMIZE_THRESHOLD", 0.3))
    return {
        "sharpe_min": sharpe_min,
        "fitness_min": fitness_min,
        "turnover_min": turnover_min,
        "turnover_max": turnover_max,
        "max_correlation": max_correlation,
        "prov_sharpe_min": prov_sharpe_min,
        "prov_fitness_min": prov_fitness_min,
        "prov_turnover_min": prov_turnover_min,
        "prov_turnover_max": prov_turnover_max,
        "score_pass_threshold": score_pass_threshold,
        "score_optimize_threshold": score_optimize_threshold,
    }


def _safe_metric(metrics: dict, key: str, default: float, fallback_flags: list) -> float:
    """Read a numeric metric; fall back to `default` on missing/None/NaN/inf/bool/str.

    Mutates `fallback_flags` by appending `key` when fallback is taken.

    Rules:
      - missing key, None     → default (flag)
      - NaN / ±inf            → default (flag)
      - bool                  → default (flag, bool ⊂ int in Python)
      - str / non-numeric     → default (flag)
      - finite int/float      → float(value)

    来源: docs/alphagbm_skills_research_2026-05-15.md P1-B
    """
    val = metrics.get(key)
    if val is None:
        fallback_flags.append(key)
        return float(default)
    if isinstance(val, bool):          # must precede isinstance(val, (int, float))
        fallback_flags.append(key)
        return float(default)
    if not isinstance(val, (int, float)):
        fallback_flags.append(key)
        return float(default)
    if math.isnan(val) or math.isinf(val):
        fallback_flags.append(key)
        return float(default)
    return float(val)


def _check_is_os_consistency(metrics: Dict) -> bool:
    """V-12: reject alphas whose IS sharpe far exceeds OS sharpe.

    Spike (2026-05-02 → 03) revealed train_sharpe values up to 16.2 paired
    with test_sharpe=0 — pure IS overfit. PASS gate requires OS consistency
    when IS sharpe is elevated.

    Rules (flat post tier-system removal, 2026-05-18):
      - is_sharpe < 2:    no OS check (conservative IS already)
      - 2 <= is_sharpe < 5: require os_sharpe > 0 AND os/is >= 0.3
      - is_sharpe >= 5:   require os_sharpe > 0 AND os/is >= 0.4

    OS sharpe sources, in priority order:
      1. metrics["os_sharpe"]           (BRAIN OS-evaluated sharpe)
      2. metrics["test_sharpe"]         (BRAIN test-period split)
      Both null/zero → reject (no OS evidence).

    Returns True if the alpha is safe (i.e., not over-fit by this rule).
    """
    is_sh = (metrics.get("sharpe") if isinstance(metrics, dict) else None) or 0
    if is_sh < 2:
        return True
    os_sh = 0.0
    if isinstance(metrics, dict):
        os_sh = metrics.get("os_sharpe") or metrics.get("test_sharpe") or 0
    if os_sh is None or os_sh <= 0:
        return False
    ratio = os_sh / is_sh if is_sh > 0 else 0
    threshold = 0.4 if is_sh >= 5 else 0.3
    return ratio >= threshold


# =============================================================================
# V-16: Suspicion mode for sharpe > 3.0 alphas
# =============================================================================
# Triggered when is_sharpe > V16_SUSPICION_THRESHOLD. Six static + dynamic
# checks against well-known quant risks. Hard flags downgrade PASS →
# PASS_PROVISIONAL; soft + info flags only annotate trace_steps for review.
#
# This is NOT a substitute for V-12 (IS/OS consistency). V-12 catches
# train→test sharpe collapse; V-16 catches "too good to be true" patterns
# that survive V-12 because train AND test both look strong (e.g., perfect
# divide-by-something-tiny throughout the test window).

from backend.config import settings as _v16_settings

# V-26.68 (2026-05-13): V16_SUSPICION_THRESHOLD now sourced from settings.
# Module-level alias kept so legacy imports (tests, scripts) still find it.
V16_SUSPICION_THRESHOLD: float = _v16_settings.V16_SUSPICION_THRESHOLD

# V-P0 (2026-05-15): the three expression-only V-16 checks (divide-by-zero,
# look-ahead bias, overfit-window) moved to backend/static_alpha_checks.py so
# they can run pre-simulate inside node_validate — a bad expression should
# never burn a BRAIN sim, and look-ahead bias must be caught regardless of the
# sharpe>3 suspicion gate below. Re-export shims keep legacy imports (tests,
# scripts) working under the old names; new code should import from
# backend.static_alpha_checks directly.
from backend.static_alpha_checks import (  # noqa: E402
    check_divide_by_zero as _v16_check_divide_by_zero,
    check_lookahead_bias as _v16_check_lookahead,
    check_overfit_window as _v16_check_overfit_window,
)


def _v16_check_outliers(metrics: Dict) -> list:
    """Risk 6: data-anomaly metrics."""
    flags = []
    if not isinstance(metrics, dict):
        return flags
    returns = metrics.get("returns") or 0
    drawdown = metrics.get("drawdown") or 0
    fitness = metrics.get("fitness") or 0
    sharpe = metrics.get("sharpe") or 0
    if returns > 1.0:  # >100% annual return
        flags.append(f"returns={returns:.2%} unrealistic for diversified portfolio")
    if drawdown == 0 and abs(sharpe) > 0.5:
        flags.append("drawdown=0 with non-trivial sharpe — simulation anomaly likely")
    if fitness > 10 and sharpe < 5:
        flags.append(f"fitness={fitness:.1f} but sharpe={sharpe:.1f} — fitness/sharpe inconsistency")
    return flags


def _v16_check_cost_vacuum(metrics: Dict) -> str | None:
    """Risk 4: high turnover + extreme sharpe = cost-model insensitive alpha."""
    if not isinstance(metrics, dict):
        return None
    turnover = metrics.get("turnover") or 0
    sharpe = metrics.get("sharpe") or 0
    # >50% turnover + sharpe>5 means the alpha trades aggressively yet still
    # claims abnormal returns. BRAIN cost-models, but the alpha may exploit
    # specific cost-model gaps (e.g., unrealistic instant fills).
    # V-26.68: thresholds sourced from settings.
    if (turnover > _v16_settings.V16_COST_VACUUM_TURNOVER
            and sharpe > _v16_settings.V16_COST_VACUUM_SHARPE):
        return f"turnover={turnover:.2f} + sharpe={sharpe:.2f} — cost-model insensitivity risk"
    return None


def _run_suspicion_checks(metrics: Dict, expression: str) -> list:
    """V-16: metric-dependent risk audit when is_sharpe > V16_SUSPICION_THRESHOLD.

    Returns list[dict] with shape:
      {"check": str, "severity": "hard" | "soft" | "info", "evidence": str}

    Severity semantics:
      hard — downgrade PASS → PASS_PROVISIONAL (alpha needs review)
      soft — annotate metrics, keep status
      info — manual-only, e.g. survivorship bias

    Returns [] when sharpe ≤ threshold.

    V-P0 (2026-05-15): the three expression-only checks (Risk 1 divide-by-zero,
    Risk 2 look-ahead bias, Risk 5 overfit-window) moved to node_validate via
    backend/static_alpha_checks.py — they need no metrics and should run
    pre-simulate. Only the metric-dependent checks remain here: Risk 3
    survivorship (info), Risk 4 cost-vacuum, Risk 6 outlier metrics. The
    `expression` parameter is kept for signature stability of the call sites.
    """
    flags: list = []
    if not isinstance(metrics, dict):
        return flags
    sharpe = metrics.get("sharpe") or 0
    if sharpe <= V16_SUSPICION_THRESHOLD:
        return flags

    # Risk 3: survivorship bias — system-level, manual review only
    flags.append({
        "check": "survivorship_bias",
        "severity": "info",
        "evidence": "BRAIN universe selection inherits survivorship; review at portfolio construction.",
    })

    # Risk 4: cost vacuum
    flag = _v16_check_cost_vacuum(metrics)
    if flag:
        flags.append({"check": "cost_vacuum", "severity": "hard", "evidence": flag})

    # Risk 6: data-anomaly outliers
    for outlier_msg in _v16_check_outliers(metrics):
        flags.append({"check": "outlier_metric", "severity": "hard", "evidence": outlier_msg})

    return flags


from dataclasses import dataclass, field as _dc_field


@dataclass
class VerdictResult:
    """Output of compute_verdict_from_signals (Feature 1, 2026-05-24).

    decision                — RoutingDecision (status/reason/band)
    v16_flags               — full V-16 suspicion flag list (node writes
                              alpha.metrics["_v16_suspicion_flags"])
    brain_actionable_fails  — actionable BRAIN fail names (node writes
                              "_brain_pass_downgrade" / "_brain_actionable_fails")
    hard_gate_pass          — informational only; no mining consumer reads it
                              (kept for sync telemetry / debugging).
    """
    decision: RoutingDecision
    v16_flags: list
    brain_actionable_fails: list
    hard_gate_pass: bool


def compute_verdict_from_signals(
    *,
    metrics: Dict,
    sharpe: float,
    fitness: float,
    turnover: float,
    self_corr: float,
    self_corr_source: object,
    meets_thresholds: bool,
    brain_check_details_present: bool,
    brain_failed_checks: list,
    brain_can_submit: bool,
    score: float,
    should_opt: bool,
    expression: str,
    th: Dict,
    check_self_corr: bool,
    check_concentrated: bool,
) -> VerdictResult:
    """Map already-computed alpha signals to a routing decision.

    Feature 1 (2026-05-24): verbatim extraction of node_evaluate's verdict block
    (was evaluation.py:559-639) so the runtime evaluator AND /alphas/sync derive
    quality_status through the SAME logic. The mining caller passes the live
    signals it just computed; sync builds equivalent signals from BRAIN data and
    passes score=0 / should_opt=False (so sync never emits OPTIMIZE).

    `th` is the 11-key bundle from _unpack_eval_thresholds().

    The ONLY change vs the original block is the brain_actionable_fails name
    extraction (T1 of the 3rd review): `_n = c.get("name") if isinstance(c, dict)
    else c`. evaluate_with_brain_checks returns failed_checks as list[str], so the
    original `c.get("name")` would crash on a non-empty list — dormant in mining
    (checks empty at eval time) but live in sync (checks populated). The dict
    branch is defensive; both real callers feed str-lists.
    """
    sub_universe_check = next(
        (c for c in metrics.get("checks", [])
         if c.get("name") == "LOW_SUB_UNIVERSE_SHARPE"),
        None,
    )
    sub_universe_ok = (
        sub_universe_check is None
        or sub_universe_check.get("result") != "FAIL"
    )
    concentrated_check = next(
        (c for c in metrics.get("checks", [])
         if c.get("name") == "CONCENTRATED_WEIGHT"),
        None,
    )
    if check_concentrated:
        concentrated_ok = (
            concentrated_check is None
            or concentrated_check.get("result") != "FAIL"
        )
    else:
        concentrated_ok = True

    # PR2: tier-aware self_corr gate
    if check_self_corr:
        self_corr_ok = self_corr < th["max_correlation"]
        self_corr_verified = self_corr_source not in (CorrSource.UNKNOWN, "unknown")
    else:
        self_corr_ok = True
        self_corr_verified = True  # tier_skipped, not unknown

    is_overfit_safe = _check_is_os_consistency(metrics)

    hard_gate_pass = (
        sharpe >= th["sharpe_min"]
        and fitness >= th["fitness_min"]
        and th["turnover_min"] <= turnover <= th["turnover_max"]
        and sub_universe_ok
        and concentrated_ok
        and self_corr_ok
        and self_corr_verified
        and is_overfit_safe
    )

    self_corr_acceptable = self_corr_ok or not self_corr_verified
    near_pass = (
        sharpe >= th["prov_sharpe_min"]
        and fitness >= th["prov_fitness_min"]
        and th["prov_turnover_min"] <= turnover <= th["prov_turnover_max"]
        and sub_universe_ok
        and concentrated_ok
        and self_corr_acceptable
    )

    v16_flags = _run_suspicion_checks(metrics, expression or "")
    hard_v16_flags = [f for f in v16_flags if f.get("severity") == "hard"]
    brain_actionable_fails_list = []
    for c in brain_failed_checks or []:
        _n = c.get("name") if isinstance(c, dict) else c
        if _n in (
            "LOW_FITNESS",
            "LOW_SHARPE",
            "CONCENTRATED_WEIGHT",
            "HIGH_TURNOVER",
            "LOW_TURNOVER",
            "MATCHES_PYRAMID",
            "HIGH_CORRELATION",
            "SELF_CORRELATION",
        ):
            brain_actionable_fails_list.append(_n)
    decision = route_alpha_action(
        hard_gate_pass=hard_gate_pass,
        meets_thresholds=meets_thresholds,
        score=score,
        score_pass_threshold=th["score_pass_threshold"],
        has_v16_hard_flags=bool(hard_v16_flags),
        brain_checks_present=brain_check_details_present,
        brain_actionable_fails=bool(brain_actionable_fails_list),
        brain_can_submit=brain_can_submit,
        near_pass=near_pass,
        should_optimize=should_opt,
        score_optimize_threshold=th["score_optimize_threshold"],
    )
    return VerdictResult(
        decision=decision,
        v16_flags=v16_flags,
        brain_actionable_fails=brain_actionable_fails_list,
        hard_gate_pass=hard_gate_pass,
    )


@dataclass
class _EvalCtx:
    """Per-round context bundle passed to _evaluate_single_alpha.

    Avoids threading 20+ parameters through the helper signature.
    """
    state: "MiningState"
    brain: object          # BrainAdapter | None
    correlation_service: object  # CorrelationService | None
    node_name: str

    # tier thresholds
    sharpe_min: float
    fitness_min: float
    turnover_min: float
    turnover_max: float
    max_correlation: float
    check_self_corr: bool
    check_concentrated: bool
    prov_sharpe_min: float
    prov_fitness_min: float
    prov_turnover_min: float
    prov_turnover_max: float
    score_pass_threshold: float
    score_optimize_threshold: float
    corr_check_threshold: float

    # cross-alpha accumulators (helper appends; caller reads after the loop)
    eval_details: list = _dc_field(default_factory=list)
    failure_feedback_queue: list = _dc_field(default_factory=list)

    # P2-C (2026-05-16) MF5: regime label threaded through the ctx so
    # _evaluate_single_alpha can stamp it on alpha.metrics deterministically.
    # None = no regime injection happened this round (legacy path).
    regime_for_eval: Optional[str] = None


@dataclass
class _SingleAlphaEvalResult:
    """What _evaluate_single_alpha returns for telemetry (alpha is mutated in-place)."""
    corr_check_performed: bool = False
    corr_check_skipped_reason: Optional[str] = None


async def _evaluate_single_alpha(
    alpha: "object",
    ctx: "_EvalCtx",
) -> _SingleAlphaEvalResult:
    """Evaluate one alpha candidate in-place, mutating alpha.quality_status and alpha.metrics.

    Returns _SingleAlphaEvalResult with telemetry only (counters live in node_evaluate).

    来源: docs/alphagbm_skills_research_2026-05-15.md P1-B — per-alpha try/except
    """
    state = ctx.state
    brain = ctx.brain
    correlation_service = ctx.correlation_service
    node_name = ctx.node_name

    sharpe_min = ctx.sharpe_min
    fitness_min = ctx.fitness_min
    turnover_min = ctx.turnover_min
    turnover_max = ctx.turnover_max
    max_correlation = ctx.max_correlation
    check_self_corr = ctx.check_self_corr
    check_concentrated = ctx.check_concentrated
    prov_sharpe_min = ctx.prov_sharpe_min
    prov_fitness_min = ctx.prov_fitness_min
    prov_turnover_min = ctx.prov_turnover_min
    prov_turnover_max = ctx.prov_turnover_max
    score_pass_threshold = ctx.score_pass_threshold
    score_optimize_threshold = ctx.score_optimize_threshold
    corr_check_threshold = ctx.corr_check_threshold

    # local telemetry flags
    _corr_performed: bool = False
    _corr_skipped_reason: Optional[str] = None

    # P1-B: explicit init instead of locals().get() -- PEP 667 safe
    self_corr_source: object = "skipped"

    metrics = alpha.metrics or {}

    train_sharpe_val = metrics.get("train_sharpe")
    train_fitness_val = metrics.get("train_fitness")
    test_sharpe_val = metrics.get("test_sharpe")
    test_fitness_val = metrics.get("test_fitness")

    # V-27.77: do NOT fabricate the OS/test leg.
    if test_sharpe_val is not None and test_fitness_val is not None:
        test_leg = {"sharpe": test_sharpe_val, "fitness": test_fitness_val}
    else:
        test_leg = {}

    # 构建完整的 sim_result，包含 BRAIN 返回的 checks
    sim_result = {
        "train": {
            "sharpe": train_sharpe_val if train_sharpe_val is not None else metrics.get("sharpe", 0),
            "fitness": train_fitness_val if train_fitness_val is not None else metrics.get("fitness", 0),
            "turnover": metrics.get("turnover", 0),
            "returns": metrics.get("returns", 0),
        },
        "test": test_leg,
        "is": {
            "sharpe": metrics.get("sharpe", 0),
            "fitness": metrics.get("fitness", 0),
            "turnover": metrics.get("turnover", 0),
            "drawdown": metrics.get("drawdown", 0),
            "longCount": metrics.get("longCount"),
            "shortCount": metrics.get("shortCount"),
            "checks": metrics.get("checks", []),  # BRAIN 官方检查结果
        },
        "riskNeutralized": metrics.get("riskNeutralized", {}),
        "investabilityConstrained": metrics.get("investabilityConstrained", {}),
        "checks": metrics.get("checks", []),  # 顶层也放一份
        "can_submit": metrics.get("can_submit", False),
    }

    # 新增：使用 BRAIN 官方检查结果进行快速判断
    brain_eval = evaluate_with_brain_checks(sim_result)
    brain_can_submit = brain_eval.get('can_submit', False)
    brain_failed_checks = brain_eval.get('failed_checks', [])

    # Stage 1: Preliminary score WITHOUT correlation
    preliminary_score = calculate_alpha_score(
        sim_result=sim_result,
        prod_corr=0.0,
        self_corr=0.0
    )

    # P1-B: use _safe_metric for sharpe/fitness/turnover to handle NaN/inf/bool/str
    fallback_flags: list = []
    sharpe = _safe_metric(metrics, "sharpe", 0.0, fallback_flags)
    turnover = _safe_metric(metrics, "turnover", 0.0, fallback_flags)
    fitness = _safe_metric(metrics, "fitness", 0.0, fallback_flags)

    # 使用 BRAIN 官方检查或本地阈值
    if brain_eval['check_details']:
        # 有官方检查结果，以官方为准
        meets_thresholds = brain_can_submit or (not brain_failed_checks)
    else:
        # Fallback: 使用本地阈值
        meets_thresholds = (
            sharpe >= sharpe_min and
            turnover <= turnover_max and
            fitness >= fitness_min
        )

    # Stage 2: Correlation check for promising candidates
    prod_corr = 0.0
    self_corr = 0.0
    needs_corr_check = check_self_corr and (
        preliminary_score >= corr_check_threshold or
        meets_thresholds
    )

    if needs_corr_check and brain and alpha.alpha_id:
        _corr_performed = True
        try:
            prod_corr_result = await brain.check_correlation(alpha.alpha_id, check_type="PROD")
            # P3-Brain (2026-05-16): check_correlation returns
            # {"status_code": int, "data": {...}}. Tolerate the legacy bare
            # {"max": ...} shape too — keeps in-tree fakes working.
            if isinstance(prod_corr_result, dict):
                _prod_data = (
                    prod_corr_result["data"]
                    if "status_code" in prod_corr_result
                    and isinstance(prod_corr_result.get("data"), dict)
                    else prod_corr_result
                )
                prod_corr = float(_prod_data.get("max", 0.0) or 0.0)
        except Exception as e:
            logger.warning(f"[{node_name}] PROD correlation check failed for {alpha.alpha_id}: {e}")

        self_corr_source = CorrSource.UNKNOWN
        if correlation_service is not None:
            try:
                _corr_raw, self_corr_source = await correlation_service.get_with_fallback(
                    alpha.alpha_id, region=state.region
                )
                self_corr = _corr_raw if _corr_raw is not None else 0.0
            except Exception as e:
                logger.warning(f"[{node_name}] correlation_service failed for {alpha.alpha_id}: {e}")
                self_corr_source = CorrSource.UNKNOWN

            # Orthogonality Phase A — record the dense signal (1 - measured
            # self_corr to the submitted/OS pool) for the A/B. get_with_fallback's
            # LOCAL tier (calc_self_corr) already FETCHES a fresh candidate's PnL +
            # correlates it vs the pool, so this covers freshly-mined alphas
            # whenever their PnL is fetchable (no separate helper needed — that was
            # redundant; the live path was confirmed via _self_corr_source=local).
            # UNKNOWN → skip (the 0.0 default would falsely read as fully
            # orthogonal; UNKNOWN = PnL-not-ready / stale cache, not fixable here).
            # MUST write into the `metrics` LOCAL: the function rebuilds
            # alpha.metrics = {**metrics, ...} at the end (:895), so a stamp on a
            # fresh alpha.metrics copy here is CLOBBERED — that clobber is why the
            # prior wiring never persisted a score.
            if self_corr_source != CorrSource.UNKNOWN:
                metrics["orthogonality_score"] = round(
                    1.0 - min(max(float(self_corr), 0.0), 1.0), 4)

            if self_corr_source == CorrSource.LOCAL:
                try:
                    crisis_by_window = await correlation_service.calc_self_corr_by_window(
                        alpha_id=alpha.alpha_id, region=state.region
                    )
                    if isinstance(alpha.metrics, dict):
                        alpha.metrics = dict(alpha.metrics)
                    else:
                        alpha.metrics = {}
                    alpha.metrics["_crisis_correlations"] = crisis_by_window
                    spikes = [
                        (w, info["max_corr"])
                        for w, info in crisis_by_window.items()
                        if info.get("status") == "ok"
                        and info.get("max_corr", 0.0) >= max_correlation
                    ]
                    if spikes:
                        logger.info(
                            f"[{node_name}] {alpha.alpha_id} crisis-corr spikes: "
                            f"{spikes} (global self_corr={self_corr:.3f})"
                        )
                except Exception as e:
                    logger.warning(
                        f"[{node_name}] crisis-window corr failed for {alpha.alpha_id}: {e}"
                    )
        else:
            try:
                self_corr_result = await brain.check_correlation(alpha.alpha_id, check_type="SELF")
                # P3-Brain (2026-05-16): see PROD branch above re: shape.
                if isinstance(self_corr_result, dict):
                    _self_data = (
                        self_corr_result["data"]
                        if "status_code" in self_corr_result
                        and isinstance(self_corr_result.get("data"), dict)
                        else self_corr_result
                    )
                    self_corr = float(_self_data.get("max", 0.0) or 0.0)
                    self_corr_source = CorrSource.BRAIN
            except Exception as e:
                logger.warning(f"[{node_name}] SELF correlation check failed for {alpha.alpha_id}: {e}")
    else:
        _corr_skipped_reason = "tier_skipped" if not check_self_corr else "skipped"
        # tier_skipped means "by tier policy, not because we couldn't measure" —
        # downstream gate should treat as ok+verified, NOT downgrade to PROVISIONAL
        self_corr_source = "tier_skipped" if not check_self_corr else "skipped"

    # Final score with correlation penalty
    score = calculate_alpha_score(
        sim_result=sim_result,
        prod_corr=prod_corr,
        self_corr=self_corr
    )

    should_opt, opt_reason = should_optimize(sim_result)
    failed_tests = get_failed_tests(sim_result)

    # Feature 1 (2026-05-24): verdict logic extracted to the shared
    # compute_verdict_from_signals so /alphas/sync derives quality_status
    # through the SAME band/route logic. Behaviour here is unchanged — the
    # threshold scalars below were unpacked into ctx by node_evaluate via
    # _unpack_eval_thresholds; we repack them into the verdict bundle.
    _th = {
        "sharpe_min": sharpe_min,
        "fitness_min": fitness_min,
        "turnover_min": turnover_min,
        "turnover_max": turnover_max,
        "max_correlation": max_correlation,
        "prov_sharpe_min": prov_sharpe_min,
        "prov_fitness_min": prov_fitness_min,
        "prov_turnover_min": prov_turnover_min,
        "prov_turnover_max": prov_turnover_max,
        "score_pass_threshold": score_pass_threshold,
        "score_optimize_threshold": score_optimize_threshold,
    }
    _verdict = compute_verdict_from_signals(
        metrics=metrics,
        sharpe=sharpe,
        fitness=fitness,
        turnover=turnover,
        self_corr=self_corr,
        self_corr_source=self_corr_source,
        meets_thresholds=meets_thresholds,
        brain_check_details_present=bool(brain_eval['check_details']),
        brain_failed_checks=brain_failed_checks,
        brain_can_submit=brain_can_submit,
        score=score,
        should_opt=should_opt,
        expression=alpha.expression or "",
        th=_th,
        check_self_corr=check_self_corr,
        check_concentrated=check_concentrated,
    )
    decision = _verdict.decision
    v16_flags = _verdict.v16_flags
    brain_actionable_fails_list = _verdict.brain_actionable_fails
    alpha.quality_status = decision.status

    if decision.status == "FAIL":
        # Enhanced: Alignment check and attribution for failures
        alignment_issues = []
        attribution = "unknown"

        hypothesis_dict = {}
        if hasattr(alpha, 'hypothesis') and alpha.hypothesis:
            if isinstance(alpha.hypothesis, dict):
                hypothesis_dict = alpha.hypothesis
            else:
                hypothesis_dict = {"statement": alpha.hypothesis}

        if hypothesis_dict and alpha.expression:
            is_aligned, alignment_issues = quick_alignment_check(
                hypothesis_dict, alpha.expression, state.fields
            )

            result_dict = {
                "success": False,
                "sharpe": sharpe,
                "fitness": fitness,
                "turnover": turnover,
            }
            attribution = determine_attribution_heuristic(
                result_dict, alignment_issues, alpha.validation_error
            )

            if not is_aligned:
                logger.debug(
                    f"[{node_name}] Alignment issues for {alpha.alpha_id}: {alignment_issues[:2]}"
                )

        error_type = "QUALITY_FAIL"
        brain_fail_priority = (
            "CONCENTRATED_WEIGHT",
            "LOW_SUB_UNIVERSE_SHARPE",
            "HIGH_PROD_CORRELATION",
            "HIGH_SELF_CORRELATION",
        )
        brain_fails = {
            c.get("name"): c
            for c in metrics.get("checks", []) or []
            if c.get("result") == "FAIL"
        }
        for name in brain_fail_priority:
            if name in brain_fails:
                error_type = name
                break
        if error_type == "QUALITY_FAIL":
            if sharpe < sharpe_min:
                error_type = "LOW_SHARPE"
            elif fitness < fitness_min:
                error_type = "LOW_FITNESS"
            elif turnover > turnover_max:
                error_type = "HIGH_TURNOVER"
            elif sharpe < 0:
                error_type = "NEGATIVE_SIGNAL"

        if alpha.expression:
            ctx.failure_feedback_queue.append({
                "expression": alpha.expression,
                "error_type": error_type,
                "metrics": metrics,
                "region": state.region,
                "dataset_id": state.dataset_id,
                "hypothesis": hypothesis_dict.get("statement", ""),
                "alignment_issues": alignment_issues,
                "attribution": attribution,
            })

    # Store detailed metrics with BRAIN checks info
    # P1-B: append _metrics_fallback_flags to the spread
    alpha.metrics = {
        **metrics,
        "_score": round(score, 4),
        "_preliminary_score": round(preliminary_score, 4),
        "_prod_corr": round(prod_corr, 4) if prod_corr else None,
        "_self_corr": (
            round(self_corr, 4)
            if self_corr_source in (CorrSource.LOCAL, CorrSource.BRAIN)
            else None
        ),
        "_self_corr_source": str(self_corr_source),
        "_corr_checked": needs_corr_check,
        "_should_optimize": should_opt,
        "_optimize_reason": opt_reason,
        "_failed_tests": failed_tests,
        "_brain_can_submit": brain_can_submit,
        "_brain_failed_checks": brain_failed_checks,
        "_brain_pending_checks": brain_eval.get('pending_checks', []),
        "_pyramid_multiplier": (brain_eval.get('pyramid_info') or {}).get('multiplier', 1.0),
        "_metrics_fallback_flags": fallback_flags,
    }

    # Routing annotations
    if decision.status in ("PASS", "PASS_PROVISIONAL"):
        alpha.metrics["_routing_reason"] = decision.reason
        if v16_flags:
            alpha.metrics["_v16_suspicion_flags"] = v16_flags
            if decision.reason == "near_pass":
                logger.warning(
                    f"[{node_name}] V-16 suspicion mode on PROVISIONAL "
                    f"(sharpe={sharpe:.2f}) | flags={[f['check'] for f in v16_flags]}"
                )
            else:
                logger.warning(
                    f"[{node_name}] V-16 suspicion mode (sharpe={sharpe:.2f}) | "
                    f"flags={[f['check'] for f in v16_flags]}"
                )
        if decision.reason == "brain_checks_unverified":
            alpha.metrics["_brain_checks_unverified"] = True
            logger.info(
                f"[{node_name}] PASS→PROVISIONAL: BRAIN returned no checks "
                f"(gate unverified) | sharpe={sharpe:.2f}"
            )
        elif decision.reason == "brain_actionable_fails":
            alpha.metrics["_brain_pass_downgrade"] = brain_actionable_fails_list
            logger.info(
                f"[{node_name}] PASS→PROVISIONAL: BRAIN rejected on "
                f"{brain_actionable_fails_list} | sharpe={sharpe:.2f} "
                f"fitness={fitness:.2f} expr={(alpha.expression or '')[:80]}"
            )
        elif decision.reason == "near_pass" and brain_actionable_fails_list:
            alpha.metrics["_brain_actionable_fails"] = brain_actionable_fails_list

    _debug_log("F", "nodes.py:evaluate:alpha_detail", f"Alpha evaluated: {alpha.quality_status}", {
        "alpha_id": alpha.alpha_id,
        "expression": alpha.expression[:80] if alpha.expression else None,
        "sharpe": round(sharpe, 3),
        "fitness": round(fitness, 3),
        "turnover": round(turnover, 3),
        "score": round(score, 3),
        "status": alpha.quality_status
    })

    ctx.eval_details.append({
        "id": alpha.alpha_id,
        "status": alpha.quality_status,
        "score": round(score, 4),
        "sharpe": sharpe,
        "fitness": fitness,
        "turnover": turnover,
        "corr_checked": needs_corr_check,
        "optimize_reason": opt_reason if should_opt else None,
    })

    # P2-C (2026-05-16) MF5 + S8 stamp. Fires whenever strategy.regime was
    # injected (i.e. at least one of ENABLE_REGIME_AWARE_THRESHOLDS /
    # ENABLE_STYLE_PRESET_GUIDANCE was on at mining_agent time), so the
    # ``_regime_at_eval`` audit hook stays present even on the data-
    # collection-only path (AWARE=True + STYLE=False).
    # ``_regime_applied_thresholds`` is True only when the AWARE flag is
    # on AND the multipliers were applied — orthogonal to the prompt-side
    # style block (S8).
    # V-26.79 defence: re-bind alpha.metrics to a fresh dict before mutating
    # so we don't punch through into a detached/parent state copy.
    if ctx.regime_for_eval:
        try:
            from backend.config import settings as _p2c_eval_settings
            if not isinstance(alpha.metrics, dict):
                alpha.metrics = {}
            else:
                alpha.metrics = dict(alpha.metrics)
            alpha.metrics["_regime_at_eval"] = ctx.regime_for_eval
            if getattr(
                _p2c_eval_settings, "ENABLE_REGIME_AWARE_THRESHOLDS", False,
            ):
                alpha.metrics["_regime_applied_thresholds"] = True
        except Exception as _p2c_stamp_ex:
            logger.warning(
                f"[{ctx.node_name}] P2-C stamp failed (non-fatal): "
                f"{_p2c_stamp_ex}"
            )

    # B1 R11 (Sprint 2): stamp capacity_usd_estimate on the alpha row +
    # mirror to alpha.metrics so the downstream persistence path (which
    # only persists alpha.metrics, not arbitrary attributes) carries it.
    # Gated by ENABLE_CAPACITY_SCORE so OFF stays byte-identical with the
    # historical alpha row shape (NULL column). Soft-fail on any error —
    # the column is nullable.
    try:
        from backend.config import settings as _r11_settings
        if getattr(_r11_settings, "ENABLE_CAPACITY_SCORE", False):
            from backend.services import capacity_estimator as _cap_svc
            # region/universe come from the mining state, NOT the candidate:
            # AlphaCandidate has no region/universe attr, so the old
            # getattr(alpha, ...) returned None and estimate_from_alpha_dict
            # short-circuited to 0 (capacity was never stamped on any alpha).
            # turnover lives on the post-sim metrics dict ("turnover" key).
            _cap_metrics = alpha.metrics if isinstance(alpha.metrics, dict) else {}
            _alpha_for_cap = {
                "region": ctx.state.region,
                "universe": ctx.state.universe,
                "turnover": _cap_metrics.get("turnover"),
            }
            _cap_usd = _cap_svc.estimate_from_alpha_dict(_alpha_for_cap)
            if _cap_usd > 0:
                if not isinstance(alpha.metrics, dict):
                    alpha.metrics = {}
                else:
                    alpha.metrics = dict(alpha.metrics)
                alpha.metrics["capacity_usd_estimate"] = float(_cap_usd)
                try:
                    setattr(alpha, "capacity_usd_estimate", float(_cap_usd))
                except Exception:
                    pass
    except Exception as _r11_stamp_ex:
        logger.debug(
            f"[{ctx.node_name}] R11 capacity stamp soft-fall: {_r11_stamp_ex}"
        )

    return _SingleAlphaEvalResult(
        corr_check_performed=_corr_performed,
        corr_check_skipped_reason=_corr_skipped_reason,
    )


# =============================================================================
# NODE: Simulate
# =============================================================================

# Pre-simulate candidate.metrics annotations that must survive node_simulate's
# wholesale `updated.metrics = res["metrics"]` overwrite. The BRAIN sim result
# is authoritative — _carry_pre_sim_metrics uses setdefault, so it never
# clobbers a key the result already set — this only rescues additive,
# namespaced telemetry the sim result never emits: node_validate findings /
# risk-bounds, plus pre-sim shadow/inject stamps (soft-reg P1/P2, Q10 prescreen,
# G10 inject, G3-v2 grammar, assistant-mode A1.3). Without it they're silently
# dropped before persistence (only post-simulate node_evaluate stamps survive),
# which is why shadow soft-reg telemetry never reached the DB. Extend this when
# adding a new pre-sim `_xxx_` telemetry family.
_CARRY_PRE_SIM_METRIC_PREFIXES = (
    "_validation_", "_risk_bounds",
    "_soft_reg", "_qlib_prescreen", "_g10_", "_g3v2_",
    "llm_mode_used", "assistant_template",
)


def _carry_pre_sim_metrics(result_metrics: dict, candidate_metrics) -> dict:
    """Merge BRAIN ``result_metrics`` (authoritative) with the allow-listed
    pre-simulate ``candidate_metrics`` annotations that the wholesale metrics
    overwrite would otherwise drop. ``setdefault`` → result wins on collision.
    """
    merged = dict(result_metrics or {})
    if isinstance(candidate_metrics, dict):
        for _k, _v in candidate_metrics.items():
            if _k.startswith(_CARRY_PRE_SIM_METRIC_PREFIXES):
                merged.setdefault(_k, _v)
    return merged


async def _apply_soft_regularizer(
    state,
    indices_to_simulate: list,
    cand_exprs: list,
    probas: list,
    keep_local: list,
    skip_local: list,
    threshold: float,
    *,
    node_name: str = "node_simulate",
):
    """AlphaAgent-style soft regularizer over pre-simulate candidates (P1+P2).

    P1 legs = complexity + originality (cheap, every candidate). P2 leg = R5
    c1/c2 alignment (LLM, only the top-N most-promising candidates, gated on
    W_ALIGNMENT>0). Legs blend into a [0,1] penalty; the alignment leg is
    one-sided (may only raise it). shadow → stamp alpha.metrics['_soft_reg_*']
    only; soft → also down-weight P(PASS) = p*(1-lambda*penalty) and re-derive
    keep/skip (can only skip MORE, never resurrect a classifier-skip).

    Returns (keep_local, skip_local, probas). Soft-fail invariant: when mode is
    off (not shadow|soft) or on ANY error, returns the inputs unchanged —
    byte-for-byte what filter_candidates produced.
    """
    _sr_mode = (getattr(settings, "CODE_GEN_SOFT_REG_MODE", "shadow") or "shadow").lower()
    if _sr_mode not in ("shadow", "soft") or not cand_exprs:
        return keep_local, skip_local, probas
    try:
        from backend.agents.services import soft_regularizer as _softreg
        from backend.alpha_originality import OriginalityChecker as _SRChecker

        _sr_checker = _SRChecker()
        try:
            await _sr_checker.load_history(
                task_id=getattr(state, "task_id", None),
                region=getattr(state, "region", None),
            )
        except Exception as _sr_hist_e:  # noqa: BLE001
            logger.debug(f"[{node_name}] soft-reg history load failed: {_sr_hist_e}")

        _sr_w_c = float(getattr(settings, "CODE_GEN_SOFT_REG_W_COMPLEXITY", 0.5))
        _sr_w_o = float(getattr(settings, "CODE_GEN_SOFT_REG_W_ORIGINALITY", 0.5))
        _sr_w_a = float(getattr(settings, "CODE_GEN_SOFT_REG_W_ALIGNMENT", 0.0))
        _sr_c0 = float(getattr(settings, "CODE_GEN_SOFT_REG_COMPLEXITY_C0", 6.0))
        _sr_cmax = float(getattr(settings, "CODE_GEN_SOFT_REG_COMPLEXITY_CMAX", 16.0))
        _sr_lambda = float(getattr(settings, "CODE_GEN_SOFT_REG_LAMBDA", 0.5))

        # Originality AST-distance per candidate (cheap legs need it).
        _dists = []
        for _expr in cand_exprs:
            try:
                _dists.append(_sr_checker.check(_expr or "").min_distance)
            except Exception:  # noqa: BLE001
                _dists.append(None)

        # P2 alignment leg (R5 c1/c2). Master switch = W_ALIGNMENT > 0 (default
        # 0 → leg dormant, no LLM cost). R5 is 2 LLM calls/candidate so only the
        # most-promising candidates earn it: rank by the cheap 2-leg effective
        # P(PASS) and judge the top-N (TOPK in soft, SHADOW_SAMPLE in shadow).
        _judge_idx: set = set()
        if _sr_w_a > 0.0:
            _n_judge = int(
                getattr(settings, "CODE_GEN_SOFT_REG_ALIGNMENT_TOPK", 3)
                if _sr_mode == "soft"
                else getattr(settings, "CODE_GEN_SOFT_REG_ALIGNMENT_SHADOW_SAMPLE", 1)
            )
            if _n_judge > 0:
                _cheap_eff = [
                    _softreg.evaluate_candidate(
                        _expr or "", _dists[_li], float(probas[_li]),
                        w_complexity=_sr_w_c, w_originality=_sr_w_o, w_alignment=0.0,
                        c0=_sr_c0, cmax=_sr_cmax, lam=_sr_lambda, mode="soft",
                    ).p_pass_adjusted
                    for _li, _expr in enumerate(cand_exprs)
                ]
                _judge_idx = set(_softreg.select_topk_indices(_cheap_eff, _n_judge))

        # Run the top-N R5 alignment judges concurrently — each is 2 sequential
        # LLM calls (c1, c2); gathering across the K candidates collapses
        # K×latency → ~1×. return_exceptions keeps the per-candidate soft-fail:
        # a failed judge → composite None → 0 alignment penalty.
        _r5_by_idx: dict = {}
        if _judge_idx:
            from backend.agents.graph.r5_judge import run_r5_judge
            from backend.agents.services.llm_service import get_llm_service
            _r5_llm = get_llm_service()
            _judge_order = sorted(_judge_idx)
            _r5_results = await asyncio.gather(*[
                run_r5_judge(
                    hypothesis_statement=getattr(
                        state.pending_alphas[indices_to_simulate[_ji]], "hypothesis", "") or "",
                    description=getattr(
                        state.pending_alphas[indices_to_simulate[_ji]], "explanation", "") or "",
                    expression=cand_exprs[_ji] or "",
                    llm_service=_r5_llm,
                )
                for _ji in _judge_order
            ], return_exceptions=True)
            _r5_by_idx = dict(zip(_judge_order, _r5_results))

        _sr_adj = list(probas)
        for _li, _expr in enumerate(cand_exprs):
            _a = state.pending_alphas[indices_to_simulate[_li]]
            # Only the top-N judged candidates get the costly alignment leg;
            # the rest use the cheap 2-leg blend (w_alignment=0).
            _judged = _li in _judge_idx
            _a_pen = 0.0
            _w_a_eff = 0.0
            _r5_extra: dict = {"_soft_reg_alignment_judged": _judged}
            if _judged:
                _r5p = _r5_by_idx.get(_li)
                _composite = None
                _r5_cost = 0.0
                if isinstance(_r5p, dict):
                    _composite = _r5p.get("r5_composite_score")
                    _r5_cost = float(_r5p.get("r5_cost_usd", 0.0) or 0.0)
                elif isinstance(_r5p, Exception):
                    logger.debug(f"[{node_name}] soft-reg R5 judge failed: {_r5p}")
                _a_pen = _softreg.alignment_penalty(_composite)
                _w_a_eff = _sr_w_a
                _r5_extra["_soft_reg_r5_composite"] = (
                    round(_composite, 4) if _composite is not None else None
                )
                _r5_extra["_soft_reg_r5_cost_usd"] = round(_r5_cost, 6)
            _res = _softreg.evaluate_candidate(
                _expr or "", _dists[_li], float(probas[_li]),
                w_complexity=_sr_w_c, w_originality=_sr_w_o, w_alignment=_w_a_eff,
                alignment_pen=_a_pen, c0=_sr_c0, cmax=_sr_cmax,
                lam=_sr_lambda, mode=_sr_mode,
            )
            if _sr_mode == "soft":
                _sr_adj[_li] = _res.p_pass_adjusted
            if not isinstance(_a.metrics, dict):
                _a.metrics = {}
            _a.metrics.update(_res.to_metrics_dict())
            _a.metrics.update(_r5_extra)

        if _sr_mode == "soft":
            # Re-derive keep/skip from the down-weighted probas (threshold
            # unchanged). Soft-reg only lowers P(PASS), so it can only skip
            # MORE, never resurrect a classifier-skipped candidate.
            probas = _sr_adj
            keep_local = [i for i, p in enumerate(probas) if p >= threshold]
            skip_local = [i for i, p in enumerate(probas) if p < threshold]
            logger.info(
                f"[{node_name}] soft-reg mode=soft lambda={_sr_lambda} "
                f"keep={len(keep_local)} skip={len(skip_local)}"
            )
    except Exception as _sr_e:  # noqa: BLE001
        logger.warning(f"[{node_name}] soft regularizer failed (non-fatal): {_sr_e}")
    return keep_local, skip_local, probas


async def node_simulate(
    state: MiningState,
    brain: BrainAdapter,
    config: RunnableConfig = None
) -> Dict:
    """
    Batch simulate ALL valid alphas on BRAIN platform.
    
    Enhanced with DB-level deduplication:
    - Check expression hash against existing alphas before simulation
    - Skip already-simulated expressions to save API calls
    
    Input State:
        - pending_alphas, region, universe
    
    Output Updates:
        - pending_alphas (with simulation result)
        - trace_steps
    """
    start_time = time.time()
    node_name = "SIMULATE"
    
    trace_service = config.get("configurable", {}).get("trace_service") if config else None
    
    # Filter valid alphas that haven't been simulated
    valid_indices = [
        i for i, a in enumerate(state.pending_alphas)
        if a.is_valid and not a.simulation_success
    ]

    # Observability (2026-05-20): record a SIMULATE trace step on EVERY exit
    # — including the silent early returns below (no-valid / all-deduped /
    # all pre-sim-filtered). BRAIN sims + dedup + the pre-simulate filter were
    # never traced, so during the multi-minute sim gap the trace/UI looked
    # frozen at VALIDATE. record_trace persists immediately, so each exit now
    # shows why 0 alphas reached BRAIN (the success path records the outcome).
    async def _sim_exit_trace(reason: str, **extra) -> Dict:
        return await record_trace(
            state, trace_service, node_name,
            {"pending": len(state.pending_alphas), "valid_to_simulate": len(valid_indices)},
            {"simulated": 0, "skip_reason": reason, **extra},
            0, "SUCCESS",
        )

    if not valid_indices:
        logger.warning(f"[{node_name}] No valid alphas to simulate")
        return await _sim_exit_trace("no_valid_alphas_after_validation")
    
    # DB-level deduplication check
    db_duplicates = 0
    indices_to_simulate = []
    # Layer 1 Anti-collapse (2026-05-11): collect skeletons of dropped
    # candidates; strategy_select reads state.recent_dedup_skeletons next
    # round so the LLM stops re-generating the same narrow neighborhood.
    dedup_skel_buf: list[str] = []

    # V-27.81: in-flight simulate dedup. Redis slots claimed below are
    # released after the batch completes (and on every early return); the
    # 900s TTL is the crash safety net. Imports kept out of the try block
    # so _release_claimed_slots stays callable even if DB dedup raises.
    claimed_sim_slots: list = []
    _dedup_lock_on = getattr(settings, "SIMULATE_DEDUP_LOCK_ENABLED", True)
    from backend.alpha_semantic_validator import compute_expression_hash
    from backend.tasks.redis_pool import (
        claim_simulate_slot,
        release_simulate_slot,
    )

    try:
        from backend.database import AsyncSessionLocal
        from backend.selection_strategy import filter_unsimulated_expressions
        from backend.knowledge_extraction import expression_to_skeleton as _expr_to_skel

        expressions_to_check = [state.pending_alphas[i].expression for i in valid_indices]

        async with AsyncSessionLocal() as db:
            new_exprs, dup_exprs = await filter_unsimulated_expressions(
                db, expressions_to_check, state.region, state.universe
            )

        new_expr_set = set(new_exprs)
        for idx in valid_indices:
            expr = state.pending_alphas[idx].expression
            if expr not in new_expr_set:
                db_duplicates += 1
                state.pending_alphas[idx].simulation_error = "DB duplicate: already simulated"
                state.pending_alphas[idx].is_simulated = True
                state.pending_alphas[idx].simulation_success = False
                # Bug A follow-up (2026-05-20): a local-DB dedup skip never
                # consumed a fresh BRAIN simulate slot (the prior simulation
                # did, when the expression was first seen). Mark it so the
                # quota guard excludes it + persistence labels DEDUP_SKIP not
                # SIMULATION_ERROR. _skip_kind distinguishes it from the
                # pre-simulate/Q10 classifier skips (which use PRESIM_SKIP).
                _dup_a = state.pending_alphas[idx]
                if not isinstance(_dup_a.metrics, dict):
                    _dup_a.metrics = {}
                _dup_a.metrics["_pre_brain_skip"] = True
                _dup_a.metrics["_skip_kind"] = "dedup"
                # Capture skeleton for next-round LLM blacklist
                try:
                    sk = _expr_to_skel(expr or "", max_depth=3)
                    if sk:
                        dedup_skel_buf.append(sk)
                except Exception:
                    pass
                continue
            # V-27.81: in-flight dedup — another worker may already be
            # simulating this same (hash, region, universe) right now.
            # Claim a Redis slot; if we can't, treat it exactly like a DB
            # duplicate so we don't burn a BRAIN slot on the concurrent
            # re-simulate.
            if _dedup_lock_on and expr:
                _h = compute_expression_hash(expr)
                _slot_token = claim_simulate_slot(_h, state.region, state.universe)
                if not _slot_token:
                    db_duplicates += 1
                    state.pending_alphas[idx].simulation_error = (
                        "in-flight duplicate: concurrent simulate"
                    )
                    state.pending_alphas[idx].is_simulated = True
                    state.pending_alphas[idx].simulation_success = False
                    try:
                        sk = _expr_to_skel(expr or "", max_depth=3)
                        if sk:
                            dedup_skel_buf.append(sk)
                    except Exception:
                        pass
                    continue
                claimed_sim_slots.append(
                    (_h, state.region, state.universe, _slot_token)
                )
            indices_to_simulate.append(idx)

        logger.info(
            f"[{node_name}] DB dedup: {db_duplicates} duplicates skipped, "
            f"{len(indices_to_simulate)} to simulate"
        )

    except Exception as e:
        logger.warning(f"[{node_name}] DB dedup check failed, proceeding with all: {e}")
        indices_to_simulate = valid_indices
    
    # Helper to merge dedup_skel_buf into state.recent_dedup_skeletons,
    # de-duped, last-N retained. Cap from settings.DEDUP_BLACKLIST_CAP.
    #
    # V-26.72 (2026-05-13): pre-fix used `dict.fromkeys(merged)` which
    # preserves the FIRST occurrence of each key. When a skeleton already
    # in the older `state.recent_dedup_skeletons` reappeared in the new
    # `dedup_skel_buf`, the position stayed at the old (front) location.
    # After the `[-cap:]` slice the recurring skeleton fell out of the
    # window first, even though it was just hit again — defeating the
    # "freshest N" intent. `ordered.pop` then re-insert implements
    # move-to-end on duplicate so recurring skeletons stay protected.
    def _merge_dedup_skels() -> list[str]:
        cap = int(getattr(settings, "DEDUP_BLACKLIST_CAP", 50) or 50)
        ordered: Dict[str, None] = {}
        for s in (state.recent_dedup_skeletons or []):
            ordered[s] = None
        for s in dedup_skel_buf:
            ordered.pop(s, None)
            ordered[s] = None
        return list(ordered.keys())[-cap:]

    def _release_claimed_slots() -> None:
        # V-27.81: release every in-flight simulate slot this node claimed —
        # called after the batch completes and before every early return.
        # The 900s TTL is the crash safety net if this is somehow missed.
        for _slot in claimed_sim_slots:
            release_simulate_slot(*_slot)
        claimed_sim_slots.clear()

    if not indices_to_simulate:
        logger.warning(f"[{node_name}] All expressions already in DB")
        return {
            "pending_alphas": state.pending_alphas,
            "recent_dedup_skeletons": _merge_dedup_skels(),
            **(await _sim_exit_trace("all_already_in_db", db_duplicates=db_duplicates)),
        }

    # Pre-simulate self-corr check (2026-05-09; V-26.77 follow-up #4
    # 2026-05-14): drop candidates that are near-certain duplicates of an
    # already-submitted alpha so BRAIN sim quota isn't burnt on a
    # submission-time SELF_CORRELATION FAIL. Two-factor match required —
    # skeleton + fields set + numerics within ±20% — because the
    # skeleton-only check over-matches (e.g. `group_rank(divide(FIELD,
    # FIELD), FIELD)` collapses every two-field ratio into one bucket
    # regardless of which financial dimensions they encode). Cache loaded
    # from backend/data/correlation_cache/submitted_portfolio_{region}.json.
    try:
        from backend.agents.seed_pool.portfolio_skeletons import (
            find_portfolio_match,
            get_portfolio_skeleton_index,
        )
        from backend.knowledge_extraction import expression_to_skeleton
        portfolio_index = get_portfolio_skeleton_index(state.region)
        if portfolio_index:
            keep_after_skel: list[int] = []
            skel_dups = 0
            for idx in indices_to_simulate:
                expr = state.pending_alphas[idx].expression or ""
                try:
                    sk = expression_to_skeleton(expr, max_depth=3)
                except Exception:
                    sk = None
                matched_aid = (
                    find_portfolio_match(expr, sk, portfolio_index)
                    if sk
                    else None
                )
                if matched_aid:
                    skel_dups += 1
                    state.pending_alphas[idx].simulation_error = (
                        f"portfolio near-duplicate (self-corr risk) of "
                        f"{matched_aid}: skeleton+fields+numerics match"
                    )
                    state.pending_alphas[idx].is_simulated = True
                    state.pending_alphas[idx].simulation_success = False
                    # Layer 1 Anti-collapse: feed back to LLM next round
                    dedup_skel_buf.append(sk)
                else:
                    keep_after_skel.append(idx)
            if skel_dups:
                logger.info(
                    f"[{node_name}] portfolio two-factor dedup: {skel_dups} "
                    f"candidates were near-duplicates (saved BRAIN sims), "
                    f"{len(keep_after_skel)} remain"
                )
            indices_to_simulate = keep_after_skel
            if not indices_to_simulate:
                logger.warning(f"[{node_name}] All candidates dropped by portfolio dedup")
                _release_claimed_slots()
                return {
                    "pending_alphas": state.pending_alphas,
                    "recent_dedup_skeletons": _merge_dedup_skels(),
                    **(await _sim_exit_trace("all_portfolio_deduped", portfolio_dups=skel_dups)),
                }
    except Exception as e:
        logger.warning(f"[{node_name}] portfolio dedup failed, proceeding: {e}")

    # Phase 3 Q10 PR2a (2026-05-18): pyqlib local pre-screen — Multi-Fidelity
    # Layer 0. Plan ~/.claude/plans/phase3-q10-pyqlib-prescreen-2026-05-18.md
    # §5 + §12: Q10 stacks BEFORE pre_simulate_filter (which is BEFORE R9 cache
    # and BEFORE BRAIN call) so a rejected alpha saves R9 lookup + BRAIN sim +
    # cache write. Asymmetric-cost rationale: Q10 reject is FREE, R9 cache-hit
    # doesn't save Q10 either way, so Q10-first is strictly better for new
    # expressions.
    #
    # Three modes (QLIB_PRESCREEN_MODE):
    #   shadow → log only, BRAIN proceeds
    #   soft   → log + alpha.metrics["_qlib_prescreen_warned"]=True, BRAIN proceeds
    #   hard   → log + skip BRAIN (alpha marked simulation_success=False)
    #
    # Soft-fail: any per-alpha exception logs warn + continues (never blocks
    # the round). Mode is read ONCE at entry to avoid mid-batch flip race
    # ([V1.2-A2-3] in plan).
    if getattr(settings, "ENABLE_QLIB_PRESCREEN", False) and indices_to_simulate:
        try:
            from backend.qlib_prescreen import prescreen_alpha
            _q10_mode = (getattr(settings, "QLIB_PRESCREEN_MODE", "shadow") or "shadow").lower()
            _q10_rows = []  # collect log rows; batch INSERT after loop
            _q10_rejects = []  # original indices to drop when hard mode
            _q10_task_id = getattr(state, "task_id", None)

            for _q10_local, idx in enumerate(list(indices_to_simulate)):
                a = state.pending_alphas[idx]
                try:
                    pres = await prescreen_alpha(
                        a.expression, region=state.region,
                        universe=getattr(state, "universe", "TOP3000"),
                        mode=_q10_mode,
                    )
                except Exception as _pre_ex:
                    logger.warning(f"[{node_name}] Q10 prescreen call raised for idx={idx}: {_pre_ex}")
                    continue
                # In soft mode, stamp the warning metric
                if _q10_mode == "soft" and pres.verdict == "reject":
                    _m = dict(a.metrics or {})
                    _m["_qlib_prescreen_warned"] = True
                    _m["_qlib_prescreen_sharpe"] = pres.local_sharpe
                    _m["_qlib_prescreen_ic"] = pres.local_ic
                    a.metrics = _m
                # In hard mode, drop the index so BRAIN never sees it
                if _q10_mode == "hard" and pres.verdict == "reject":
                    _q10_rejects.append(idx)
                    a.simulation_error = (
                        f"Q10 pre-screen reject: {pres.reject_reason or ''}"
                    )
                    a.is_simulated = True
                    a.simulation_success = False
                    # Bug A fix (2026-05-20): Q10 hard reject is also a pre-BRAIN
                    # skip — no BRAIN slot consumed. Mark so quota guard excludes
                    # it + persistence labels error_type=PRESIM_SKIP.
                    if not isinstance(a.metrics, dict):
                        a.metrics = {}
                    a.metrics["_pre_brain_skip"] = True
                # Build log row (all modes)
                _q10_rows.append({
                    "task_id": _q10_task_id,
                    "alpha_candidate_idx": idx,
                    "brain_expression": pres.brain_expression,
                    "expression_hash": hashlib.sha256(
                        (pres.brain_expression or "").encode("utf-8")
                    ).hexdigest()[:64],
                    "qlib_expression": pres.qlib_expression,
                    "region": pres.region, "universe": pres.universe,
                    "verdict": pres.verdict,
                    "reject_reason": pres.reject_reason,
                    "skip_reason": pres.skip_reason,
                    "translation_error": pres.translation_error,
                    "local_sharpe": pres.local_sharpe,
                    "local_ic": pres.local_ic,
                    "engine_kind": pres.engine_kind,
                    "elapsed_ms": pres.elapsed_ms,
                    "mode_at_call": pres.mode_at_call,
                })

            # In hard mode reduce indices_to_simulate
            if _q10_rejects:
                rejects_set = set(_q10_rejects)
                indices_to_simulate = [i for i in indices_to_simulate if i not in rejects_set]
                logger.info(
                    f"[{node_name}] Q10 hard mode skipped={len(_q10_rejects)} "
                    f"keep={len(indices_to_simulate)}"
                )

            # Batch-INSERT log rows on dedicated session per plan §6.4 — soft-fail
            if _q10_rows:
                try:
                    from backend.database import AsyncSessionLocal as _Q10_SessionLocal
                    from backend.models.qlib_prescreen_log import QlibPrescreenLog
                    async with _Q10_SessionLocal() as _q10_db:
                        for r in _q10_rows:
                            _q10_db.add(QlibPrescreenLog(**r))
                        await _q10_db.commit()
                    logger.info(
                        f"[{node_name}] Q10 wrote {len(_q10_rows)} prescreen_log rows "
                        f"(mode={_q10_mode})"
                    )
                except Exception as _q10_db_ex:
                    logger.warning(
                        f"[{node_name}] Q10 log write failed (round unaffected): {_q10_db_ex}"
                    )
        except Exception as _q10_e:
            logger.warning(f"[{node_name}] Q10 prescreen block failed (proceed full BRAIN): {_q10_e}")

    # Plan v5+ #3 (2026-05-07): pre-simulate skeleton classifier filter.
    # Predict P(PASS) per candidate and skip very-likely-fails BEFORE
    # sending to BRAIN simulate. Threshold 0.10 keeps ≥98% PASS recall.
    try:
        from backend.agents.services.pre_simulate_filter import filter_candidates
        threshold = float(getattr(settings, "PRE_SIMULATE_FILTER_THRESHOLD", 0.05))
        cand_exprs = [state.pending_alphas[i].expression for i in indices_to_simulate]
        keep_local, skip_local, probas = filter_candidates(
            cand_exprs, threshold=threshold,
        )

        # Soft regularizer (P1 complexity+originality, P2 R5 alignment); see
        # _apply_soft_regularizer. Returns (keep, skip, probas) unchanged when
        # mode=off / on any error (byte-for-byte the filter_candidates result).
        keep_local, skip_local, probas = await _apply_soft_regularizer(
            state, indices_to_simulate, cand_exprs, probas,
            keep_local, skip_local, threshold, node_name=node_name,
        )

        if skip_local:
            # Translate skip_local positions back to original indices_to_simulate
            pre_sim_skipped: list = []
            for local_idx in skip_local:
                orig_idx = indices_to_simulate[local_idx]
                p_pass = probas[local_idx]
                pre_sim_skipped.append(orig_idx)
                a = state.pending_alphas[orig_idx]
                a.simulation_error = (
                    f"pre-simulate filter skip: P(PASS)={p_pass:.3f} < {threshold}"
                )
                a.is_simulated = True
                a.simulation_success = False
                # Bug A fix (2026-05-20): mark as a pre-BRAIN skip. These never
                # consumed a BRAIN simulate slot, so the quota guard must NOT
                # count them (it inflated the daily-quota denominator ~20% →
                # premature session pause). persistence.py labels them
                # error_type='PRESIM_SKIP' instead of SIMULATION_ERROR.
                if not isinstance(a.metrics, dict):
                    a.metrics = {}
                a.metrics["_pre_brain_skip"] = True
            # Reduce indices_to_simulate to keepers only
            indices_to_simulate = [
                indices_to_simulate[i] for i in keep_local
            ]
            logger.info(
                f"[{node_name}] pre-simulate filter: skipped={len(pre_sim_skipped)} "
                f"keep={len(indices_to_simulate)} threshold={threshold}"
            )
    except Exception as _filter_e:
        logger.warning(
            f"[{node_name}] pre-simulate filter failed (proceed with all): {_filter_e}"
        )

    if not indices_to_simulate:
        logger.warning(
            f"[{node_name}] All expressions filtered by pre-simulate classifier"
        )
        _release_claimed_slots()
        return {
            "pending_alphas": state.pending_alphas,
            "recent_dedup_skeletons": _merge_dedup_skels(),
            **(await _sim_exit_trace("all_pre_simulate_filtered")),
        }

    logger.info(f"[{node_name}] Starting batch simulation | count={len(indices_to_simulate)} region={state.region}")
    
    expressions = [state.pending_alphas[i].expression for i in indices_to_simulate]
    
    _debug_log("E", "nodes.py:simulate:expressions", "Expressions to simulate", {
        "count": len(expressions),
        "expressions": [e[:150] for e in expressions],
        "region": state.region,
        "universe": state.universe
    })
    
    # A1: smart simulation settings — per-expression settings choice based on
    # structural form (group_neutralize → neut=NONE, trade_when → decay=0,
    # etc.) and field category. Bucket expressions by their chosen settings
    # tuple and call simulate_batch per bucket; results are merged back to
    # original index order.
    # (Retired ENABLE_SMART_SIM_SETTINGS flag 2026-05-19 — hard-wired ON;
    #  legacy single-batch fallback branch deleted.)
    smart_settings_per_idx: Dict[int, Dict] = {}  # local_index → settings dict
    smart_reasons_per_idx: Dict[int, str] = {}

    from backend.sim_settings import settings_reason, smart_simulation_settings

    SETTINGS_KEYS = ("region", "universe", "delay", "decay", "neutralization", "truncation", "test_period")
    buckets: Dict[Tuple, List[int]] = {}
    for local_i, idx in enumerate(indices_to_simulate):
        expr = state.pending_alphas[idx].expression
        # delay-0 native mining (②/B): a FLAT session with task.config["delay"]==0
        # threads state.delay here so the sim runs at delay-0. delay-1 is the
        # smart-settings default → no override when delay==1 (path unchanged).
        _delay = getattr(state, "delay", 1)
        smart = smart_simulation_settings(
            expr,
            region=state.region,
            universe=state.universe,
            # P3-Brain: 从 task-startup snapshot 传 test_period(plan §8.4)
            # 避免 Consultant 切换中途 simulate 不同 alpha 用不同 test_period。
            test_period=getattr(state, "effective_default_test_period", None),
            overrides=({"delay": _delay} if _delay != 1 else None),
        )
        smart_settings_per_idx[local_i] = smart
        smart_reasons_per_idx[local_i] = settings_reason(expr)
        key = tuple(smart.get(k) for k in SETTINGS_KEYS)
        buckets.setdefault(key, []).append(local_i)

    logger.info(
        f"[{node_name}] smart-settings: {len(buckets)} bucket(s) for "
        f"{len(indices_to_simulate)} expressions"
    )

    results = [None] * len(indices_to_simulate)
    for settings_key, local_indices in buckets.items():
        bucket_kwargs = dict(zip(SETTINGS_KEYS, settings_key))
        bucket_exprs = [expressions[li] for li in local_indices]
        # V-26.66 (2026-05-13): one batch-level retry with a short
        # backoff before declaring the whole bucket failed. Most
        # bucket-wide failures are transient (BRAIN auth blip, sim
        # slot starvation, transport timeout). Retrying once recovers
        # ~70-80% of these without doubling worst-case latency.
        bucket_results = None
        for _attempt in range(2):
            try:
                # R9 (Phase 3, 2026-05-18): cached BRAIN sim wrapper when
                # ENABLE_SIMULATION_CACHE ON; OFF or import error → direct
                # brain.simulate_batch (byte-equivalent legacy). Soft-fall.
                if getattr(settings, "ENABLE_SIMULATION_CACHE", False):
                    try:
                        from backend.agents.sim_cache import cached_simulate_batch
                        from backend.database import AsyncSessionLocal as _R9_SessionLocal
                        async with _R9_SessionLocal() as _r9_db:
                            bucket_results = await cached_simulate_batch(
                                _r9_db, brain,
                                expressions=bucket_exprs,
                                **bucket_kwargs,
                            )
                    except Exception as _r9_e:
                        logger.warning(
                            f"[{node_name}] R9 cached_simulate_batch failed "
                            f"(falling back to direct BRAIN): {_r9_e}"
                        )
                        bucket_results = await brain.simulate_batch(
                            expressions=bucket_exprs, **bucket_kwargs,
                        )
                else:
                    bucket_results = await brain.simulate_batch(
                        expressions=bucket_exprs,
                        **bucket_kwargs,
                    )
                break
            except Exception as e:
                if _attempt == 0:
                    logger.warning(
                        f"[{node_name}] V-26.66 bucket sim error ({settings_key}) — "
                        f"retrying once: {e}"
                    )
                    await asyncio.sleep(2.0)
                    continue
                logger.error(
                    f"[{node_name}] V-26.66 bucket sim failed after retry "
                    f"({settings_key}): {e}"
                )
                bucket_results = [
                    {"success": False, "error": f"sim_batch_fail_after_retry: {e}"}
                    for _ in bucket_exprs
                ]
        # V-26.71 (2026-05-13): alert when BRAIN returns fewer results
        # than requested. Pre-fix silently filled the missing tail with
        # `{success: False, error: "Missing"}` so the caller never
        # noticed the asymmetry. Now we logger.error so the operator
        # can investigate (truncated BRAIN response, bucket payload
        # encoding drift, etc.) — the silent fill is preserved so the
        # workflow still makes forward progress.
        if len(bucket_results) < len(local_indices):
            logger.error(
                f"[{node_name}] V-26.71 BRAIN returned "
                f"{len(bucket_results)} results for {len(local_indices)} "
                f"expressions (bucket={settings_key}); padding tail with "
                f"Missing failures"
            )
        for j, li in enumerate(local_indices):
            results[li] = bucket_results[j] if j < len(bucket_results) else {"success": False, "error": "Missing"}
    
    duration_ms = int((time.time() - start_time) * 1000)
    
    # Update alphas
    updated_alphas = state.pending_alphas.copy()
    success_count = 0

    for i, idx in enumerate(indices_to_simulate):
        res = results[i] if i < len(results) else {"success": False, "error": "Missing result"}
        
        current = updated_alphas[idx]
        updated = current.model_copy()
        
        updated.simulation_success = res.get("success", False)
        updated.alpha_id = res.get("alpha_id")
        # Replace metrics with the BRAIN result, but carry the allow-listed
        # pre-simulate annotations across (see _carry_pre_sim_metrics) — else
        # soft-reg/Q10/G10/G3-v2/validation stamps are dropped before persist.
        updated.metrics = _carry_pre_sim_metrics(res.get("metrics", {}) or {}, current.metrics)
        updated.simulation_error = res.get("error")

        # V-27.61: a retryable failure (429 / slot-acquire timeout / stale
        # slot counter — brain_adapter.simulate_alpha returns retryable=True
        # for these) is TRANSIENT, not a verdict on the alpha. Keep
        # is_simulated=False and tag the metrics so node_evaluate holds it at
        # PENDING instead of burying it as a terminal FAIL that pollutes the
        # KB + failure_feedback_queue. (Full re-enqueue carrying the
        # expression into a later round needs the mining main loop's
        # cooperation — tracked as backlog, see V-27 RCA.)
        if res.get("retryable"):
            updated.is_simulated = False
            updated.metrics = {
                "_sim_retryable": True,
                "_retry_after_sec": res.get("retry_after_sec"),
            }
        else:
            updated.is_simulated = True

        # A1: stamp the resolved sim-settings into metrics for audit / KB insight,
        # and so node_evaluate's signal-vs-control dual-run can re-simulate the
        # control with the SAME settings — otherwise Δsharpe mixes signal-core
        # difference with sim-settings difference and the attribution is invalid.
        # 2026-05-20 fix: `smart_enabled` was the retired ENABLE_SMART_SIM_SETTINGS
        # flag (hard-wired ON since 2026-05-19) — its definition was deleted with
        # the fallback branch but this reference was left dangling → NameError on
        # every sim-result iteration, silently dropping the _sim_settings stamp.
        if i in smart_settings_per_idx:
            updated.metrics = {
                **updated.metrics,
                "_sim_settings": smart_settings_per_idx[i],
                "_sim_settings_reason": smart_reasons_per_idx.get(i, ""),
            }
        else:
            updated.metrics = {
                **updated.metrics,
                "_sim_settings": {
                    "region": state.region,
                    "universe": state.universe,
                    # delay-0: use the task delay (getattr default handles delay=0,
                    # which is falsy — never use `or SIM_DEFAULT_DELAY` here).
                    "delay": getattr(state, "delay", _v16_settings.SIM_DEFAULT_DELAY),
                    "decay": _v16_settings.SIM_DEFAULT_DECAY,
                    "neutralization": _v16_settings.SIM_DEFAULT_NEUTRALIZATION,
                },
            }

        if updated.simulation_success:
            success_count += 1

        updated_alphas[idx] = updated

    # V-27.81: batch done — release every in-flight simulate slot we claimed
    # (covers both simulated and post-claim-filtered expressions).
    _release_claimed_slots()

    failed_errors = [
        {"expr": expressions[i][:80], "error": results[i].get("error", "unknown")[:200]}
        for i in range(len(results)) if not results[i].get("success")
    ]
    
    _debug_log("E", "nodes.py:simulate:result", "Simulation complete", {
        "total_to_simulate": len(indices_to_simulate),
        "success": success_count,
        "failed": len(indices_to_simulate) - success_count,
        "db_duplicates_skipped": db_duplicates,
        "duration_ms": duration_ms,
        "success_rate": round(success_count / max(1, len(indices_to_simulate)) * 100, 1),
        "failed_errors": failed_errors[:5]
    })
    
    logger.info(f"[{node_name}] Complete | success={success_count}/{len(indices_to_simulate)} db_skipped={db_duplicates}")
    
    # Experiment tracking
    if EXPERIMENT_TRACKING_ENABLED:
        exp = get_current_experiment()
        if exp:
            exp.metrics.increment("simulation_count", len(indices_to_simulate))
            exp.metrics.record("dedup_skip_rate",
                (db_duplicates / (len(indices_to_simulate) + db_duplicates) * 100)
                if (len(indices_to_simulate) + db_duplicates) > 0 else 0,
                tags={"node": node_name, "region": state.region}
            )
            exp.metrics.record("simulation_success_rate",
                (success_count / len(indices_to_simulate) * 100) if len(indices_to_simulate) > 0 else 0,
                tags={"node": node_name}
            )
    
    trace_update = await record_trace(
        state, trace_service, node_name,
        {
            "batch_size": len(indices_to_simulate),
            "db_duplicates_skipped": db_duplicates,
            "expressions": [e[:50] for e in expressions[:10]]
        },
        {
            "success_count": success_count,
            "simulated_count": len(indices_to_simulate),
            "db_duplicates": db_duplicates,
            "results": [{"id": r.get("alpha_id"), "err": r.get("error")} for r in results[:20]]
        },
        duration_ms,
        "SUCCESS" if success_count > 0 else "PARTIAL_FAILURE"
    )
    
    return {
        "pending_alphas": updated_alphas,
        "recent_dedup_skeletons": _merge_dedup_skels(),
        **trace_update
    }


# =============================================================================
# NODE: Evaluate Quality
# =============================================================================

async def node_evaluate(
    state: MiningState,
    brain: BrainAdapter = None,
    config: RunnableConfig = None
) -> Dict:
    """
    Evaluate alpha quality using multi-objective scoring.
    
    Enhanced with:
    - Two-stage correlation checking
    - BRAIN platform official checks integration (checks 数组)
    - Pyramid multiplier consideration for prioritization
    
    Input State:
        - pending_alphas (with simulation results)
    
    Output Updates:
        - pending_alphas (with quality_status and score)
        - trace_steps
    """
    start_time = time.time()
    node_name = "EVALUATE"

    trace_service = config.get("configurable", {}).get("trace_service") if config else None

    # V-27.63 / V-27.62: state.pending_alphas.copy() is a SHALLOW list copy —
    # each element is still the SAME object as state.pending_alphas[i], so the
    # quality_status writes (FAIL/PASS/PASS_PROVISIONAL/OPTIMIZE) AND the
    # metrics-dict writes (_v16_suspicion_flags / _brain_actionable_fails on
    # the near_pass path) below punch straight through into the LangGraph
    # input state, corrupting it for replay / interrupt-resume. node_simulate
    # already guards this with model_copy(); node_evaluate mutates both
    # top-level fields and the nested metrics dict, so it needs a DEEP copy.
    updated_alphas = [a.model_copy(deep=True) for a in state.pending_alphas]
    corr_checks_performed = 0
    corr_checks_skipped = 0

    # W0.5: local PnL-matrix self-correlation with BRAIN-API fallback.
    # Shared across all alphas in this round to amortise the cache load.
    correlation_service = CorrelationService(brain) if brain is not None else None
    
    logger.info(f"[{node_name}] Starting two-stage evaluation | count={len(state.pending_alphas)}")

    # Flat evaluation thresholds post tier-system removal (2026-05-18).
    # BRAIN role-switch (P3-Brain): pass task-startup snapshot to keep running
    # tasks consistent across Consultant flag toggles (avoids re-judging
    # mid-round alphas with new sharpe bar).
    # delay-aware (2026-05-28): MiningState.delay is injected from
    # task.config["delay"] at workflow start; delay=0 yields the stricter
    # BRAIN-aligned band (sharpe>=2.0/fit>=1.3/sub-univ>=0.81).
    tier_cfg = _eval_thresholds(
        delay=int(getattr(state, "delay", 1) or 1),
        sharpe_submit_min_override=getattr(state, "effective_sharpe_submit_min", None),
    )

    # P2-C (2026-05-16): regime-aware threshold adjustment. The mining_agent
    # injection block puts ``regime`` + ``style_preset`` onto strategy BEFORE
    # the workflow runs; we read the regime out of config["configurable"][
    # "strategy"] (a dict from EvolutionStrategy.to_dict) and, if the
    # ``ENABLE_REGIME_AWARE_THRESHOLDS`` flag is on AND a regime is present,
    # scale the tier_cfg in-place. MF5 thread-through: ``_regime_for_eval``
    # carries forward into ``_EvalCtx`` so the per-alpha stamp downstream
    # uses the same value (not a re-derived one).
    #
    # MF6 invariant verified by apply_regime_multipliers: ``score_optimize``
    # is NEVER scaled — only ``score_pass`` shifts with regime.
    #
    # S2: the ``_regime_at_eval`` stamp on alpha.metrics is gated by
    # ``_regime_for_eval`` being non-None (i.e. strategy.regime was set),
    # which only happens when at least one of the two effect flags
    # (AWARE / STYLE) was True in mining_agent. So even with AWARE=False
    # + STYLE=True, the stamp is written; the threshold multipliers
    # below are NOT applied (only the prompt block downstream).
    _regime_for_eval: Optional[str] = None
    try:
        _strategy_blob = (
            (config.get("configurable", {}) or {}).get("strategy", {})
            if config else {}
        )
        if isinstance(_strategy_blob, dict):
            _regime_for_eval = _strategy_blob.get("regime")
    except Exception:
        _regime_for_eval = None

    if (
        getattr(settings, "ENABLE_REGIME_AWARE_THRESHOLDS", False)
        and _regime_for_eval
    ):
        try:
            from backend.regime_classifier import (  # lazy
                apply_regime_multipliers as _apply_regime_multipliers,
            )
            _old_sharpe = tier_cfg.get("sharpe_min")
            adjusted_tier_cfg = _apply_regime_multipliers(
                tier_cfg, _regime_for_eval,
            )
            logger.info(
                f"[{node_name}] P2-C regime gate | regime="
                f"{_regime_for_eval} sharpe: "
                f"{_old_sharpe}→{adjusted_tier_cfg.get('sharpe_min')}"
            )
            tier_cfg = adjusted_tier_cfg
        except Exception as _p2c_ex:
            logger.warning(
                f"[{node_name}] P2-C regime adjust failed "
                f"(non-fatal): {_p2c_ex}"
            )

    # Feature 1 (2026-05-24): the 11 numeric verdict thresholds are now unpacked
    # by the shared _unpack_eval_thresholds (single source of truth with sync).
    # Behaviour is unchanged — this is a verbatim move of the prior inline
    # mapping. T1: check_self_corr/check_concentrated stay sourced from tier_cfg,
    # corr_check_threshold from settings — they are NOT verdict thresholds.
    _th = _unpack_eval_thresholds(tier_cfg)
    sharpe_min = _th["sharpe_min"]
    fitness_min = _th["fitness_min"]
    turnover_min = _th["turnover_min"]
    turnover_max = _th["turnover_max"]
    max_correlation = _th["max_correlation"]
    prov_sharpe_min = _th["prov_sharpe_min"]
    prov_fitness_min = _th["prov_fitness_min"]
    prov_turnover_min = _th["prov_turnover_min"]
    prov_turnover_max = _th["prov_turnover_max"]
    score_pass_threshold = _th["score_pass_threshold"]
    score_optimize_threshold = _th["score_optimize_threshold"]
    check_self_corr = tier_cfg["check_self_corr"]
    check_concentrated = tier_cfg["check_concentrated"]
    corr_check_threshold = getattr(settings, 'CORR_CHECK_THRESHOLD', 0.5)
    logger.info(
        f"[{node_name}] flat-eval sharpe>={sharpe_min} fitness>={fitness_min} "
        f"turnover [{turnover_min}, {turnover_max}] check_self_corr={check_self_corr} "
        f"check_concentrated={check_concentrated}"
    )
    
    eval_details = []
    failure_feedback_queue = []

    # P1-B: bundle context for per-alpha helper
    _ctx = _EvalCtx(
        state=state,
        brain=brain,
        correlation_service=correlation_service,
        node_name=node_name,
        sharpe_min=sharpe_min,
        fitness_min=fitness_min,
        turnover_min=turnover_min,
        turnover_max=turnover_max,
        max_correlation=max_correlation,
        check_self_corr=check_self_corr,
        check_concentrated=check_concentrated,
        prov_sharpe_min=prov_sharpe_min,
        prov_fitness_min=prov_fitness_min,
        prov_turnover_min=prov_turnover_min,
        prov_turnover_max=prov_turnover_max,
        score_pass_threshold=score_pass_threshold,
        score_optimize_threshold=score_optimize_threshold,
        corr_check_threshold=corr_check_threshold,
        eval_details=eval_details,
        failure_feedback_queue=failure_feedback_queue,
        # P2-C (2026-05-16) MF5: thread the regime label through the ctx
        # so the per-alpha stamp downstream uses the same value the
        # multiplier path saw (no re-derivation, no inconsistency window).
        regime_for_eval=_regime_for_eval,
    )
    eval_errors = 0

    for i, alpha in enumerate(updated_alphas):
        # Hard rule: anything that didn't simulate successfully cannot be PASS.
        if not alpha.is_simulated or not alpha.simulation_success:
            if isinstance(alpha.metrics, dict) and alpha.metrics.get("_sim_retryable"):
                # V-27.61 + P1-B: stamp PENDING explicitly so the post-loop
                # tally classifies retryable as PENDING, not FAIL.
                alpha.quality_status = "PENDING"
                logger.info(
                    f"[{node_name}] retryable sim failure held at PENDING "
                    f"(not FAIL) | expr={(alpha.expression or '')[:60]}"
                )
                updated_alphas[i] = alpha
                continue
            alpha.quality_status = "FAIL"
            updated_alphas[i] = alpha
            continue

        try:
            _single_result = await _evaluate_single_alpha(alpha, _ctx)
            if _single_result.corr_check_performed:
                corr_checks_performed += 1
            elif _single_result.corr_check_skipped_reason:
                corr_checks_skipped += 1
        except Exception as _eval_exc:
            # P1-B fear-score fallback: one bad alpha does NOT crash the batch
            eval_errors += 1
            alpha.quality_status = "FAIL"
            prior_metrics = alpha.metrics or {}
            existing_flags = prior_metrics.get("_metrics_fallback_flags")
            if not isinstance(existing_flags, list):
                existing_flags = []
            alpha.metrics = {
                **prior_metrics,
                "_eval_error": f"{type(_eval_exc).__name__}: {_eval_exc}"[:500],
                "_metrics_fallback_flags": existing_flags + ["__eval_exception__"],
            }
            _ctx.eval_details.append({
                "id": alpha.alpha_id,
                "status": "FAIL",
                "score": None,
                "sharpe": None,
                "fitness": None,
                "turnover": None,
                "corr_checked": False,
                "optimize_reason": None,
                "_eval_error": f"{type(_eval_exc).__name__}: {_eval_exc}"[:200],
            })
            logger.exception(
                f"[{node_name}] per-alpha eval crashed for "
                f"{alpha.alpha_id or (alpha.expression or '')[:60]}; marking FAIL"
            )
        updated_alphas[i] = alpha

    # PR5 — sign-flip retry. For each FAIL alpha whose |sharpe| ≥
    # T1_FLIP_RETRY_SHARPE (real signal pointing the wrong direction, not
    # statistical noise), simulate the negated expression and re-evaluate.
    # Bounded by T1_FLIP_RETRY_CAP per round.
    flip_retry_count = 0
    flip_retry_pass = 0
    flip_retry_prov = 0
    # (Retired ENABLE_T1_SIGN_FLIP_RETRY 2026-05-19 — hard-wired ON.)
    if brain is not None:
        flip_threshold = getattr(settings, "T1_FLIP_RETRY_SHARPE", 0.5)
        flip_cap = getattr(settings, "T1_FLIP_RETRY_CAP", 5)

        flip_candidates = sorted(
            [
                a for a in updated_alphas
                if a.quality_status == "FAIL"
                and a.is_simulated and a.simulation_success
                and isinstance(a.metrics, dict)
                and a.metrics.get("sharpe") is not None
                and a.metrics["sharpe"] <= -flip_threshold
                and not (a.metadata or {}).get("flipped")
            ],
            key=lambda a: a.metrics["sharpe"],  # most-negative first
        )[:flip_cap]

        if flip_candidates:
            logger.info(
                f"[{node_name}] T1 flip-retry: {len(flip_candidates)} candidates "
                f"with sharpe ≤ -{flip_threshold}"
            )

        from backend.agents.graph.state import AlphaCandidate

        # A2: flip-retry single-alpha sim → smart settings (zero bucketing cost).
        # (Retired ENABLE_SMART_SIM_SETTINGS 2026-05-19 — hard-wired ON.)

        # V-19.3 (2026-05-06): pre-dedup flipped expressions across the WHOLE
        # alphas table. Sign-flip historically bypassed node_simulate's
        # filter_unsimulated_expressions, so BRAIN was repeatedly handed
        # already-known expressions and returned existing alpha_ids that
        # collided on uq_alpha_id at INSERT (spike → task=115 dup ZY2K0nwn /
        # GrMeLOg3 with task 81/83). Now we pre-filter to save BRAIN quota
        # AND avoid the doomed INSERT.
        flip_dedup_skipped = 0
        # V-27.81: in-flight simulate dedup for flip-retry. Slots claimed in
        # the dedup loop below are released after the simulate loop.
        flip_claimed_slots: list = []
        _flip_dedup_lock_on = getattr(settings, "SIMULATE_DEDUP_LOCK_ENABLED", True)
        from backend.alpha_semantic_validator import (
            compute_expression_hash as _flip_expr_hash,
        )
        from backend.tasks.redis_pool import (
            claim_simulate_slot as _flip_claim_slot,
            release_simulate_slot as _flip_release_slot,
        )
        try:
            from backend.database import AsyncSessionLocal as _ASL
            from backend.selection_strategy import filter_unsimulated_expressions as _flt
            flipped_exprs = [f"multiply(-1, {o.expression})" for o in flip_candidates]
            async with _ASL() as _ds:
                _new_flipped, _dup_flipped = await _flt(
                    _ds, flipped_exprs, state.region, state.universe,
                )
            _new_flipped_set = set(_new_flipped)
            kept_candidates = []
            for o in flip_candidates:
                fexpr = f"multiply(-1, {o.expression})"
                if fexpr not in _new_flipped_set:
                    flip_dedup_skipped += 1
                    logger.info(
                        f"[{node_name}] V-19.3 flip-retry skip — flipped expr already "
                        f"in DB: {fexpr[:100]!r}"
                    )
                    continue
                # V-27.81: in-flight dedup — skip if another worker is
                # already simulating this flipped expression right now.
                if _flip_dedup_lock_on:
                    _fh = _flip_expr_hash(fexpr)
                    _flip_slot_token = _flip_claim_slot(
                        _fh, state.region, state.universe
                    )
                    if not _flip_slot_token:
                        flip_dedup_skipped += 1
                        logger.info(
                            f"[{node_name}] V-27.81 flip-retry skip — flipped "
                            f"expr in-flight on another worker: {fexpr[:100]!r}"
                        )
                        continue
                    flip_claimed_slots.append(
                        (_fh, state.region, state.universe, _flip_slot_token)
                    )
                kept_candidates.append(o)
            flip_candidates = kept_candidates
            if flip_dedup_skipped:
                logger.info(
                    f"[{node_name}] V-19.3 flip-retry: {flip_dedup_skipped} candidates "
                    f"pre-deduped, {len(flip_candidates)} will simulate"
                )
        except Exception as _e:
            logger.warning(
                f"[{node_name}] V-19.3 flip-retry dedup query failed, proceeding: {_e}"
            )

        # V-27.81 followup: try/finally so a raise anywhere in the simulate
        # loop (e.g. a None-metric f-string format, an evaluation-helper
        # exception) still releases every claimed in-flight slot instead of
        # leaking them to the 900s TTL.
        try:
            # opt A (2026-05-25): simulate all flip candidates CONCURRENTLY.
            # Was a serial `for ... await simulate_alpha` (it6: ~4 sims ×
            # ~2.7min ≈ 11min EVALUATE, tipping the round over the 1200s
            # timeout). Each simulate_alpha already holds a cross-process BRAIN
            # sim slot (_acquire_sim_slot, role-aware USER=3 / CONSULTANT=80),
            # so gathering is safe — the slot counter caps real concurrency and
            # never lets us hit 429 CONCURRENT_SIMULATION_LIMIT. Results keep
            # input order; post-processing (build + re-evaluate) stays serial
            # since it is local/fast (corr probe = 2.5s).
            from backend.sim_settings import smart_simulation_settings

            async def _sim_one_flip(_orig):
                _fexpr = f"multiply(-1, {_orig.expression})"
                # delay-0 native mining: the flip-retry sub-path is a SECOND sim
                # call — it must inherit the task's delay too, else a delay-0
                # round's flip sims run at delay-1 (smart-settings default) and
                # persist a delay-1 alpha into a delay-0 session. delay-1 → no
                # override (unchanged).
                _fd = getattr(state, "delay", 1)
                try:
                    _smart = smart_simulation_settings(
                        _fexpr,
                        region=state.region,
                        universe=state.universe,
                        # P3-Brain: flip-retry 同 round 内保持 test_period 一致
                        # (否则 sharpe 不可比)。从 task snapshot 传。
                        test_period=getattr(state, "effective_default_test_period", None),
                        overrides=({"delay": _fd} if _fd != 1 else None),
                    )
                    _res = await brain.simulate_alpha(expression=_fexpr, **_smart)
                    return (_orig, _fexpr, dict(_smart), _res)
                except Exception as _e:  # noqa: BLE001
                    logger.warning(f"[{node_name}] flip-retry sim failed: {_e}")
                    return (_orig, _fexpr, None, None)

            _flip_sim_results = await asyncio.gather(
                *[_sim_one_flip(_o) for _o in flip_candidates]
            )

            for orig, flipped_expr, _flip_sim_settings, sim_result in _flip_sim_results:
                if sim_result is None or not sim_result.get("success"):
                    if sim_result is not None:
                        logger.debug(f"[{node_name}] flip sim returned failure for {flipped_expr[:80]}")
                    continue

                flip_metrics = sim_result.get("metrics") or {}
                # Stamp the settings actually used so node_evaluate's dual-run can
                # replay the control with identical settings (see _sim_settings).
                flip_metrics["_sim_settings"] = _flip_sim_settings
                new_sharpe = flip_metrics.get("sharpe") or 0
                new_fitness = flip_metrics.get("fitness") or 0
                new_turnover = flip_metrics.get("turnover") or 0

                new_alpha = AlphaCandidate(
                    expression=flipped_expr,
                    hypothesis=(orig.hypothesis or "") + " (sign-flipped)",
                    explanation=(
                        f"sign-flip retry — original {orig.expression[:60]} "
                        f"had sharpe={orig.metrics.get('sharpe'):.3f}"
                    ),
                    is_valid=True,
                    is_simulated=True,
                    simulation_success=True,
                    alpha_id=sim_result.get("alpha_id"),
                    metrics=flip_metrics,
                    metadata={
                        "flipped": True,
                        "original_expression": orig.expression,
                        "original_sharpe": orig.metrics.get("sharpe"),
                        "round": getattr(orig.metadata, "get", lambda k: None)("round")
                                if not isinstance(orig.metadata, dict)
                                else (orig.metadata or {}).get("round"),
                    },
                )

                # Bug B fix (2026-05-16): route flip-retry alphas through
                # _evaluate_single_alpha so they get the same multi-tier
                # routing, line-646 metrics spread, V-16 suspicion check,
                # P2-C regime stamp (_regime_at_eval), P0 dual_run,
                # P1-A graded_score, P1-D RobustnessGate as main-loop alphas.
                # Pre-fix: this block re-implemented (and drifted from) the
                # gate logic, and skipped every P0/P1/P2 stamp — flip-retry
                # PROV alphas landed in alphas.metrics with only BRAIN raw
                # fields, breaking _regime_at_eval audit + downstream score
                # consumers.
                try:
                    _flip_single_result = await _evaluate_single_alpha(new_alpha, _ctx)
                    if _flip_single_result.corr_check_performed:
                        corr_checks_performed += 1
                    elif _flip_single_result.corr_check_skipped_reason:
                        corr_checks_skipped += 1
                except Exception as _flip_eval_ex:
                    # Mirror main-loop fear-score fallback (line 1469-1496):
                    # one bad flip-retry alpha does NOT crash the batch.
                    eval_errors += 1
                    new_alpha.quality_status = "FAIL"
                    prior_metrics = new_alpha.metrics or {}
                    existing_flags = prior_metrics.get("_metrics_fallback_flags")
                    if not isinstance(existing_flags, list):
                        existing_flags = []
                    new_alpha.metrics = {
                        **prior_metrics,
                        "_eval_error": f"{type(_flip_eval_ex).__name__}: {_flip_eval_ex}"[:500],
                        "_metrics_fallback_flags": existing_flags + ["__flip_eval_exception__"],
                    }
                    logger.exception(
                        f"[{node_name}] flip-retry per-alpha eval crashed for "
                        f"{new_alpha.alpha_id or (new_alpha.expression or '')[:60]}; marking FAIL"
                    )

                updated_alphas.append(new_alpha)
                flip_retry_count += 1
                if new_alpha.quality_status == "PASS":
                    flip_retry_pass += 1
                elif new_alpha.quality_status == "PASS_PROVISIONAL":
                    flip_retry_prov += 1
                logger.info(
                    f"[{node_name}] flip-retry result: orig_sharpe={orig.metrics.get('sharpe'):.2f} "
                    f"→ flipped_sharpe={new_sharpe:.2f} status={new_alpha.quality_status}"
                )
        finally:
            # V-27.81: release every in-flight simulate slot claimed in the
            # dedup loop above (covers simulated, failed, mid-loop-continue'd,
            # and now also raised-out flipped expressions). TTL is the crash
            # net for anything still missed.
            for _flip_slot in flip_claimed_slots:
                _flip_release_slot(*_flip_slot)

    # P0: signal-vs-control 双跑归因 (docs/alphagbm_skills_research_2026-05-15.md).
    # 为每个 PASS alpha 模拟一个"对照"表达式(T1 信号核剥成裸字段、保留 T2/T3 结构),
    # 用 Δ(sharpe_signal − sharpe_control) 归因业绩来源。
    # Δ 大 → 信号在做功 → 保持 PASS / "hypothesis"。
    # Δ 小 → 结构产物 → PASS 降级 PASS_PROVISIONAL / "implementation"。
    # 整块为 best-effort:任何失败仅跳过单个 alpha,评估继续。
    # opt-in via ENABLE_SIGNAL_CONTROL_DUAL_RUN(默认 False,避免意外耗配额)。
    dual_run_count = 0
    dual_run_downgraded = 0
    dual_run_no_control = 0
    dual_run_sim_failed = 0
    if brain is not None and getattr(settings, "ENABLE_SIGNAL_CONTROL_DUAL_RUN", False):
        from backend.alpha_expression_utils import derive_control_expression
        from backend.agents.prompts.alignment import determine_attribution_dual_run
        _dr_delta_min = getattr(settings, "SIGNAL_CONTROL_DELTA_SHARPE_MIN", 0.3)
        _dr_cap = getattr(settings, "SIGNAL_CONTROL_CAP", 5)

        # 仅对 PASS 跑对照(直接提交池、"走运结构"风险最高)。
        # flip-retry 提升出的新 PASS 也包含在内(它们已追加到 updated_alphas)。
        # 按 sharpe 降序取 top-cap:最高分的最值得审查。
        _dr_candidates = sorted(
            [
                a for a in updated_alphas
                if a.quality_status == "PASS"
                and a.is_simulated and a.simulation_success
                and isinstance(a.metrics, dict)
            ],
            key=lambda a: a.metrics.get("sharpe", 0) or 0,
            reverse=True,
        )[:_dr_cap]

        if _dr_candidates:
            logger.info(
                f"[{node_name}] signal-vs-control dual-run: "
                f"{len(_dr_candidates)} PASS candidate(s) | delta_min={_dr_delta_min}"
            )

        for _dr_alpha in _dr_candidates:
            _ctrl_expr = derive_control_expression(_dr_alpha.expression or "")
            if not _ctrl_expr:
                dual_run_no_control += 1
                _dr_alpha.metrics["_dual_run_skipped"] = "no_clean_control"
                logger.debug(
                    f"[{node_name}] dual-run: no clean control for "
                    f"{(_dr_alpha.expression or '')[:80]!r}"
                )
                continue
            # Simulate the control with the SAME settings the signal alpha used
            # (stamped into metrics["_sim_settings"] by node_simulate / flip-retry).
            # Without this, Δsharpe mixes signal-core difference with sim-settings
            # difference and the attribution is invalid. Fall back to region/
            # universe only if the stamp is somehow absent.
            _ctl_sim_kwargs = dict(_dr_alpha.metrics.get("_sim_settings") or {
                "region": state.region,
                "universe": state.universe,
                # delay-0: stamp is normally present (has delay); this fallback
                # only fires if it's absent — still honor the task delay then.
                "delay": getattr(state, "delay", 1),
            })
            _ctl_sim_kwargs["expression"] = _ctrl_expr
            try:
                _ctl_result = await brain.simulate_alpha(**_ctl_sim_kwargs)
            except Exception as _dr_exc:
                dual_run_sim_failed += 1
                logger.warning(
                    f"[{node_name}] dual-run control sim failed for "
                    f"{_ctrl_expr[:80]!r}: {_dr_exc}"
                )
                continue
            if not _ctl_result.get("success"):
                dual_run_sim_failed += 1
                logger.debug(
                    f"[{node_name}] dual-run: control sim returned failure for {_ctrl_expr[:80]!r}"
                )
                continue
            _ctl_metrics = _ctl_result.get("metrics") or {}
            _attr, _conf, _evid = determine_attribution_dual_run(
                signal_result=_dr_alpha.metrics,
                control_result=_ctl_metrics,
                delta_sharpe_min=_dr_delta_min,
            )
            _dr_alpha.metrics["_control_expression"] = _ctrl_expr
            _dr_alpha.metrics["_control_sharpe"] = _ctl_metrics.get("sharpe")
            _dr_alpha.metrics["_control_fitness"] = _ctl_metrics.get("fitness")
            _dr_alpha.metrics["_delta_sharpe"] = round(
                (_dr_alpha.metrics.get("sharpe") or 0)
                - (_ctl_metrics.get("sharpe") or 0),
                4,
            )
            _dr_alpha.metrics["_dual_run_attribution"] = _attr
            _dr_alpha.metrics["_dual_run_confidence"] = round(_conf, 3)
            _dr_alpha.metrics["_dual_run_evidence"] = _evid
            dual_run_count += 1
            # 结构产物 → PASS 降级为 PASS_PROVISIONAL(镜像 flip-retry V-16 降级模式)
            if _attr in ("implementation", "both"):
                _dr_alpha.quality_status = "PASS_PROVISIONAL"
                _dr_alpha.metrics["_routing_reason"] = "dual_run_downgrade"
                dual_run_downgraded += 1
                logger.warning(
                    f"[{node_name}] signal-vs-control: PASS → PROV | "
                    f"expr={(_dr_alpha.expression or '')[:60]!r} "
                    f"Δsharpe={_dr_alpha.metrics['_delta_sharpe']} "
                    f"attribution={_attr}"
                )
            else:
                logger.info(
                    f"[{node_name}] signal-vs-control: PASS confirmed | "
                    f"expr={(_dr_alpha.expression or '')[:60]!r} "
                    f"Δsharpe={_dr_alpha.metrics['_delta_sharpe']} "
                    f"attribution={_attr}"
                )

    # ── Shared baseline setup ─────────────────────────────────────────────────
    # expected_signal lookup + category_resolver are needed by both the
    # graded-score block (below) and the baseline screening block (further down).
    # Computed once here so both blocks share the SAME resolver — previously
    # graded-score's BaselineProvider was constructed without one and could
    # never fall back to the coarse cell, inflating false "no baseline" counts.
    _shared_baseline_cfg_needed = (
        getattr(settings, "ENABLE_GRADED_SCORE", False)
        or getattr(settings, "BASELINE_SCREEN_ENABLED", False)
    )
    _shared_expected_signal: str = "unknown"
    def _shared_resolve_category(_ds):  # default no-op resolver
        return None
    if _shared_baseline_cfg_needed:
        for _h in (state.hypotheses or []):
            if isinstance(_h, dict) and _h.get("expected_signal"):
                _shared_expected_signal = _h["expected_signal"]
                break
        if _shared_expected_signal == "unknown" and getattr(state, "current_hypothesis_id", None):
            try:
                from backend.database import AsyncSessionLocal
                from backend.models import Hypothesis as _SharedHyp
                async with AsyncSessionLocal() as _shared_db:
                    _shared_row = await _shared_db.get(_SharedHyp, state.current_hypothesis_id)
                    if _shared_row and _shared_row.expected_signal:
                        _shared_expected_signal = _shared_row.expected_signal
            except Exception:
                pass

        _cat_map: Dict[str, str] = {}
        for _f in (state.fields or []):
            if isinstance(_f, dict):
                _ds = _f.get("dataset_id")
                _cat = _f.get("category") or _f.get("dataset_category")
                if _ds and _cat:
                    _cat_map[_ds] = _cat
        _default_cat = getattr(state, "dataset_category", None) or None

        def _shared_resolve_category(ds_id):
            return _cat_map.get(ds_id) or _default_cat

    # P1-A: graded-score — 百分位归一化 + 非均匀权重 + confidence 维度
    # (docs/alphagbm_skills_research_2026-05-15.md 原则①).
    # Advisory layer: compute_graded_score produces percentile/grade/confidence
    # alongside the existing raw _score.  A PASS alpha with low confidence
    # (applicable inputs < SCORE_CONFIDENCE_MIN) is downgraded to PASS_PROVISIONAL —
    # confidence is the real decision gate, not the grade.
    # calculate_alpha_score / route_alpha_action / band thresholds are untouched.
    # Opt-in via ENABLE_GRADED_SCORE (default False).  Setup failures degrade
    # the whole block; per-alpha failures are isolated and counted.
    graded_count = 0
    graded_downgraded = 0
    graded_no_baseline = 0
    graded_failed = 0
    if getattr(settings, "ENABLE_GRADED_SCORE", False):
        try:
            from backend.alpha_scoring import compute_graded_score
            from backend.agents.services.baseline_provider import BaselineProvider

            _gs_conf_min = getattr(settings, "SCORE_CONFIDENCE_MIN", 0.5)
            _gs_cap = getattr(settings, "SCORE_GRADED_CAP", 0)
            _gs_weights = {
                "test_sharpe":           getattr(settings, "SCORE_WEIGHT_TEST_SHARPE", 0.55),
                "train_sharpe":          getattr(settings, "SCORE_WEIGHT_TRAIN_SHARPE", 0.25),
                "fitness":               getattr(settings, "SCORE_WEIGHT_FITNESS", 0.20),
                "prod_corr_penalty":     getattr(settings, "SCORE_WEIGHT_PROD_CORR_PENALTY", 0.30),
                "turnover_penalty":      getattr(settings, "SCORE_WEIGHT_TURNOVER_PENALTY", 0.15),
                "investability_penalty": getattr(settings, "SCORE_WEIGHT_INVESTABILITY_PENALTY", 0.20),
            }
            # request-scoped; uses the shared category_resolver so insufficient
            # fine cells fall back to the coarse cell — same as baseline block.
            _gs_bp = BaselineProvider(category_resolver=_shared_resolve_category)
            _gs_tier = tier_cfg.get("tier")

            _gs_candidates = [
                a for a in updated_alphas
                if a.quality_status == "PASS"
                and a.is_simulated and a.simulation_success
                and isinstance(a.metrics, dict)
            ]
            if _gs_cap > 0:
                _gs_candidates = sorted(
                    _gs_candidates,
                    key=lambda a: a.metrics.get("sharpe", 0) or 0,
                    reverse=True,
                )[:_gs_cap]

            for _gs_alpha in _gs_candidates:
                try:
                    # Baseline lookup
                    _gs_stats = None
                    try:
                        _gs_stats = await _gs_bp.get_baseline(
                            expected_signal=_shared_expected_signal,
                            dataset_id=state.dataset_id or "",
                            region=state.region,
                        )
                    except Exception as _gs_bl_exc:
                        logger.warning(
                            f"[{node_name}] graded-score baseline lookup failed: {_gs_bl_exc}"
                        )
                    _gs_baseline_usable = (
                        _gs_stats is not None and getattr(_gs_stats, "usable", False)
                    )
                    if not _gs_baseline_usable:
                        graded_no_baseline += 1

                    # calculate_alpha_score reads flat keys (sharpe/fitness/turnover)
                    # from sim_result["train"/"test"]. alpha.metrics IS flat with those
                    # keys → wrapping it satisfies the extractor without a snapshot.
                    _gs_sim = {"train": _gs_alpha.metrics, "test": _gs_alpha.metrics}

                    # Tri-state confidence_inputs (P1-A fix):
                    #   True  = real measurement on file (incl. true 0.0)
                    #   False = expected for this alpha but missing/fabricated
                    #   None  = not applicable for this tier/context → SKIPPED
                    # Old code conflated "not measured" with "measured zero" and
                    # counted N/A as fabricated, systematically downgrading
                    # tier-1 / cold-start alphas.
                    if bool(_gs_alpha.metrics.get("_corr_checked")):
                        # Once corr-check ran, any value (incl. true 0.0) is real.
                        _prod_corr_input: Optional[bool] = True
                    else:
                        _prod_corr_input = None  # corr-check didn't run for this alpha

                    if _gs_tier in (2, 3):
                        # Only tier-2/3 are expected to carry self_corr.
                        _self_corr_input: Optional[bool] = (
                            _gs_alpha.metrics.get("_self_corr") is not None
                        )
                    else:
                        _self_corr_input = None  # tier-1 / unknown — N/A

                    # P1-B integration: a metric that was fallback-replaced
                    # (NaN/inf/missing → default by _safe_metric) is `not None`
                    # but no longer a real measurement. Treat as "expected but
                    # missing" so confidence drops, per fear-score's
                    # "fallback 标记 + confidence=真实数据占比" pairing.
                    _core_metric_keys = ("sharpe", "fitness", "turnover")
                    _fallback_flags = _gs_alpha.metrics.get("_metrics_fallback_flags") or []
                    _core_fellback = any(f in _core_metric_keys for f in _fallback_flags)
                    _gs_conf_inputs: Dict[str, Optional[bool]] = {
                        "prod_corr_real":   _prod_corr_input,
                        "self_corr_real":   _self_corr_input,
                        "baseline_real":    _gs_baseline_usable,
                        "metrics_complete": (
                            all(
                                _gs_alpha.metrics.get(k) is not None
                                for k in _core_metric_keys
                            )
                            and not _core_fellback
                        ),
                    }
                    _gs = compute_graded_score(
                        _gs_sim,
                        prod_corr=_gs_alpha.metrics.get("_prod_corr") or 0.0,
                        self_corr=_gs_alpha.metrics.get("_self_corr") or 0.0,
                        weights=_gs_weights,
                        baseline_stats=_gs_stats,
                        confidence_inputs=_gs_conf_inputs,
                    )
                    _gs_alpha.metrics["_score_pct"] = round(_gs.percentile, 4)
                    _gs_alpha.metrics["_score_grade"] = _gs.grade
                    _gs_alpha.metrics["_score_grade_action"] = _gs.grade_action
                    _gs_alpha.metrics["_score_confidence"] = round(_gs.confidence, 3)
                    _gs_alpha.metrics["_score_evidence"] = _gs.evidence
                    graded_count += 1

                    if _gs.confidence < _gs_conf_min:
                        _gs_alpha.quality_status = "PASS_PROVISIONAL"
                        _gs_alpha.metrics["_routing_reason"] = "graded_low_confidence"
                        graded_downgraded += 1
                        logger.warning(
                            f"[{node_name}] graded-score: PASS → PROV | "
                            f"expr={(_gs_alpha.expression or '')[:60]!r} "
                            f"confidence={_gs.confidence:.3f} < {_gs_conf_min} "
                            f"grade={_gs.grade}"
                        )
                    else:
                        logger.info(
                            f"[{node_name}] graded-score: PASS kept | "
                            f"expr={(_gs_alpha.expression or '')[:60]!r} "
                            f"grade={_gs.grade} pct={_gs.percentile:.3f} "
                            f"confidence={_gs.confidence:.3f}"
                        )
                except Exception as _gs_alpha_exc:
                    graded_failed += 1
                    logger.warning(
                        f"[{node_name}] graded-score: per-alpha failure for "
                        f"{(_gs_alpha.expression or '')[:60]!r}: {_gs_alpha_exc}"
                    )
                    continue
        except Exception as _gs_block_exc:
            # Setup-level guard: import / config failure leaves all alphas
            # un-graded; evaluation continues as if ENABLE_GRADED_SCORE were False.
            logger.warning(
                f"[{node_name}] graded-score block setup failed (degrading): {_gs_block_exc}"
            )

    # ── P1-D: What-if window-perturbation robustness gate ────────────────────
    # Source: docs/alphagbm_skills_research_2026-05-15.md skill `pnl-simulator`.
    # M-9 insertion site: graded-score block 之后,baseline-screen 块之前(独立段)。
    # 既不污染 graded-score 共享 baseline setup,也不影响 baseline-screen 的 soft-signal
    # 注释。本块只动 quality_status (PASS → PASS_PROVISIONAL 降级)+ metrics stamp。
    # 整块为 best-effort:任何失败仅跳过单个 alpha,评估继续。
    # opt-in via ENABLE_ROBUSTNESS_CHECK (默认 False,避免意外烧 BRAIN 配额)。
    robustness_attempted = 0
    robustness_passed = 0
    robustness_failed_downgrade = 0
    robustness_skipped_no_window = 0
    robustness_skipped_quota = 0
    robustness_skipped_round_cap = 0
    robustness_skipped_timeout = 0
    robustness_skipped_baseline_missing = 0  # P3 fix: split _other into 4 buckets
    robustness_skipped_baseline_zero = 0
    robustness_skipped_sim_failed = 0
    robustness_skipped_exception = 0
    robustness_sim_failed_total = 0

    if brain is not None and getattr(settings, "ENABLE_ROBUSTNESS_CHECK", False):
        try:
            _rb_n = getattr(settings, "ROBUSTNESS_N_PERTURBATIONS", 4)
            _rb_ratio = getattr(settings, "ROBUSTNESS_MIN_RATIO", 0.7)
            _rb_cap = getattr(settings, "MAX_ROBUSTNESS_PER_ROUND", 5)
            _rb_qpct = getattr(settings, "ROBUSTNESS_SKIP_QUOTA_PCT", 0.65)
            _rb_hot_pct = getattr(settings, "ROBUSTNESS_HOTCHECK_QUOTA_PCT", 0.85)
            _rb_alpha_timeout = getattr(
                settings, "ROBUSTNESS_PER_ALPHA_TIMEOUT_SEC", 600
            )
            _rb_strategy = getattr(
                settings, "ROBUSTNESS_SELECTION_STRATEGY", "first"
            )

            # M-2:复用 _quota_guard_async(单 SQL,handles Alpha.created_at naive
            # vs AlphaFailure.created_at tz-aware 不一致)。失败 → pct=0.0 放行。
            _rb_redis = None
            _rb_robustness_extra = 0
            _rb_today_total = 0
            _rb_limit = getattr(settings, "BRAIN_DAILY_SIMULATE_LIMIT", 1000)
            try:
                # P2 review fix (2026-05-16): lazy import to break agents↔tasks cycle.
                from backend.tasks.session_watchdog import _quota_guard_async
                _q = await _quota_guard_async()
                _rb_today_total = int(_q.get("today_total_count", 0) or 0)
                _rb_limit = int(
                    _q.get("limit")
                    or getattr(settings, "BRAIN_DAILY_SIMULATE_LIMIT", 1000)
                )
            except Exception as _rb_q_exc:
                logger.warning(
                    f"[{node_name}] robustness quota pre-check failed (degrading): {_rb_q_exc}"
                )

            try:
                _rb_redis = _rb_redis_aio.from_url(
                    settings.REDIS_URL, decode_responses=True
                )
                # P2 fix: per-UTC-day key, aligned with BRAIN 00:00 UTC reset.
                _rb_today_key = RobustnessGate.today_key()
                _rb_extra_raw = await _rb_redis.get(_rb_today_key)
                _rb_robustness_extra = int(_rb_extra_raw or 0)
            except Exception as _rb_r_exc:
                _rb_redis = None
                _rb_robustness_extra = 0
                logger.warning(
                    f"[{node_name}] robustness redis init failed (counter disabled): {_rb_r_exc}"
                )

            _rb_used_pct = (
                (_rb_today_total + _rb_robustness_extra) / max(_rb_limit, 1)
            )

            if _rb_used_pct >= _rb_qpct:
                logger.warning(
                    f"[{node_name}] robustness ROUND SKIPPED quota_pct={_rb_used_pct:.2f} >= {_rb_qpct}"
                )
                for _rb_alpha in updated_alphas:
                    if (
                        _rb_alpha.quality_status == "PASS"
                        and isinstance(_rb_alpha.metrics, dict)
                        and _rb_alpha.metrics.get("_robustness_passed") is None
                        and _rb_alpha.metrics.get("_robustness_skipped") is None
                    ):
                        _rb_alpha.metrics["_robustness_skipped"] = "quota_exhausted"
                        robustness_skipped_quota += 1
            else:
                # Top-sharpe PASS only (scope per plan); cap to MAX_ROBUSTNESS_PER_ROUND.
                _rb_candidates = sorted(
                    [
                        a for a in updated_alphas
                        if a.quality_status == "PASS"
                        and a.is_simulated
                        and a.simulation_success
                        and isinstance(a.metrics, dict)
                    ],
                    key=lambda a: a.metrics.get("sharpe", 0) or 0,
                    reverse=True,
                )
                _rb_in_cap = _rb_candidates[:_rb_cap]
                for _rb_alpha in _rb_candidates[_rb_cap:]:
                    if (
                        _rb_alpha.metrics.get("_robustness_passed") is None
                        and _rb_alpha.metrics.get("_robustness_skipped") is None
                    ):
                        _rb_alpha.metrics["_robustness_skipped"] = "round_cap"
                        robustness_skipped_round_cap += 1

                gate = RobustnessGate(
                    brain,
                    n_perturbations=_rb_n,
                    min_ratio=_rb_ratio,
                    selection_strategy=_rb_strategy,
                    redis_client=_rb_redis,
                )

                for _rb_alpha in _rb_in_cap:
                    # idempotent: 已 stamp 的 alpha 跳过(防止双跑 / 重入)
                    if (
                        _rb_alpha.metrics.get("_robustness_passed") is not None
                        or _rb_alpha.metrics.get("_robustness_skipped") is not None
                    ):
                        continue

                    # M-7 hot-check:每完成一个 alpha 检查 counter,超 0.85 后续 skip
                    if _rb_redis is not None:
                        try:
                            _cur_extra_raw = await _rb_redis.get(
                                RobustnessGate.today_key()
                            )
                            _cur_extra = int(_cur_extra_raw or 0)
                            _cur_pct = (
                                (_rb_today_total + _cur_extra) / max(_rb_limit, 1)
                            )
                            if _cur_pct >= _rb_hot_pct:
                                _rb_alpha.metrics["_robustness_skipped"] = "quota_exhausted"
                                robustness_skipped_quota += 1
                                continue
                        except Exception:
                            pass

                    # S-5 per-alpha hard timeout
                    try:
                        result = await asyncio.wait_for(
                            gate.check(_rb_alpha),
                            timeout=_rb_alpha_timeout,
                        )
                    except asyncio.TimeoutError:
                        _rb_alpha.metrics["_robustness_skipped"] = "per_alpha_timeout"
                        robustness_skipped_timeout += 1
                        continue
                    except Exception as _rb_exc:
                        logger.warning(
                            f"[{node_name}] robustness raised for "
                            f"{(_rb_alpha.expression or '')[:60]!r}: {_rb_exc}"
                        )
                        _rb_alpha.metrics["_robustness_skipped"] = "exception"
                        robustness_skipped_exception += 1
                        continue

                    robustness_attempted += 1
                    robustness_sim_failed_total += result.sim_failed_count
                    _rb_alpha.metrics["_robustness_baseline_sharpe"] = round(
                        result.baseline_sharpe, 4
                    )
                    _rb_alpha.metrics["_robustness_n_run"] = result.perturbation_count
                    _rb_alpha.metrics["_robustness_elapsed_ms"] = result.elapsed_ms

                    if result.skip_reason:
                        _rb_alpha.metrics["_robustness_skipped"] = result.skip_reason
                        # P3 fix: split _other into 4 specific buckets so ops
                        # can distinguish data-deficit vs sim-failure vs code-bug.
                        if result.skip_reason == "no_window":
                            robustness_skipped_no_window += 1
                        elif result.skip_reason == "baseline_metrics_missing":
                            robustness_skipped_baseline_missing += 1
                        elif result.skip_reason == "baseline_sharpe_zero":
                            robustness_skipped_baseline_zero += 1
                        elif result.skip_reason == "all_perturbations_failed":
                            robustness_skipped_sim_failed += 1
                        else:
                            robustness_skipped_exception += 1
                        continue

                    # 成功完成 check (passed=True 或 False)
                    _rb_alpha.metrics["_robustness_worst_sharpe"] = result.worst_sharpe
                    _rb_alpha.metrics["_robustness_worst_ratio"] = result.worst_ratio
                    _rb_alpha.metrics["_robustness_passed"] = result.passed
                    _rb_alpha.metrics["_robustness_can_submit_consistency"] = (
                        result.can_submit_consistency
                    )  # S-7 观测信号

                    if result.passed:
                        robustness_passed += 1
                        logger.info(
                            f"[{node_name}] robustness PASS | "
                            f"expr={(_rb_alpha.expression or '')[:60]!r} "
                            f"baseline={result.baseline_sharpe:.3f} "
                            f"worst={result.worst_sharpe:.3f} "
                            f"ratio={result.worst_ratio:.3f}"
                        )
                    else:
                        _rb_alpha.quality_status = "PASS_PROVISIONAL"
                        # M-6 idempotent:不覆盖 graded-score / dual-run 已 stamp 的 _routing_reason
                        _rb_alpha.metrics.setdefault(
                            "_routing_reason", "robustness_downgrade"
                        )
                        _rb_alpha.metrics["_robustness_failed"] = True  # M-8 KB-skip flag
                        _rb_alpha.metrics["_skip_optimize_pool"] = True  # M-8 防 OPTIMIZE 池二次烧
                        robustness_failed_downgrade += 1
                        logger.warning(
                            f"[{node_name}] robustness: PASS → PROV | "
                            f"expr={(_rb_alpha.expression or '')[:60]!r} "
                            f"baseline={result.baseline_sharpe:.3f} "
                            f"worst={result.worst_sharpe:.3f} "
                            f"ratio={result.worst_ratio:.3f} < {_rb_ratio}"
                        )

            # Close async redis client gracefully so its socket releases
            # before the node returns (matters in test fixtures / aiosqlite).
            if _rb_redis is not None:
                try:
                    await _rb_redis.close()
                except Exception:
                    pass
        except Exception as _rb_block_exc:
            logger.warning(
                f"[{node_name}] robustness block setup failed (degrading): {_rb_block_exc}"
            )

    # P0: baseline + Nσ-residual screening (docs/alphagbm_skills_research_2026-05-15.md).
    # Annotates each successfully-simulated alpha with its residual against the
    # (hypothesis-family × dataset × region) grid baseline. SOFT SIGNAL ONLY —
    # never touches quality_status / hard_gate / near_pass / submission gates.
    # Historically consumed by the retired ``_identify_optimization_candidates``
    # in mining_agent.py to prioritise the optimization budget. Post-Phase-16-A
    # the residual is dormant (annotation only); it could be revived by the new
    # OptimizationService near-gate SQL if Stage A's GO/STOP gate calls for it.
    # Opt-in via BASELINE_SCREEN_ENABLED.
    # The whole block is best-effort: any failure leaves alphas un-annotated and
    # evaluation proceeds exactly as before.
    baseline_discoveries = 0
    baseline_below = 0
    baseline_insufficient = 0
    if getattr(settings, "BASELINE_SCREEN_ENABLED", False):
        try:
            from backend.agents.services.baseline_provider import BaselineProvider
            from backend.baseline_screener import (
                BELOW,
                DISCOVERY,
                INSUFFICIENT_DATA,
                classify_residual,
                residual_sigma,
            )

            # expected_signal + category_resolver come from the shared baseline
            # setup block above (computed once, reused by graded-score too).
            expected_signal = _shared_expected_signal
            provider = BaselineProvider(
                metric_col="is_sharpe",
                category_resolver=_shared_resolve_category,
            )
            metric_key = getattr(settings, "BASELINE_METRIC", "sharpe")
            discovery_sigma = getattr(settings, "BASELINE_DISCOVERY_SIGMA", 2.0)
            below_sigma = getattr(settings, "BASELINE_BELOW_SIGMA", -1.0)

            for i, alpha in enumerate(updated_alphas):
                if not (alpha.is_simulated and alpha.simulation_success):
                    continue
                metrics = alpha.metrics if isinstance(alpha.metrics, dict) else {}
                metric_val = metrics.get(metric_key)
                stats = await provider.get_baseline(
                    expected_signal, state.dataset_id, state.region
                )
                sigma = residual_sigma(metric_val, stats)
                cls = classify_residual(sigma, discovery_sigma, below_sigma)
                # Detach metrics before mutating so we don't write through to a
                # dict shared with the LangGraph input state (see V-26.79).
                alpha.metrics = dict(metrics)
                alpha.metrics["baseline_residual_sigma"] = (
                    round(sigma, 4) if sigma is not None else None
                )
                alpha.metrics["baseline_cell"] = stats.cell_key
                alpha.metrics["baseline_n"] = stats.count
                alpha.metrics["baseline_class"] = cls
                updated_alphas[i] = alpha
                if cls == DISCOVERY:
                    baseline_discoveries += 1
                elif cls == BELOW:
                    baseline_below += 1
                elif cls == INSUFFICIENT_DATA:
                    baseline_insufficient += 1

            logger.info(
                f"[{node_name}] baseline screen | discoveries={baseline_discoveries} "
                f"below={baseline_below} insufficient={baseline_insufficient} "
                f"signal={expected_signal}"
            )
        except Exception as e:
            logger.warning(f"[{node_name}] baseline screen skipped (error): {e}")

    # P1-B: single source of truth for counters — replaces 12 scattered +=1/-=1 sites.
    # Invariant: pass + optimize + fail + pending == N. provisional_count is a
    # subset of optimize (PROV enters optimize bucket but is also reported
    # separately for visibility, consistent with P1-A's PROV→optimize mapping).
    # PENDING (V-27.61 retryable sim) is its OWN bucket so transient BRAIN 429s
    # / slot timeouts do NOT inflate fail_count and mislead operators.
    pass_count = 0
    optimize_count = 0
    fail_count = 0
    pending_count = 0
    provisional_count = 0
    for _tally_alpha in updated_alphas:
        _qs = _tally_alpha.quality_status
        if _qs == "PASS":
            pass_count += 1
        elif _qs == "PASS_PROVISIONAL":
            provisional_count += 1
            optimize_count += 1
        elif _qs == "OPTIMIZE":
            optimize_count += 1
        elif _qs == "PENDING":
            pending_count += 1
        else:
            fail_count += 1       # FAIL / None / unknown

    duration_ms = int((time.time() - start_time) * 1000)

    _debug_log("E", "nodes.py:evaluate:result", "Evaluation complete", {
        "pass": pass_count,
        "optimize": optimize_count,
        "fail": fail_count,
        "pending": pending_count,
        "corr_checked": corr_checks_performed,
        "corr_skipped": corr_checks_skipped,
        "duration_ms": duration_ms,
        "pass_rate": round(pass_count / max(1, pass_count + optimize_count + fail_count) * 100, 1)
    })

    logger.info(
        f"[{node_name}] Complete | pass={pass_count} optimize={optimize_count} "
        f"fail={fail_count} pending={pending_count} "
        f"corr_checked={corr_checks_performed} corr_skipped={corr_checks_skipped}"
    )
    
    # Experiment tracking
    if EXPERIMENT_TRACKING_ENABLED:
        exp = get_current_experiment()
        if exp:
            exp.metrics.increment("pass_count", pass_count)
            exp.metrics.record("iteration_duration_ms", duration_ms, tags={"node": node_name})
            
            total_evaluated = pass_count + optimize_count + fail_count
            if total_evaluated > 0:
                exp.metrics.record("pass_rate", pass_count / total_evaluated * 100, tags={"region": state.region})
            
            total_corr = corr_checks_performed + corr_checks_skipped
            if total_corr > 0:
                exp.metrics.record("corr_check_skip_rate",
                    corr_checks_skipped / total_corr * 100,
                    tags={"node": node_name}
                )
    
    # Record failure feedback with attribution-aware filtering
    if failure_feedback_queue:
        rag_service = config.get("configurable", {}).get("rag_service") if config else None
        if rag_service:
            feedback_recorded = 0
            hypothesis_failures = 0
            implementation_failures = 0

            sample_size = min(3, len(failure_feedback_queue))
            sampled_failures = random.sample(failure_feedback_queue, sample_size)

            # Plan v5+ §B8: tag every recorded pitfall with the active typed
            # Hypothesis + experiment_variant so the KB learning unit becomes
            # (alpha, hypothesis_id, dataset_pool) instead of (alpha, dataset).
            #
            # Smoke-test (2026-05-06) revealed that LangGraph scalar field
            # propagation is unreliable across nodes — state.current_hypothesis_id
            # was None at evaluation time even though the list field
            # state.current_hypothesis_ids was populated correctly. Fallback
            # to list[0] keeps the KB tagging working under that regime.
            cfg = config.get("configurable", {}) if config else {}
            current_hypothesis_id = getattr(state, "current_hypothesis_id", None)
            if current_hypothesis_id is None:
                _hids = getattr(state, "current_hypothesis_ids", None) or []
                if _hids:
                    current_hypothesis_id = _hids[0]
            experiment_variant = cfg.get("experiment_variant")

            for feedback in sampled_failures:
                attribution = feedback.get("attribution", "unknown")

                # Track attribution stats
                if attribution == "hypothesis":
                    hypothesis_failures += 1
                elif attribution == "implementation":
                    implementation_failures += 1

                try:
                    # V-26.40 (2026-05-13): only record when attribution is
                    # confidently HYPOTHESIS or BOTH. Pre-fix used
                    # `attribution != "implementation"` which let "unknown"
                    # alphas through, contaminating the KB with feedback we
                    # couldn't even attribute. classify_attribution returns
                    # "unknown" precisely when there's no signal — those
                    # rows should NOT teach the KB anything.
                    should_record = attribution in ("hypothesis", "both")

                    # V-27.97: symmetric guard with record_success_pattern
                    # (persistence.py V-26.93). When hypothesis-keyed KB is
                    # the active level (>=2) but no hypothesis link is
                    # available, skip the write rather than poison the
                    # FAILURE_PITFALL pool with hypothesis_id=None rows —
                    # success pool got this guard, failure pool didn't.
                    active_level = cfg.get("hypothesis_centric_level") or 0
                    if should_record and active_level >= 2 and current_hypothesis_id is None:
                        logger.warning(
                            f"[{node_name}] V-27.97 skip FAILURE_PITFALL write: "
                            f"level={active_level} but hypothesis_id=None"
                        )
                        should_record = False

                    if should_record:
                        await rag_service.record_failure_pattern(
                            expression=feedback["expression"],
                            error_type=feedback["error_type"],
                            metrics=feedback["metrics"],
                            region=feedback["region"],
                            dataset_id=feedback["dataset_id"],
                            hypothesis_id=current_hypothesis_id,
                            experiment_variant=experiment_variant,
                        )
                        feedback_recorded += 1
                    else:
                        logger.debug(
                            f"[{node_name}] Skipping knowledge record "
                            f"(attribution={attribution}): "
                            f"{feedback['alignment_issues'][:2] if feedback.get('alignment_issues') else 'N/A'}"
                        )
                except Exception as e:
                    logger.warning(f"[{node_name}] Failed to record feedback: {e}")
            
            logger.info(
                f"[{node_name}] Knowledge feedback | recorded={feedback_recorded}/{len(failure_feedback_queue)} "
                f"(hypothesis_fail={hypothesis_failures} impl_fail={implementation_failures})"
            )
    
    trace_update = await record_trace(
        state, trace_service, node_name,
        {
            "evaluation_mode": "two_stage_correlation",
            "thresholds": {
                "sharpe_min": sharpe_min,
                "turnover_max": turnover_max,
                "fitness_min": fitness_min,
                "score_pass": score_pass_threshold,
                "corr_check_threshold": corr_check_threshold,
            }
        },
        {
            "pass_count": pass_count,
            "optimize_count": optimize_count,
            "fail_count": fail_count,
            "corr_checks_performed": corr_checks_performed,
            "corr_checks_skipped": corr_checks_skipped,
            "flip_retry_count": flip_retry_count,
            "flip_retry_pass": flip_retry_pass,
            "flip_retry_prov": flip_retry_prov,
            "baseline_discoveries": baseline_discoveries,
            "baseline_below": baseline_below,
            "baseline_insufficient": baseline_insufficient,
            "dual_run_count": dual_run_count,
            "dual_run_downgraded": dual_run_downgraded,
            "dual_run_no_control": dual_run_no_control,
            "dual_run_sim_failed": dual_run_sim_failed,
            "graded_count": graded_count,
            "graded_downgraded": graded_downgraded,
            "graded_no_baseline": graded_no_baseline,
            "graded_failed": graded_failed,
            "robustness_attempted": robustness_attempted,
            "robustness_passed": robustness_passed,
            "robustness_failed_downgrade": robustness_failed_downgrade,
            "robustness_skipped_no_window": robustness_skipped_no_window,
            "robustness_skipped_quota": robustness_skipped_quota,
            "robustness_skipped_round_cap": robustness_skipped_round_cap,
            "robustness_skipped_timeout": robustness_skipped_timeout,
            "robustness_skipped_baseline_missing": robustness_skipped_baseline_missing,
            "robustness_skipped_baseline_zero": robustness_skipped_baseline_zero,
            "robustness_skipped_sim_failed": robustness_skipped_sim_failed,
            "robustness_skipped_exception": robustness_skipped_exception,
            "robustness_sim_failed_total": robustness_sim_failed_total,
            "provisional_count": provisional_count,
            "pending_count": pending_count,
            "eval_errors": eval_errors,
            "details": eval_details[:20]
        },
        duration_ms,
        "SUCCESS"
    )

    # === R1a hook + R5 LLM judge (Phase 0 v1.6 / Phase 2 R5) ===
    # R1a writes AttributionType into alpha.metrics + dedicated
    # r1a_attribution_log table (50× INSERT throughput vs. relying on
    # alpha persistence — FAIL/OPTIMIZE alphas get GC'd, only 1/round
    # actually persists). R5 LLM judge runs AFTER R1a heuristic so it
    # can OVERWRITE R1a verdict per [V1.0-A2-3] conflict-resolution lock.
    #
    # Bug-#6 fix (2026-05-18): R5 used to be NESTED under ENABLE_R1A_HOOK
    # so ENABLE_LLM_JUDGE alone did nothing and turning R1a off silently
    # killed R5. Now each flag has its own guard; if either is ON we
    # walk updated_alphas once and apply whichever hooks are enabled. The
    # r1a_attribution_log row is still written when ANY hook ran so the
    # R5-only configuration has a place to land its r5_* columns.
    _r1a_on = bool(getattr(settings, "ENABLE_R1A_HOOK", False))
    _r5_on = bool(getattr(settings, "ENABLE_LLM_JUDGE", False))
    if _r1a_on or _r5_on:
        # Lazy imports: avoid cold-start cost when both flags OFF.
        import hashlib  # noqa: E402
        from backend.database import AsyncSessionLocal  # noqa: E402
        from backend.models.r1a_attribution import R1aAttributionLog  # noqa: E402

        # Only import R1a integration shim when its flag is on — keeps
        # the legacy "core/ is 3223 LOC DORMANT" cost gate intact.
        if _r1a_on:
            from backend.agents.core.integration import enhance_existing_node_evaluate  # noqa: E402
        if _r5_on:
            from backend.agents.graph.r5_judge import run_r5_judge  # noqa: E402
            from backend.agents.services.llm_service import get_llm_service  # noqa: E402

        _r1a_failures = 0
        _r1a_db_failures = 0  # M1 review fix: count per-row DB-side failures
                              # (savepoint rollbacks) + whole-batch transaction
                              # failures, separately from heuristic-side
                              # _r1a_failures. Surfaced via end-of-round stats
                              # log so log scrapers / GO gate observer can see
                              # silent data loss.
        _r1a_log_rows = []  # collect first, INSERT in one batch after loop
        _task_id_for_r1a = getattr(state, "task_id", None)

        for _a in updated_alphas:  # _a is AlphaCandidate (Pydantic BaseModel)
            # V-26.79 detach: rebind to fresh dict before mutating
            _new_metrics = dict(_a.metrics) if isinstance(_a.metrics, dict) else {}
            _r1a_log = {
                "task_id": _task_id_for_r1a,
                "alpha_id_brain": getattr(_a, "alpha_id", None),
                "expression": getattr(_a, "expression", "") or "",
                "expression_hash": hashlib.sha256(
                    (getattr(_a, "expression", "") or "").encode("utf-8")
                ).hexdigest()[:64],
                "quality_status_at_eval": getattr(_a, "quality_status", None),
                "hook_version": "v1",
                "attribution": None,
                "attribution_confidence": None,
                "attribution_evidence": None,
                "should_retry_implementation": None,
                "should_modify_hypothesis": None,
                "hook_error": None,
            }
            # Shared context for both hooks — populated even when R1a flag
            # is off so the R5-only path still has a hypothesis statement
            # to feed run_r5_judge.
            _hyp = {"statement": getattr(_a, "hypothesis", "") or ""}

            if _r1a_on:
                try:
                    _sim = {
                        "sharpe": _new_metrics.get("sharpe"),
                        "fitness": _new_metrics.get("fitness"),
                    }
                    _fb = enhance_existing_node_evaluate(_a, _sim, _hyp, trace=None)
                    _new_metrics["_r1a_attribution"] = _fb.attribution.value
                    _new_metrics["_r1a_attribution_confidence"] = _fb.attribution_confidence
                    if _fb.attribution_evidence:  # skip empty list — saves storage
                        _new_metrics["_r1a_attribution_evidence"] = list(_fb.attribution_evidence)
                    _new_metrics["_r1a_should_retry"] = bool(_fb.should_retry_implementation)
                    _new_metrics["_r1a_should_modify"] = bool(_fb.should_modify_hypothesis)
                    _new_metrics["_r1a_hook_version"] = "v1"
                    # log row mirrors metrics
                    _r1a_log["attribution"] = _fb.attribution.value
                    _r1a_log["attribution_confidence"] = _fb.attribution_confidence
                    _r1a_log["attribution_evidence"] = list(_fb.attribution_evidence) if _fb.attribution_evidence else None
                    _r1a_log["should_retry_implementation"] = "true" if _fb.should_retry_implementation else "false"
                    _r1a_log["should_modify_hypothesis"] = "true" if _fb.should_modify_hypothesis else "false"
                except Exception as _r1a_e:  # noqa: BLE001 — must isolate per-alpha failures
                    _r1a_failures += 1
                    logger.warning(
                        "[R1a] hook failed for alpha {}: {}",
                        getattr(_a, "alpha_id", "?"), _r1a_e
                    )
                    _new_metrics["_r1a_attribution"] = None
                    _new_metrics["_r1a_hook_error"] = str(_r1a_e)[:200]
                    _new_metrics["_r1a_hook_version"] = "v1"  # mark even on fail so GO denominator includes
                    _r1a_log["hook_error"] = str(_r1a_e)[:200]
            # Pydantic field reassignment (AlphaCandidate is Pydantic BaseModel —
            # no flag_modified needed; persistence.py INSERT path doesn't need dirty marker)
            _a.metrics = _new_metrics

            # === Phase 2 R5 LLM judge (plan v1.0 §1.2, 2026-05-18) ===
            # Runs AFTER R1a heuristic (when both enabled) so R5 can
            # OVERWRITE R1a verdict per [V1.0-A2-3]. R5 None (both PASS
            # / low conf) preserves R1a verdict. Per-alpha try/except
            # guard — same as R1a, must not block round.
            if _r5_on:
                try:
                    # AlphaCandidate (Pydantic, agents/graph/state.py:19) has
                    # field `explanation`; SQLAlchemy Alpha.logic_explanation
                    # is the DB-persisted name (mapped in persistence.py:270).
                    # In-loop _a is AlphaCandidate so read `explanation` —
                    # task 1463 verify showed 100% skip due to reading the
                    # wrong (DB) name. Same for `hypothesis` (already a
                    # field on AlphaCandidate, plain attr).
                    _desc_text = getattr(_a, "explanation", "") or ""
                    _hyp_text = getattr(_a, "hypothesis", "") or _hyp.get("statement", "") or ""
                    _r5_payload = await run_r5_judge(
                        hypothesis_statement=_hyp_text,
                        description=_desc_text,
                        expression=getattr(_a, "expression", "") or "",
                        llm_service=get_llm_service(),
                        r1a_attribution=_r1a_log.get("attribution"),
                        operators_used=None,
                    )
                    # Merge 10 r5_* columns (pop the internal r5_attribution first)
                    _r5_override = _r5_payload.pop("r5_attribution", None)
                    _r1a_log.update(_r5_payload)
                    # [V1.0-A2-3] R5 wins: overwrite R1a attribution when R5 has verdict
                    if _r5_override is not None:
                        _r1a_log["attribution"] = _r5_override
                        # mirror to metrics for downstream R2/Q7 / R8 readers
                        _new_metrics["_r1a_attribution"] = _r5_override
                        _a.metrics = _new_metrics
                except Exception as _r5_e:  # noqa: BLE001
                    logger.warning(
                        "[R5] judge failed for alpha {}: {}",
                        getattr(_a, "alpha_id", "?"), _r5_e
                    )
                    _r1a_log["r5_hook_error"] = str(_r5_e)[:200]
            # === end R5 judge ===

            _r1a_log_rows.append(_r1a_log)

        # Batch INSERT to r1a_attribution_log — independent of alpha persistence.
        # M1 review fix (2026-05-18): per-row savepoint isolation. Previously a
        # single bad row (NOT NULL violation, non-JSON-serializable field,
        # schema drift) failed the whole batch and lost all 50 rows silently.
        # Now each row gets its own SAVEPOINT (begin_nested) so one bad row
        # only rolls back its own savepoint, then we commit the outer txn with
        # whatever survived. _r1a_db_failures counts the dropped rows; an
        # outer-transaction failure (pool exhaustion etc.) counts ALL queued
        # rows as failures so the metric remains conservative.
        if _r1a_log_rows:
            try:
                async with AsyncSessionLocal() as _r1a_db:
                    for _row in _r1a_log_rows:
                        try:
                            async with _r1a_db.begin_nested():
                                _r1a_db.add(R1aAttributionLog(**_row))
                                # flush inside savepoint surfaces row-specific
                                # errors (NOT NULL, type, JSON serialize) HERE
                                # so the outer commit can still succeed for
                                # the other rows.
                                await _r1a_db.flush()
                        except Exception as _r1a_row_e:  # noqa: BLE001
                            _r1a_db_failures += 1
                            logger.error(
                                "[R1a] log row INSERT failed (savepoint rollback, "
                                "alpha_id_brain={}): {}",
                                _row.get("alpha_id_brain"), _r1a_row_e,
                            )
                    await _r1a_db.commit()
            except Exception as _r1a_db_e:  # noqa: BLE001
                # Outer-transaction failure (e.g. pool exhausted, commit fail):
                # everything queued is lost — count all rows we tried to write.
                _r1a_db_failures += len(_r1a_log_rows)
                logger.error(
                    "[R1a] log batch INSERT failed (whole-batch loss, rows={}): {}",
                    len(_r1a_log_rows), _r1a_db_e,
                )

        if _r1a_failures:
            logger.warning(
                "[R1a] {}/{} hook failed this round", _r1a_failures, len(updated_alphas)
            )
        # M1 review fix: end-of-round stats line so log scrapers see DB-side
        # losses even when heuristic-side _r1a_failures is zero. Stash on
        # state too for downstream readers (no metrics_tracker counter wiring
        # exists for R1a today — keep this surgical).
        if _r1a_db_failures:
            logger.error(
                "[R1a] {}/{} log rows lost this round (DB-side, see prior ERROR for cause)",
                _r1a_db_failures, len(_r1a_log_rows),
            )
        try:
            setattr(state, "_r1a_db_failures_last_round", _r1a_db_failures)
        except Exception:  # noqa: BLE001 — state attr-set must never break round
            pass
    # === end R1a + R5 hooks ===

    # === Phase 2 R10 Family-cap (Hubble v2, 2026-05-18) ===
    # Apply AFTER R1a/R5 hooks so per-alpha scores (composite_score / sharpe)
    # are already finalized — family cap ranks by score within each
    # (pillar, family) group. Soft-fail: any error logged but never blocks
    # the round.
    #
    # Phase 4 Sprint 2 B3 (2026-05-20): stamp-only refactor (plan v5 §6.10).
    # Family cap previously set ``quality_status = "FAIL"`` inline here;
    # that double-write conflicted with the R10-v2 (hard-ban) overlay,
    # which needs both stamps to coexist on PASS rows for the 7d 互验 SQL
    # to compute false-positive rates. New flow: stamp only, then the
    # unified finalize pass at end of this function transitions any
    # stamped row to FAIL. ``apply_family_cap`` already only returns
    # drop indices (no inline mutation), so the change is local here.
    if getattr(settings, "ENABLE_FAMILY_CAP", False):
        try:
            from backend.family_classifier import apply_family_cap  # noqa: E402
            _top_k = int(getattr(settings, "FAMILY_CAP_TOP_K", 2))
            _drop_idx = apply_family_cap(updated_alphas, top_k=_top_k)
            if _drop_idx:
                for _i in _drop_idx:
                    _a = updated_alphas[_i]
                    _new_metrics = dict(_a.metrics) if isinstance(_a.metrics, dict) else {}
                    _new_metrics["_r10_family_cap_dropped"] = True
                    _new_metrics["_r10_family_cap_top_k"] = _top_k
                    _a.metrics = _new_metrics
                logger.info(
                    f"[R10] family-cap stamped {len(_drop_idx)}/{len(updated_alphas)} "
                    f"alphas (top_k={_top_k}) — FAIL deferred to finalize pass"
                )
        except Exception as _r10_e:  # noqa: BLE001
            logger.warning(f"[R10] family-cap failed (non-fatal): {_r10_e}")
    # === end R10 family-cap ===

    # === Phase 4 Sprint 2 B3 R10-v2 family hard-ban shadow stamp ===
    # Pairwise PnL-correlation ≥ τ within a family → stamp
    # _r10v2_hard_banned. Shadow mode by design: no FAIL inline.
    # Both R10 and R10-v2 stamps survive to persistence; the互验 SQL
    # in plan v5 §6.10 compares false-positive rates after 7d obs.
    #
    # Tier A upstream wire (2026-05-20): the corr matrix producer. When
    # flag ON + matrix not pre-populated, BUILD it in-round: group the
    # round's non-FAIL alphas by (pillar, family_signature), fetch daily
    # PnL ONLY for same-family members (solo-family alphas can never be
    # banned → no wasted BRAIN cost; most rounds have 0 same-family
    # duplicates → 0 fetches), and compute the pairwise corr matrix.
    # BRAIN_AUTH_CIRCUIT short-circuit + soft-fail inside the service.
    if getattr(settings, "ENABLE_FAMILY_HARD_BAN", False):
        try:
            from backend.family_classifier import (  # noqa: E402
                apply_family_hard_ban,
                same_family_alpha_ids,
            )
            _tau = float(getattr(settings, "FAMILY_BAN_MIN_PAIRWISE_CORR", 0.65))
            _corr_matrix = getattr(state, "r10v2_pnl_corr_matrix", None)

            # Tier A: build the matrix if an upstream producer didn't.
            if _corr_matrix is None and brain is not None:
                _same_family_ids = same_family_alpha_ids(updated_alphas)
                if len(_same_family_ids) >= 2:
                    try:
                        from backend.services.correlation_service import (
                            CorrelationService as _R10v2CorrSvc,
                        )
                        _r10v2_svc = _R10v2CorrSvc(brain)
                        _corr_matrix = await _r10v2_svc.compute_pairwise_corr_for_ids(
                            _same_family_ids,
                        )
                        # Stash on state so re-entry within the same run reuses it
                        try:
                            state.r10v2_pnl_corr_matrix = _corr_matrix
                        except Exception:  # noqa: BLE001
                            pass
                        logger.info(
                            f"[R10-v2] built corr matrix for {len(_same_family_ids)} "
                            f"same-family alpha(s) "
                            f"(matrix={'ok' if _corr_matrix is not None else 'empty'})"
                        )
                    except Exception as _r10v2_build_e:  # noqa: BLE001
                        logger.warning(
                            f"[R10-v2] corr matrix build failed (non-fatal): "
                            f"{_r10v2_build_e}"
                        )
                        _corr_matrix = None
                else:
                    logger.debug(
                        "[R10-v2] no same-family alphas this round — skip corr fetch"
                    )

            if _corr_matrix is not None:
                _ban_idx = apply_family_hard_ban(
                    updated_alphas, pnl_corr_matrix=_corr_matrix, threshold=_tau,
                )
                if _ban_idx:
                    for _i in _ban_idx:
                        _a = updated_alphas[_i]
                        _new_metrics = dict(_a.metrics) if isinstance(_a.metrics, dict) else {}
                        _new_metrics["_r10v2_hard_banned"] = True
                        _new_metrics["_r10v2_hard_ban_threshold"] = _tau
                        _a.metrics = _new_metrics
                    logger.info(
                        f"[R10-v2] hard-ban stamped {len(_ban_idx)}/{len(updated_alphas)} "
                        f"alphas (τ={_tau:.2f}) — FAIL deferred to finalize pass"
                    )
            else:
                logger.debug("[R10-v2] flag ON but no corr matrix — skip ban")
        except Exception as _r10v2_e:  # noqa: BLE001
            logger.warning(f"[R10-v2] hard-ban failed (non-fatal): {_r10v2_e}")
    # === end R10-v2 hard-ban ===

    # === B3 Sprint 2 stamp → FAIL finalize pass (plan v5 §6.10) ===
    # Both R10 family-cap and R10-v2 hard-ban stamp metrics without
    # touching quality_status (per the 互验 design). Here we transition
    # any stamped row to FAIL. Idempotent — rows already FAIL stay FAIL;
    # PASS rows with stamps become FAIL; nothing else changes.
    if updated_alphas:
        _r10_finalize_count = 0
        for _a in updated_alphas:
            _m = _a.metrics if isinstance(_a.metrics, dict) else {}
            if _m.get("_r10_family_cap_dropped") is True or _m.get("_r10v2_hard_banned") is True:
                _current_status = getattr(_a, "quality_status", None)
                _status_str = getattr(_current_status, "value", _current_status)
                if _status_str != "FAIL":
                    _a.quality_status = "FAIL"
                    _r10_finalize_count += 1
        if _r10_finalize_count > 0:
            logger.info(
                f"[R10-finalize] {_r10_finalize_count} alpha(s) transitioned to FAIL "
                f"via stamp scan"
            )
    # === end finalize pass ===

    # === B2 Sprint 2 R13 factor_lens shadow stamp (plan v5 §6.9) ===
    # OLS decompose each PASS alpha's daily PnL against the region's
    # static factor-returns snapshot. shadow mode (default): stamp only;
    # soft mode: residual<τ → PASS_PROVISIONAL (transition inline OK —
    # PROV is reversible). hard mode: stamp `_r13_hard_failed=True`,
    # FAIL transition deferred to the finalize pass below (consistent
    # with B3 stamp-only refactor pattern — F13 review fix).
    # Default OFF → byte-identical round behavior.
    if (
        updated_alphas
        and getattr(settings, "ENABLE_FACTOR_LENS", False)
        and brain is not None
    ):
        try:
            from backend.services import factor_lens_service as _r13_svc
            from backend.services.correlation_service import (
                CorrelationService as _CorrSvc,
                _series_to_returns as _to_returns,
            )
            # F6 review fix: short-circuit when BRAIN auth circuit is open.
            # /alphas/{id}/recordsets/pnl shares the same auth path as
            # simulate; without this check, an expired session burns
            # ~5×60s retry × N PASS alpha = potentially hours of round
            # latency per stamp pass.
            from backend.adapters.brain_adapter import BRAIN_AUTH_CIRCUIT

            _r13_mode = getattr(settings, "FACTOR_LENS_MODE", "shadow")
            _r13_tau = float(getattr(settings, "FACTOR_LENS_RESIDUAL_SHARPE_MIN", 0.5))
            # F5 review fix: `list([] or None)` → `list(None) → TypeError`
            # silent kill. Use explicit truthiness then list(); preserve
            # None semantics so decompose_alpha falls back to DEFAULT_FACTORS.
            _r13_factors_raw = getattr(settings, "FACTOR_LENS_FACTORS", None)
            _r13_factors = list(_r13_factors_raw) if _r13_factors_raw else None
            _r13_min_overlap = int(getattr(settings, "FACTOR_LENS_MIN_OVERLAP_DAYS", 60))
            # D9 review fix: wire FACTOR_LENS_OLS_LOOKBACK_DAYS (was dead config)
            _r13_lookback = int(getattr(settings, "FACTOR_LENS_OLS_LOOKBACK_DAYS", 504))

            # F14 review fix: snapshot availability cache. Probe per-region
            # ONCE per node invocation so we don't pay BRAIN PnL fetch
            # latency for every PASS alpha in a region that has no
            # snapshot (current production state — only README exists).
            _r13_snapshot_probe: Dict[str, bool] = {}
            def _has_snapshot(_region: str) -> bool:
                if _region in _r13_snapshot_probe:
                    return _r13_snapshot_probe[_region]
                _df = _r13_svc.load_factor_returns(_region, factors=_r13_factors)
                _ok = _df is not None and not _df.empty
                _r13_snapshot_probe[_region] = _ok
                if not _ok:
                    logger.info(
                        f"[R13] no snapshot for region={_region} — "
                        f"R13 stamps disabled for this region until snapshot deployed"
                    )
                return _ok

            _corr_svc = _CorrSvc(brain)
            _r13_stamped = 0
            _r13_soft_pp = 0
            _r13_hard_stamped = 0
            _r13_circuit_breaks = 0
            for _a in updated_alphas:
                # F6: re-check the breaker every iteration so a mid-loop
                # auth drop doesn't waste the remaining alphas.
                if BRAIN_AUTH_CIRCUIT.is_open():
                    _r13_circuit_breaks += 1
                    break

                _status = getattr(_a, "quality_status", None)
                _status_str = getattr(_status, "value", _status)
                # Only decompose PASS alphas (FAIL already excluded from
                # downstream submission; decomposition adds no value)
                if _status_str not in ("PASS", "PASS_PROVISIONAL"):
                    continue
                _alpha_id = getattr(_a, "alpha_id", None)
                _region = getattr(_a, "region", None)
                if not _alpha_id or not _region:
                    continue
                # F14: skip BRAIN PnL fetch entirely when snapshot absent
                if not _has_snapshot(_region):
                    continue
                try:
                    _pnl_series = await _corr_svc._fetch_pnl_series(
                        _alpha_id, max_attempts=2,
                    )
                except Exception as _fetch_e:
                    logger.debug(
                        f"[R13] PnL fetch failed for {_alpha_id}: {_fetch_e}"
                    )
                    continue
                if _pnl_series is None or _pnl_series.empty:
                    continue
                _daily_returns = _to_returns(_pnl_series)
                if _daily_returns is None or _daily_returns.empty:
                    continue
                _residual = _r13_svc.decompose_alpha(
                    alpha_returns=_daily_returns,
                    region=_region,
                    factors=_r13_factors,
                    min_overlap_days=_r13_min_overlap,
                    lookback_days=_r13_lookback,
                )
                # F4 review fix: positive filter — only act on real OLS
                # output. Previous filter only excluded 2 of 8 empty-
                # residual reasons (skipped / no_snapshot), letting
                # insufficient_overlap / lstsq_failed / bad_alpha_shape /
                # etc. leak through with residual_sharpe=0.0 < τ=0.5 →
                # spurious PROVISIONAL/FAIL in soft/hard mode.
                if _residual.mode_used != "ols_daily":
                    continue

                _new_m = dict(_a.metrics) if isinstance(_a.metrics, dict) else {}
                _new_m["_r13_residual_sharpe"] = float(_residual.residual_sharpe)
                _new_m["_r13_factor_exposures"] = _residual.factor_exposures
                _new_m["_r13_r_squared"] = float(_residual.r_squared)
                _new_m["_r13_ols_n_days"] = int(_residual.ols_n_days)
                _new_m["_r13_mode_used"] = _residual.mode_used
                _new_m["_r13_factor_lens_phase"] = _r13_mode
                _a.metrics = _new_m
                _r13_stamped += 1

                # F13 review fix: hard-mode transition now goes through
                # the same finalize pass as R10/R10-v2 — stamp only here.
                # PROV transition stays inline (PROV is reversible and
                # doesn't collide with R10 stamps).
                if _r13_mode == "soft" and _residual.residual_sharpe < _r13_tau:
                    if _status_str == "PASS":
                        _a.quality_status = "PASS_PROVISIONAL"
                        _r13_soft_pp += 1
                elif _r13_mode == "hard" and _residual.residual_sharpe < _r13_tau:
                    _new_m["_r13_hard_failed"] = True
                    _new_m["_r13_residual_sharpe_at_fail"] = float(_residual.residual_sharpe)
                    _a.metrics = _new_m
                    _r13_hard_stamped += 1

            if _r13_stamped > 0 or _r13_circuit_breaks > 0:
                logger.info(
                    f"[R13] factor_lens stamped {_r13_stamped} alpha(s) "
                    f"(mode={_r13_mode}, soft_pp={_r13_soft_pp}, "
                    f"hard_stamped={_r13_hard_stamped}, "
                    f"circuit_break={_r13_circuit_breaks})"
                )
        except Exception as _r13_e:  # noqa: BLE001
            # F5 review fix: bump to WARNING so silent kills are visible.
            logger.warning(f"[R13] factor_lens shadow failed (non-fatal): {_r13_e}")
    # === end R13 factor_lens ===

    # === B5 R8-v3 cognitive layer stamp (Sprint 3, 2026-05-20) ===
    # Copy the layer id used at hypothesis time onto each alpha's metrics
    # so the offline bandit reward update (cron, Sprint 3 fast-follow)
    # can attribute PASS/FAIL to the active layer. Soft-fail: when state
    # has no layer id (R8-v3 OFF), nothing happens.
    _cognitive_layer_id = getattr(state, "cognitive_layer_id_used", "") or ""
    if _cognitive_layer_id and updated_alphas:
        for _a in updated_alphas:
            _new_m = dict(_a.metrics) if isinstance(_a.metrics, dict) else {}
            _new_m["_cognitive_layer_used"] = _cognitive_layer_id
            _a.metrics = _new_m
    # === end R8-v3 stamp ===

    # === RAG category-overlap A/B stamp (2026-05-21) ===
    # Copy the round's experiment arm onto every evaluated alpha's metrics so
    # the alphas-table rows (PASS / PROV / OPTIMIZE / FAIL) carry it for
    # scripts/rag_ab_report.py. "" when ENABLE_RAG_CATEGORY_AB OFF → no stamp.
    _rag_ab_arm = getattr(state, "rag_ab_arm", "") or ""
    if _rag_ab_arm and updated_alphas:
        for _a in updated_alphas:
            _new_m = dict(_a.metrics) if isinstance(_a.metrics, dict) else {}
            _new_m["_rag_ab_arm"] = _rag_ab_arm
            _a.metrics = _new_m
    # === end RAG A/B stamp ===

    # === F13 Sprint 2 R13 hard-mode stamp → FAIL finalize (review fix) ===
    # Mirrors the R10/R10-v2 stamp → FAIL pattern: R13 hard mode no longer
    # transitions quality_status inline (avoids the same anti-pattern B3
    # just refactored away from R10). Sweep one more time picking up the
    # `_r13_hard_failed` stamp.
    if updated_alphas:
        _r13_finalize_count = 0
        for _a in updated_alphas:
            _m = _a.metrics if isinstance(_a.metrics, dict) else {}
            if _m.get("_r13_hard_failed") is True:
                _current_status = getattr(_a, "quality_status", None)
                _status_str = getattr(_current_status, "value", _current_status)
                if _status_str != "FAIL":
                    _a.quality_status = "FAIL"
                    _r13_finalize_count += 1
        if _r13_finalize_count > 0:
            logger.info(
                f"[R13-finalize] {_r13_finalize_count} alpha(s) transitioned to FAIL "
                f"via _r13_hard_failed stamp scan"
            )
    # === end R13 finalize pass ===

    # === Phase 4 PR0.6 Sentinel stamp (Sprint 0, 2026-05-19) ===
    # Backfills 3 of the 6 R12-sentinel-tied stamps onto alpha.metrics so the
    # R12 decision counterfactual SQL (Sprint末) can attribute per-sentinel
    # PASS rate margin. Sources of truth already exist:
    #   - R1b mutation: Hypothesis.r1b_mutation_depth ≥ 1
    #   - G8 forest reference: state.g8_forest_referenced_ids (List[int],
    #     set in node_hypothesis after fetch_cross_task_promoted; reset on
    #     every node entry per F-A3 fix)
    #   - R9 cache hit: alpha.metrics["_simulation_cache_hit"]=True (planted
    #     by sim_cache.cached_simulate_batch in result["metrics"] nested dict
    #     which evaluation.py:1267 propagates to alpha.metrics — F-A1 fix)
    # Soft-fail: any error skips stamping; never blocks the round.
    if updated_alphas:
        try:
            _hyp_ids = {
                getattr(a, "hypothesis_id", None) for a in updated_alphas
                if getattr(a, "hypothesis_id", None) is not None
            }
            # F-T1 fix (post-review): SQL lookup extracted to top-level
            # helper _pr06_lookup_mutated_hypothesis_ids so the integration
            # test can drive it with an in-memory aiosqlite session.
            _mutated_hids = await _pr06_lookup_mutated_hypothesis_ids(_hyp_ids)
            _forest_hids: set = set(
                getattr(state, "g8_forest_referenced_ids", None) or []
            )

            _stamp_count = {"r1b": 0, "forest": 0, "cache": 0}
            for _a in updated_alphas:
                try:
                    _m = dict(_a.metrics) if isinstance(_a.metrics, dict) else {}
                    _hid = getattr(_a, "hypothesis_id", None)
                    if _hid is not None and _hid in _mutated_hids:
                        _m["_r1b_mutation_triggered"] = True
                        _stamp_count["r1b"] += 1
                    if _hid is not None and _hid in _forest_hids:
                        _m["_hypothesis_forest_reference"] = True
                        _stamp_count["forest"] += 1
                    # F-A1 fix (2026-05-19 review): sim_cache stamps
                    # `_simulation_cache_hit` directly into result["metrics"]
                    # (the nested dict propagated to alpha.metrics in
                    # node_simulate line ~1267). AlphaCandidate has no
                    # `sim_result` attribute — the only carrier is metrics.
                    if _m.get("_simulation_cache_hit") is True:
                        _stamp_count["cache"] += 1
                    if _m is not _a.metrics:
                        _a.metrics = _m
                except Exception:  # noqa: BLE001
                    pass
            if any(_stamp_count.values()):
                logger.info(
                    "[PR0.6 stamp] r1b=%d forest=%d cache=%d / total=%d",
                    _stamp_count["r1b"], _stamp_count["forest"],
                    _stamp_count["cache"], len(updated_alphas),
                )
        except Exception as _pr06_outer:  # noqa: BLE001
            logger.debug(
                "[PR0.6 stamp] outer block failed (non-fatal): %s", _pr06_outer
            )
    # === end Phase 4 PR0.6 ===

    # === G3 AST Originality Gate (Phase A shadow, 2026-05-19) ===
    # Runs AFTER R10 family-cap (coarse operator-sequence dedup) to catch
    # the "换皮" alphas R10 misses — same AST subtree set, different op
    # pipeline. Phase A defaults to shadow mode: every blocked alpha gets
    # alpha.metrics['_g3_*'] flags but quality_status stays unchanged so
    # operators can validate τ via /ops/g3/originality-stats before
    # promoting to soft (PROVISIONAL) or hard (REJECT).
    # Soft-fail invariant — any exception is swallowed and downgraded to
    # "checker disabled this round" so the evaluation loop never breaks.
    if getattr(settings, "ENABLE_AST_ORIGINALITY_GATE", False) and updated_alphas:
        try:
            from backend.alpha_originality import (  # noqa: E402
                OriginalityChecker,
                apply_to_alpha,
            )

            _g3_checker = OriginalityChecker()
            # History is per-task + per-region. The checker dedupes + caps
            # at history_k internally. Falls back to empty history on DB
            # error, in which case every verdict is "skipped" (safe default).
            await _g3_checker.load_history(
                task_id=getattr(state, "task_id", None),
                region=getattr(state, "region", None),
            )

            _g3_blocked = 0
            _g3_skipped = 0
            _g3_errs = 0
            for _a in updated_alphas:
                # Skip alphas already in a terminal-fail bucket (R10 drop,
                # validation REJECT, etc.) — they won't be persisted /
                # simulated, so G3 numbers would be polluted.
                _status = getattr(_a, "quality_status", None)
                _status_str = getattr(_status, "value", _status) if _status is not None else None
                if _status_str in {"FAIL", "REJECT"}:
                    continue
                try:
                    _verdict = _g3_checker.check(getattr(_a, "expression", "") or "")
                    apply_to_alpha(_a, _verdict)
                    if _verdict.verdict == "blocked":
                        _g3_blocked += 1
                    elif _verdict.verdict == "skipped":
                        _g3_skipped += 1
                except Exception as _g3_inner:  # noqa: BLE001
                    # Per-alpha soft-fail; do not pollute the round
                    _g3_errs += 1
                    logger.debug(
                        "[G3] per-alpha check failed (non-fatal): %s", _g3_inner
                    )

            if _g3_blocked or _g3_errs:
                logger.info(
                    "[G3] mode=%s blocked=%d skipped=%d errs=%d total=%d "
                    "history_k=%d τ=%.4f",
                    _g3_checker.mode,
                    _g3_blocked,
                    _g3_skipped,
                    _g3_errs,
                    len(updated_alphas),
                    _g3_checker.history_k,
                    _g3_checker.threshold,
                )
        except Exception as _g3_outer:  # noqa: BLE001
            logger.warning(f"[G3] originality gate failed (non-fatal): {_g3_outer}")
    # === end G3 ===

    return {
        "pending_alphas": updated_alphas,
        **trace_update
    }
