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
from backend.adapters.brain_adapter import BrainAdapter
from backend.config import settings
from backend.agents.prompts import (
    quick_alignment_check,
    determine_attribution_heuristic,
)
from backend.alpha_routing import route_alpha_action
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


def _check_is_os_consistency(metrics: Dict, tier: Optional[int] = None) -> bool:
    """V-12: reject alphas whose IS sharpe far exceeds OS sharpe.

    Spike (2026-05-02 → 03) revealed train_sharpe values up to 16.2 paired
    with test_sharpe=0 — pure IS overfit. PASS gate must require OS
    consistency for elevated IS sharpe.

    V-26.76 (2026-05-13): `tier` parameter raises the bar for T3, which
    is one step from BRAIN submission. T1 keeps the original ratios
    (exploration tier — false positives cost less than false negatives);
    T2 keeps default; T3 adds a strict 0.5 floor when IS sharpe is
    elevated. Caller threads through `state.factor_tier`.

    Tiered rules:
      - is_sharpe < 2:    no OS check (conservative IS already)
      - 2 <= is_sharpe < 5: require os_sharpe > 0 AND os/is >= ratio_mid
      - is_sharpe >= 5:   require os_sharpe > 0 AND os/is >= ratio_high

    Ratios per tier (mid / high):
      T1: 0.3 / 0.4  (explore — original spike calibration)
      T2: 0.3 / 0.4
      T3: 0.4 / 0.5  (strict — closest to submission gate)

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
    if tier == 3:
        threshold = 0.5 if is_sh >= 5 else 0.4
    else:
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
        self_corr_ok = self_corr < max_correlation
        self_corr_verified = self_corr_source not in (CorrSource.UNKNOWN, "unknown")
    else:
        self_corr_ok = True
        self_corr_verified = True  # tier_skipped, not unknown

    # V-26.76: pass tier so T3 hits the strict 0.4/0.5 floor
    _tier_val = getattr(state, "factor_tier", None)
    is_overfit_safe = _check_is_os_consistency(metrics, tier=_tier_val)

    hard_gate_pass = (
        sharpe >= sharpe_min
        and fitness >= fitness_min
        and turnover_min <= turnover <= turnover_max
        and sub_universe_ok
        and concentrated_ok
        and self_corr_ok
        and self_corr_verified
        and is_overfit_safe
    )

    self_corr_acceptable = self_corr_ok or not self_corr_verified
    near_pass = (
        sharpe >= prov_sharpe_min
        and fitness >= prov_fitness_min
        and prov_turnover_min <= turnover <= prov_turnover_max
        and sub_universe_ok
        and concentrated_ok
        and self_corr_acceptable
    )

    v16_flags = _run_suspicion_checks(metrics, alpha.expression or "")
    hard_v16_flags = [f for f in v16_flags if f.get("severity") == "hard"]
    brain_actionable_fails_list = [
        c.get("name") for c in brain_failed_checks or []
        if c.get("name") in (
            "LOW_FITNESS",
            "LOW_SHARPE",
            "CONCENTRATED_WEIGHT",
            "HIGH_TURNOVER",
            "LOW_TURNOVER",
            "MATCHES_PYRAMID",
            "HIGH_CORRELATION",
            "SELF_CORRELATION",
        )
    ]
    decision = route_alpha_action(
        hard_gate_pass=hard_gate_pass,
        meets_thresholds=meets_thresholds,
        score=score,
        score_pass_threshold=score_pass_threshold,
        has_v16_hard_flags=bool(hard_v16_flags),
        brain_checks_present=bool(brain_eval['check_details']),
        brain_actionable_fails=bool(brain_actionable_fails_list),
        brain_can_submit=brain_can_submit,
        near_pass=near_pass,
        should_optimize=should_opt,
        score_optimize_threshold=score_optimize_threshold,
    )
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

    return _SingleAlphaEvalResult(
        corr_check_performed=_corr_performed,
        corr_check_skipped_reason=_corr_skipped_reason,
    )


# =============================================================================
# NODE: Simulate
# =============================================================================

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
    
    if not valid_indices:
        logger.warning(f"[{node_name}] No valid alphas to simulate")
        return {}
    
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
                }
    except Exception as e:
        logger.warning(f"[{node_name}] portfolio dedup failed, proceeding: {e}")

    # Plan v5+ #3 (2026-05-07): pre-simulate skeleton classifier filter.
    # When ENABLE_PRE_SIMULATE_FILTER=True, predict P(PASS) per candidate
    # and skip very-likely-fails BEFORE sending to BRAIN simulate. Default
    # OFF; opt-in via .env. Conservative threshold 0.05 keeps 99% PASS
    # recall on the training-set CV (AUC=0.813).
    if getattr(settings, "ENABLE_PRE_SIMULATE_FILTER", False):
        try:
            from backend.agents.services.pre_simulate_filter import filter_candidates
            threshold = float(getattr(settings, "PRE_SIMULATE_FILTER_THRESHOLD", 0.05))
            cand_exprs = [state.pending_alphas[i].expression for i in indices_to_simulate]
            keep_local, skip_local, probas = filter_candidates(
                cand_exprs, threshold=threshold,
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
    # etc.) and field category. When enabled, bucket expressions by their
    # chosen settings tuple and call simulate_batch per bucket; results are
    # merged back to original index order.
    smart_enabled = getattr(settings, "ENABLE_SMART_SIM_SETTINGS", False)
    smart_settings_per_idx: Dict[int, Dict] = {}  # local_index → settings dict
    smart_reasons_per_idx: Dict[int, str] = {}

    if smart_enabled:
        from backend.sim_settings import settings_reason, smart_simulation_settings

        SETTINGS_KEYS = ("region", "universe", "delay", "decay", "neutralization", "truncation", "test_period")
        buckets: Dict[Tuple, List[int]] = {}
        for local_i, idx in enumerate(indices_to_simulate):
            expr = state.pending_alphas[idx].expression
            smart = smart_simulation_settings(
                expr,
                tier=getattr(state, "factor_tier", None),
                region=state.region,
                universe=state.universe,
                # P3-Brain: 从 task-startup snapshot 传 test_period(plan §8.4)
                # 避免 Consultant 切换中途 simulate 不同 alpha 用不同 test_period。
                test_period=getattr(state, "effective_default_test_period", None),
            )
            smart_settings_per_idx[local_i] = smart
            smart_reasons_per_idx[local_i] = settings_reason(
                expr, tier=getattr(state, "factor_tier", None)
            )
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
    else:
        try:
            # V-26.65 (2026-05-13): sim defaults pulled from settings.
            results = await brain.simulate_batch(
                expressions=expressions,
                region=state.region,
                universe=state.universe,
                delay=_v16_settings.SIM_DEFAULT_DELAY,
                decay=_v16_settings.SIM_DEFAULT_DECAY,
                neutralization=_v16_settings.SIM_DEFAULT_NEUTRALIZATION,
            )
        except Exception as e:
            logger.error(f"[{node_name}] Batch Simulate Loop Error: {e}")
            results = [{"success": False, "error": str(e)} for _ in expressions]
    
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
        updated.metrics = res.get("metrics", {}) or {}
        updated.simulation_error = res.get("error")

        # P1-E follow-up (M-4 incomplete fix): node_validate stamps
        # `_validation_findings` and `_risk_bounds` into pre-simulate
        # alpha.metrics so persistence (which writes alpha.metrics to
        # JSONB) can carry them to KB. The unconditional `updated.metrics =
        # res.get("metrics")` above DROPS those annotations before
        # persistence ever sees them. Carry them across explicitly.
        # `setdefault` so a (hypothetical) BRAIN metrics dict containing
        # the same key does not get overwritten by stale validation data.
        if isinstance(current.metrics, dict):
            for _k, _v in current.metrics.items():
                if _k.startswith("_validation_") or _k == "_risk_bounds":
                    updated.metrics.setdefault(_k, _v)

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
        if smart_enabled and i in smart_settings_per_idx:
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
                    "delay": _v16_settings.SIM_DEFAULT_DELAY,
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

    # PR2: tier-aware thresholds + gate config. state.factor_tier is set by the
    # router from agent_mode (AUTONOMOUS_TIER1 → 1, AUTONOMOUS_TIER2 → 2,
    # AUTONOMOUS_TIER3 → 3). For legacy AUTONOMOUS, factor_tier defaults to 1
    # via MiningState; setting ENABLE_FACTOR_TIERING=False keeps it on legacy
    # globals via the tier=None fallback inside tier_thresholds.
    from backend.agents.graph.tier_thresholds import get_tier_thresholds

    # BRAIN role-switch (P3-Brain): pass task-startup snapshot to keep running
    # tasks consistent across Consultant flag toggles (avoids re-judging
    # mid-round alphas with new sharpe bar).
    tier_cfg = get_tier_thresholds(
        getattr(state, "factor_tier", None),
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

    sharpe_min = tier_cfg["sharpe_min"]
    fitness_min = tier_cfg["fitness_min"]
    turnover_min = tier_cfg["turnover_min"]
    turnover_max = tier_cfg["turnover_max"]
    max_correlation = tier_cfg.get("self_corr_max") or getattr(settings, "MAX_CORRELATION", 0.7)
    check_self_corr = tier_cfg["check_self_corr"]
    check_concentrated = tier_cfg["check_concentrated"]
    # PROVISIONAL config: tier-specific looser bar for near-PASS pool (KB / island GA seeds).
    prov_cfg = tier_cfg.get("provisional") or {}
    prov_sharpe_min = prov_cfg.get("sharpe_min", sharpe_min)
    prov_fitness_min = prov_cfg.get("fitness_min", 0.6)
    # V-26.80 (2026-05-13): symmetric provisional turnover band. Pre-fix used
    # the regular `turnover_min` as lower bound and `prov_turnover_max` as
    # upper bound, mixing two tiers' policies — a near_pass alpha could
    # have an upper-loose / lower-tight gate that no PASS path explicitly
    # asks for. Default to the same min unless the tier override supplies
    # its own.
    prov_turnover_min = prov_cfg.get("turnover_min", turnover_min)
    prov_turnover_max = prov_cfg.get("turnover_max", 0.85)

    # P0 #3: tier-aware score thresholds. tier_cfg["score_pass/optimize"] are
    # set by get_tier_thresholds() from TIER{N}_SCORE_PASS/OPTIMIZE; their
    # defaults equal the global 0.8/0.3 constants, so behaviour is unchanged
    # until per-tier values are explicitly tuned in .env.
    score_pass_threshold = tier_cfg.get("score_pass", getattr(settings, 'SCORE_PASS_THRESHOLD', 0.8))
    score_optimize_threshold = tier_cfg.get("score_optimize", getattr(settings, 'SCORE_OPTIMIZE_THRESHOLD', 0.3))
    corr_check_threshold = getattr(settings, 'CORR_CHECK_THRESHOLD', 0.5)
    logger.info(
        f"[{node_name}] tier={tier_cfg['tier']} sharpe>={sharpe_min} fitness>={fitness_min} "
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

    # PR5 — T1 sign-flip retry. For each FAIL alpha whose |sharpe| ≥
    # T1_FLIP_RETRY_SHARPE (i.e. a real signal pointing the wrong direction,
    # not just statistical noise), simulate the negated expression and
    # re-evaluate. Bounded by T1_FLIP_RETRY_CAP. Only enabled at T1 because
    # T2/T3 already operate on direction-stable seeds.
    flip_retry_count = 0
    flip_retry_pass = 0
    flip_retry_prov = 0
    if (
        tier_cfg["tier"] == 1
        and brain is not None
        and getattr(settings, "ENABLE_T1_SIGN_FLIP_RETRY", True)
    ):
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

        # A2: flip-retry single-alpha sim → smart settings (zero bucketing cost)
        flip_use_smart = getattr(settings, "ENABLE_SMART_SIM_SETTINGS", False)

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
            for orig in flip_candidates:
                flipped_expr = f"multiply(-1, {orig.expression})"
                try:
                    if flip_use_smart:
                        from backend.sim_settings import smart_simulation_settings
                        smart = smart_simulation_settings(
                            flipped_expr,
                            tier=tier_cfg["tier"],
                            region=state.region,
                            universe=state.universe,
                            # P3-Brain: flip-retry 同 round 内必须保持 test_period
                            # 一致(否则 sharpe 不可比)。从 task snapshot 传。
                            test_period=getattr(state, "effective_default_test_period", None),
                        )
                        _flip_sim_settings = dict(smart)
                        sim_result = await brain.simulate_alpha(
                            expression=flipped_expr,
                            **smart,
                        )
                    else:
                        _flip_sim_settings = {
                            "region": state.region,
                            "universe": state.universe,
                        }
                        sim_result = await brain.simulate_alpha(
                            expression=flipped_expr,
                            region=state.region,
                            universe=state.universe,
                        )
                except Exception as e:
                    logger.warning(f"[{node_name}] flip-retry sim failed: {e}")
                    continue

                if not sim_result.get("success"):
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
        from backend.factor_tier_classifier import derive_control_expression
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
    # never touches quality_status / hard_gate / near_pass / submission gates;
    # the residual is consumed downstream by _identify_optimization_candidates
    # to prioritise the optimization budget. Opt-in via BASELINE_SCREEN_ENABLED.
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

    # === R1a hook (Phase 0 v1.6, 2026-05-17): capture AttributionType ===
    # v1.5 designed to write into alpha.metrics (Pydantic), relying on
    # persistence to push it to DB. But empirically only PROV/PASS alphas
    # INSERT (1/round); FAIL+OPTIMIZE (49/round) get GC'd with their
    # AlphaCandidate. v1.6 fix: ALSO INSERT a dedicated row into
    # r1a_attribution_log per evaluated alpha, captured independent of
    # alpha persistence. 50× better R1a accumulation throughput.
    # Default OFF; flip via FeatureFlagOverride ENABLE_R1A_HOOK=true.
    if getattr(settings, "ENABLE_R1A_HOOK", False):
        # Lazy import: avoid cold-start cost when flag OFF (core/ is 3223 LOC DORMANT)
        from backend.agents.core.integration import enhance_existing_node_evaluate  # noqa: E402
        from backend.database import AsyncSessionLocal  # noqa: E402
        from backend.models.r1a_attribution import R1aAttributionLog  # noqa: E402
        import hashlib  # noqa: E402

        _r1a_failures = 0
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
            try:
                _sim = {
                    "sharpe": _new_metrics.get("sharpe"),
                    "fitness": _new_metrics.get("fitness"),
                }
                _hyp = {"statement": getattr(_a, "hypothesis", "") or ""}
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
            # Runs AFTER R1a heuristic so R5 can OVERWRITE R1a verdict
            # per [V1.0-A2-3] conflict resolution lock. R5 None (both
            # PASS / low conf) preserves R1a verdict.
            # Per-alpha try/except guard — same as R1a, must not block round.
            if getattr(settings, "ENABLE_LLM_JUDGE", False):
                try:
                    from backend.agents.graph.r5_judge import run_r5_judge  # noqa: E402
                    from backend.agents.services.llm_service import get_llm_service  # noqa: E402
                    _r5_payload = await run_r5_judge(
                        hypothesis_statement=_hyp.get("statement", ""),
                        description=getattr(_a, "logic_explanation", "") or "",
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

        # Batch INSERT to r1a_attribution_log — independent of alpha persistence
        if _r1a_log_rows:
            try:
                async with AsyncSessionLocal() as _r1a_db:
                    for _row in _r1a_log_rows:
                        _r1a_db.add(R1aAttributionLog(**_row))
                    await _r1a_db.commit()
            except Exception as _r1a_db_e:  # noqa: BLE001
                logger.warning(
                    "[R1a] log INSERT failed (non-fatal): {}", _r1a_db_e
                )

        if _r1a_failures:
            logger.warning(
                "[R1a] {}/{} hook failed this round", _r1a_failures, len(updated_alphas)
            )
    # === end R1a hook ===

    return {
        "pending_alphas": updated_alphas,
        **trace_update
    }
