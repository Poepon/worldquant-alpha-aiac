"""Phase 4 Sprint 1 A1.4 — R12 LLM_MODE comparison + GO gate.

Plan: docs/phase4_a_b_plan_v5_2026-05-19.md §6.1 (A1.4)

Final piece of the R12 critical path: a comparison service that
stratifies recent alphas by ``llm_mode_used`` (author / assistant) +
region + assistant_template_id, computes bootstrap effect-size CI on
the PASS rate diff, and applies the plan's GO/NO-GO/PARTIAL rules.

Why bootstrap CI (not KS test)
------------------------------
Per Round v3-B 高风险 #1 review finding: KS test on unpaired sample
sets has no statistical meaning when treatment/control come from
different hypothesis distributions. Bootstrap effect size on the rate
diff is the right tool — it asks "given the observed PASS counts, what
is the 80% CI on the difference of population PASS rates?" Independent
of distributional assumptions.

GO gate rules (per plan v5 §6.1)
--------------------------------
- effect_size_pct_pts > -10  AND  CI[lower, upper] does NOT cross 0
  → GO (assistant ≥ author within rough parity, statistically significant)
- effect_size_pct_pts ≤ -10  OR  upper bound of CI < -0.10
  → NO-GO (assistant significantly worse than author)
- Otherwise → PARTIAL (inconclusive; needs more obs OR per-region split)

All percentages are absolute (e.g. author=1.5%, assistant=1.3% →
effect_size=-0.2pp, NOT relative-20%).

Soft-fail
---------
query_mode_pool returns empty stats on DB error. bootstrap_diff_ci on
empty samples returns an explicit "insufficient_samples" decision.
evaluate_go_gate never raises — always returns a decision dict.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

logger = logging.getLogger("services.llm_mode_comparison")


# Statuses that count as PASS for the rate calculation. Mirrors the rest
# of the codebase (evaluation.py treats PASS_PROVISIONAL as soft-pass,
# but for the GO gate we only count clean PASS).
_PASS_STATUSES = ("PASS",)

# Default GO-gate threshold per plan v5 §6.1 — "assistant ≥ 90% of
# author PASS rate" expressed as percentage-point margin.
_DEFAULT_EFFECT_FLOOR_PCT_PTS = -0.10  # author=1.5% → assistant ≥ 1.35% OK

# Bootstrap parameters — operator can override via service kwargs.
_DEFAULT_BOOTSTRAP_ITER = 1000
_DEFAULT_CI_LEVEL = 0.80


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


async def query_mode_pool(
    db,
    *,
    days: int = 30,
    region: Optional[str] = None,
) -> Dict[str, Any]:
    """Return PASS-rate distribution stratified by (region, llm_mode_used,
    assistant_template_id).

    Reads alpha rows created in the last ``days`` days. Reads
    ``llm_mode_used`` from ``alpha.metrics`` (default "author" when
    absent — preserves pre-A1.1 row semantics). Reads
    ``assistant_template_id`` for the stratified template-level view.

    Result shape::

        {
            "window_days": int,
            "region_filter": Optional[str],
            "total_alphas": int,
            "by_mode": {
                "author":   {"total": int, "pass": int, "rate": float,
                             "sharpe_mean": float, "sharpe_count": int},
                "assistant":{...},
            },
            "by_region_mode": {
                "USA": {"author": {...}, "assistant": {...}},
                ...
            },
            "by_template": {
                "momentum.basic_ts_zscore": {"total": int, "pass": int, ...},
                ...
            },
            "assistant_fallthrough_count": int,  # candidates where template
                                                  # didn't match → reverted to
                                                  # LLM expression
        }

    Soft-fail returns ``{"error": "..."}``.
    """
    from backend.models import Alpha

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, days))
        cutoff_naive = cutoff.replace(tzinfo=None)
        stmt = (
            select(
                Alpha.region,
                Alpha.is_sharpe,
                Alpha.quality_status,
                Alpha.metrics,
            )
            .where(Alpha.created_at >= cutoff_naive)
        )
        if region:
            stmt = stmt.where(Alpha.region == region)
        rows = (await db.execute(stmt)).all()
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[llm_mode_comparison] query failed: %s", ex,
        )
        return {"error": str(ex)[:200]}

    # Python-side aggregation for cross-DB compatibility (JSONB ->>
    # extraction differs Postgres vs SQLite).
    def _make_bucket() -> Dict[str, Any]:
        return {
            "total": 0,
            "pass": 0,
            "rate": 0.0,
            "sharpe_sum": 0.0,
            "sharpe_count": 0,
            "sharpe_mean": 0.0,
        }

    by_mode: Dict[str, Dict[str, Any]] = {"author": _make_bucket(), "assistant": _make_bucket()}
    by_region_mode: Dict[str, Dict[str, Dict[str, Any]]] = {}
    by_template: Dict[str, Dict[str, Any]] = {}
    fallthrough_count = 0
    total_alphas = 0

    for row in rows:
        total_alphas += 1
        # row.metrics could be None / dict / SQLite TEXT
        m = row.metrics if isinstance(row.metrics, dict) else {}
        mode = m.get("llm_mode_used") or "author"
        if mode not in ("author", "assistant"):
            mode = "author"
        template_id = m.get("assistant_template_id")
        fallthrough = bool(m.get("assistant_template_fallthrough"))
        is_pass = (row.quality_status or "").upper() in _PASS_STATUSES
        sharpe = row.is_sharpe
        region_key = row.region or "UNKNOWN"

        # by_mode aggregate
        b = by_mode[mode]
        b["total"] += 1
        if is_pass:
            b["pass"] += 1
        if isinstance(sharpe, (int, float)):
            b["sharpe_sum"] += float(sharpe)
            b["sharpe_count"] += 1

        # by_region_mode aggregate
        by_region_mode.setdefault(region_key, {"author": _make_bucket(), "assistant": _make_bucket()})
        rb = by_region_mode[region_key][mode]
        rb["total"] += 1
        if is_pass:
            rb["pass"] += 1
        if isinstance(sharpe, (int, float)):
            rb["sharpe_sum"] += float(sharpe)
            rb["sharpe_count"] += 1

        # by_template aggregate (only assistant + non-fallthrough)
        if mode == "assistant" and template_id and not fallthrough:
            t = by_template.setdefault(template_id, _make_bucket())
            t["total"] += 1
            if is_pass:
                t["pass"] += 1
            if isinstance(sharpe, (int, float)):
                t["sharpe_sum"] += float(sharpe)
                t["sharpe_count"] += 1

        if mode == "assistant" and fallthrough:
            fallthrough_count += 1

    # Finalize rates + means
    def _finalize(bucket: Dict[str, Any]) -> None:
        if bucket["total"] > 0:
            bucket["rate"] = bucket["pass"] / bucket["total"]
        if bucket["sharpe_count"] > 0:
            bucket["sharpe_mean"] = bucket["sharpe_sum"] / bucket["sharpe_count"]
        bucket.pop("sharpe_sum", None)

    for b in by_mode.values():
        _finalize(b)
    for region_dict in by_region_mode.values():
        for b in region_dict.values():
            _finalize(b)
    for b in by_template.values():
        _finalize(b)

    return {
        "window_days": days,
        "region_filter": region,
        "total_alphas": total_alphas,
        "by_mode": by_mode,
        "by_region_mode": by_region_mode,
        "by_template": by_template,
        "assistant_fallthrough_count": fallthrough_count,
    }


# ---------------------------------------------------------------------------
# Bootstrap effect-size CI
# ---------------------------------------------------------------------------


def bootstrap_diff_ci(
    author_total: int,
    author_pass: int,
    assistant_total: int,
    assistant_pass: int,
    *,
    iterations: int = _DEFAULT_BOOTSTRAP_ITER,
    ci_level: float = _DEFAULT_CI_LEVEL,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Bootstrap CI on the (assistant_rate - author_rate) effect size.

    Args:
      *_total: sample sizes
      *_pass: PASS counts (≤ total)
      iterations: how many bootstrap resamples (default 1000 — sufficient
        for 80% CI; bump to 10k for tighter tails)
      ci_level: confidence level ∈ (0, 1); 0.80 = 80% CI
      seed: optional RNG seed for reproducible tests

    Returns::

        {
            "author_rate": float,
            "assistant_rate": float,
            "effect_pct_pts": float,    # assistant_rate - author_rate
            "ci_lower": float,           # ci_level CI lower (pct-pts)
            "ci_upper": float,           # ci_level CI upper (pct-pts)
            "ci_level": float,
            "iterations": int,
            "insufficient_samples": bool,
        }

    Soft-fail: returns insufficient_samples=True when either pool has 0
    samples. ci_lower/ci_upper are still computed (NaN-safe defaults).
    """
    if author_total <= 0 or assistant_total <= 0:
        return {
            "author_rate": 0.0,
            "assistant_rate": 0.0,
            "effect_pct_pts": 0.0,
            "ci_lower": 0.0,
            "ci_upper": 0.0,
            "ci_level": ci_level,
            "iterations": 0,
            "insufficient_samples": True,
        }

    rng = random.Random(seed) if seed is not None else random.Random()

    author_rate = author_pass / author_total
    assistant_rate = assistant_pass / assistant_total
    effect = assistant_rate - author_rate

    # Bootstrap: build 0/1 outcome arrays per pool, resample with
    # replacement, recompute diff, accumulate.
    # Optimization: we don't need to materialize full arrays — generate
    # Bernoulli resamples by total + rate.
    diffs: List[float] = []
    for _ in range(iterations):
        # Resample author pool: draw author_total Bernoulli(author_rate)
        a_pass = sum(1 for _ in range(author_total) if rng.random() < author_rate)
        b_pass = sum(1 for _ in range(assistant_total) if rng.random() < assistant_rate)
        diffs.append(b_pass / assistant_total - a_pass / author_total)
    diffs.sort()

    alpha = (1.0 - ci_level) / 2.0  # two-tailed
    lo_idx = max(0, int(alpha * iterations))
    hi_idx = min(iterations - 1, int((1.0 - alpha) * iterations))
    ci_lower = diffs[lo_idx]
    ci_upper = diffs[hi_idx]

    return {
        "author_rate": author_rate,
        "assistant_rate": assistant_rate,
        "effect_pct_pts": effect,
        "ci_lower": ci_lower,
        "ci_upper": ci_upper,
        "ci_level": ci_level,
        "iterations": iterations,
        "insufficient_samples": False,
    }


# ---------------------------------------------------------------------------
# GO gate
# ---------------------------------------------------------------------------


def evaluate_go_gate(
    comparison: Dict[str, Any],
    *,
    effect_floor_pct_pts: float = _DEFAULT_EFFECT_FLOOR_PCT_PTS,
    iterations: int = _DEFAULT_BOOTSTRAP_ITER,
    ci_level: float = _DEFAULT_CI_LEVEL,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Decide GO / NO-GO / PARTIAL given the comparison output.

    Decision rules (plan v5 §6.1):
      - INSUFFICIENT (PARTIAL): assistant or author pool too small
      - NO-GO: effect ≤ effect_floor (default -10pp) OR
               upper CI < effect_floor (definitive worse-than-floor)
      - GO:   effect > effect_floor AND
              CI does NOT cross 0 (assistant strictly ≥ author with
              statistical significance)
      - PARTIAL: anything else (inconclusive; needs more obs)

    Returns dict with ``decision`` + the underlying stats + a
    one-sentence ``rationale``.
    """
    if comparison.get("error"):
        return {
            "decision": "ERROR",
            "rationale": f"comparison error: {comparison['error']}",
            "stats": None,
        }

    by_mode = comparison.get("by_mode", {})
    author_b = by_mode.get("author", {})
    assistant_b = by_mode.get("assistant", {})

    ci = bootstrap_diff_ci(
        author_total=int(author_b.get("total", 0) or 0),
        author_pass=int(author_b.get("pass", 0) or 0),
        assistant_total=int(assistant_b.get("total", 0) or 0),
        assistant_pass=int(assistant_b.get("pass", 0) or 0),
        iterations=iterations,
        ci_level=ci_level,
        seed=seed,
    )

    if ci["insufficient_samples"]:
        return {
            "decision": "INSUFFICIENT",
            "rationale": (
                f"insufficient samples — author_n="
                f"{int(author_b.get('total', 0) or 0)}, assistant_n="
                f"{int(assistant_b.get('total', 0) or 0)}; need ≥1 in each"
            ),
            "stats": ci,
            "thresholds": {
                "effect_floor_pct_pts": effect_floor_pct_pts,
            },
        }

    effect = ci["effect_pct_pts"]
    lo = ci["ci_lower"]
    hi = ci["ci_upper"]

    # NO-GO: effect deeply below floor OR CI upper bound below floor
    if effect <= effect_floor_pct_pts or hi < effect_floor_pct_pts:
        decision = "NO-GO"
        rationale = (
            f"assistant underperforms author by {effect:.4f}pp "
            f"(CI=[{lo:.4f}, {hi:.4f}]); floor={effect_floor_pct_pts:.4f}pp"
        )
    # GO: effect above floor AND CI strictly not crossing 0
    elif effect > effect_floor_pct_pts and lo > 0:
        decision = "GO"
        rationale = (
            f"assistant beats author by {effect:.4f}pp with CI=[{lo:.4f}, "
            f"{hi:.4f}] (lower>0, statistically significant); "
            f"floor={effect_floor_pct_pts:.4f}pp respected"
        )
    else:
        decision = "PARTIAL"
        rationale = (
            f"effect={effect:.4f}pp, CI=[{lo:.4f}, {hi:.4f}] — CI "
            f"crosses 0 OR effect within floor-zone; needs more obs"
        )

    return {
        "decision": decision,
        "rationale": rationale,
        "stats": ci,
        "thresholds": {
            "effect_floor_pct_pts": effect_floor_pct_pts,
        },
    }


__all__ = [
    "query_mode_pool",
    "bootstrap_diff_ci",
    "evaluate_go_gate",
]
