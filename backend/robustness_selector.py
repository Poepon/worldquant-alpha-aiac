"""Per-alpha robustness selector — the OS-survival proxy for which already-
submittable alphas to submit, given BRAIN hides realized OS.

Context (`docs/dev_plan_greenfield_2026-06-07.md`, memory
`reference_brain_os_hidden_is_only`): BRAIN's simulation only ever returns IS;
the realized OS that drives 考核 / consultant comp is architecturally hidden
until post-submission. So the ONLY controllable lever on submission quality is
pre-submit ROBUSTNESS — an alpha whose edge is consistent across sub-periods
(not a lone spike) is likelier to survive the hidden OS than one with the same
full-window Sharpe earned from one lucky stretch.

This is the missing half of the submit selector. The orthogonality / breadth
half already exists (`marginal_drain.greedy_orthogonal_order` +
`pairwise_corr_from_pnl`); this adds the per-alpha robustness dimension:

  - sub-period Sharpe CONSISTENCY (the PoC signal, 2026-06-07): split the local
    ``alpha_pnl`` series into K contiguous sub-periods, Sharpe each — a robust
    alpha is positive across most/all of them; a fragile one earns its full
    Sharpe from one spike and bleeds in the rest.
  - max drawdown (cumulative-PnL trough).
  - a ``[0,1]`` ``robustness_score`` + ``ROBUST`` / ``MODERATE`` / ``FRAGILE``
    verdict.

⚠️ The local ``alpha_pnl`` is the FROZEN IS-backtest window (~2019-2023).
Sub-period consistency WITHIN that window is a legitimate "is this a robust
edge" signal, but it is NOT current-regime validity — a structure robust
2019-2023 can still have decayed now (mLxlen69: submitted IS 2.01 → re-sim IS
−0.74). The ONLINE stage (re-sim on current data) is the decisive regime-decay
check; this OFFLINE stage narrows the field cheaply (zero BRAIN cost) before
spending sim budget.

DSR / PBO / CPCV are documented future enhancements; sub-period consistency is
the MVP. Pure / dependency-light (pandas + stdlib) per the repo's standalone-
analytics-module convention — the router pulls ``alpha_pnl`` rows and feeds them
in, exactly like ``marginal_drain``.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

try:  # pandas is a hard dep elsewhere; guard so import never crashes tooling
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None  # type: ignore


# Trading days/yr for Sharpe annualisation (repo convention — marginal_drain.py:47).
_TRADING_DAYS = 252

# ---- Defaults (overridable per-call; the router passes settings.ROBUSTNESS_*) ----
DEFAULT_SUBPERIODS = 6          # split the PnL window into K sub-periods
DEFAULT_MIN_OVERLAP = 200       # need ≥ this many PnL days to assess (≈ 10 trade-months)
DEFAULT_WORST_REF = 1.0         # maps min-subperiod Sharpe [-ref,+ref] → score [0,1]
DEFAULT_ROBUST_MIN_SUB = -0.1   # ROBUST needs worst sub-period Sharpe ≥ this
DEFAULT_ROBUST_MIN_FRAC = 0.83  # ROBUST needs ≥ this fraction of sub-periods positive (≈5/6)
DEFAULT_FRAGILE_MIN_SUB = -1.0  # FRAGILE if worst sub-period Sharpe ≤ this
DEFAULT_FRAGILE_MAX_FRAC = 0.5  # FRAGILE if < this fraction of sub-periods positive


def _ann_sharpe(vals: List[float]) -> Optional[float]:
    """Annualised Sharpe of a daily-PnL list (mean/std(ddof=0)·√252 — the repo
    convention, marginal_drain.annualized_sharpe). None on < 2 obs or zero vol."""
    n = len(vals)
    if n < 2:
        return None
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n  # population (ddof=0)
    sd = math.sqrt(var)
    if sd <= 0:
        return None
    return mean / sd * math.sqrt(_TRADING_DAYS)


def subperiod_sharpes(daily_pnl: List[float], k: int) -> List[float]:
    """Annualised Sharpe of each of K contiguous, chronological sub-periods.

    The last segment absorbs the remainder so no day is dropped. Segments whose
    Sharpe is unmeasurable (< 2 obs or zero vol) are skipped, not counted — so a
    thin window simply yields fewer sub-periods.
    """
    n = len(daily_pnl)
    k = max(1, int(k))
    # Guard against a window too short to split into k meaningful (≥ ~20-day)
    # segments — degrade k so each segment keeps signal rather than ~1 obs.
    if n < 2 * k:
        k = max(1, n // 20)
    seg = max(1, n // k)
    out: List[float] = []
    for i in range(k):
        chunk = daily_pnl[i * seg:] if i == k - 1 else daily_pnl[i * seg:(i + 1) * seg]
        sh = _ann_sharpe(chunk)
        if sh is not None:
            out.append(sh)
    return out


def max_drawdown(daily_pnl: List[float]) -> float:
    """Max peak-to-trough of the cumulative daily PnL (≤ 0; 0 = monotone-up)."""
    cum = peak = mdd = 0.0
    for x in daily_pnl:
        cum += x
        if cum > peak:
            peak = cum
        dd = cum - peak
        if dd < mdd:
            mdd = dd
    return mdd


def robustness_metrics(
    daily_pnl: List[float],
    *,
    k: int = DEFAULT_SUBPERIODS,
    min_overlap: int = DEFAULT_MIN_OVERLAP,
) -> Optional[Dict[str, Any]]:
    """Sub-period consistency + drawdown for ONE alpha's daily PnL.

    Returns None when the series is too short (< ``min_overlap``), degenerate
    (zero full-window vol), or yields no measurable sub-period.
    """
    if not daily_pnl or len(daily_pnl) < int(min_overlap):
        return None
    full = _ann_sharpe(daily_pnl)
    if full is None:
        return None
    subs = subperiod_sharpes(daily_pnl, k)
    if not subs:
        return None
    n_sub = len(subs)
    n_pos = sum(1 for s in subs if s > 0)
    return {
        "full_sharpe": round(full, 4),
        "subperiod_sharpes": [round(s, 3) for s in subs],
        "min_subperiod_sharpe": round(min(subs), 4),
        "n_subperiods": n_sub,
        "n_positive_subperiods": n_pos,
        "frac_positive_subperiods": round(n_pos / n_sub, 4),
        "max_drawdown": round(max_drawdown(daily_pnl), 6),
    }


def robustness_score(metrics: Dict[str, Any], *, worst_ref: float = DEFAULT_WORST_REF) -> float:
    """``[0,1]`` blend = 0.5·consistency + 0.5·worst-segment-quality.

    ``consistency`` = fraction of positive sub-periods; ``worst`` maps the worst
    sub-period Sharpe from ``[-worst_ref, +worst_ref]`` → ``[0,1]`` (0 at −ref,
    0.5 at 0, 1 at +ref). A consistent, never-deeply-negative alpha scores high.
    """
    consistency = float(metrics.get("frac_positive_subperiods") or 0.0)
    min_sub = float(metrics.get("min_subperiod_sharpe") or 0.0)
    ref = float(worst_ref) if worst_ref and worst_ref > 0 else 1.0
    worst = (min_sub + ref) / (2.0 * ref)
    worst = max(0.0, min(1.0, worst))
    return round(0.5 * consistency + 0.5 * worst, 4)


def robustness_verdict(
    metrics: Dict[str, Any],
    *,
    robust_min_sub: float = DEFAULT_ROBUST_MIN_SUB,
    robust_min_frac: float = DEFAULT_ROBUST_MIN_FRAC,
    fragile_min_sub: float = DEFAULT_FRAGILE_MIN_SUB,
    fragile_max_frac: float = DEFAULT_FRAGILE_MAX_FRAC,
) -> str:
    """``ROBUST`` (consistent, no deep losing stretch) / ``FRAGILE`` (a deeply
    negative sub-period or mostly-losing) / ``MODERATE`` (in between).

    FRAGILE wins ties (conservative — don't crown a fragile alpha ROBUST).
    """
    min_sub = float(metrics.get("min_subperiod_sharpe") or 0.0)
    frac = float(metrics.get("frac_positive_subperiods") or 0.0)
    if min_sub <= float(fragile_min_sub) or frac < float(fragile_max_frac):
        return "FRAGILE"
    if min_sub >= float(robust_min_sub) and frac >= float(robust_min_frac):
        return "ROBUST"
    return "MODERATE"


def assess_from_pnl_rows(
    rows: List[Any],
    *,
    k: int = DEFAULT_SUBPERIODS,
    min_overlap: int = DEFAULT_MIN_OVERLAP,
    worst_ref: float = DEFAULT_WORST_REF,
    robust_min_sub: float = DEFAULT_ROBUST_MIN_SUB,
    robust_min_frac: float = DEFAULT_ROBUST_MIN_FRAC,
    fragile_min_sub: float = DEFAULT_FRAGILE_MIN_SUB,
    fragile_max_frac: float = DEFAULT_FRAGILE_MAX_FRAC,
) -> Dict[int, Dict[str, Any]]:
    """Assess MANY alphas from ``alpha_pnl`` rows in one pass.

    ``rows`` = ``[(alpha_pk, trade_date, daily_pnl), ...]`` — the SAME shape the
    router feeds :func:`marginal_drain.pairwise_corr_from_pnl`, so the endpoint
    reuses the rows it already loaded (zero extra DB cost). Returns
    ``{alpha_pk: {...metrics, robustness_score, robustness_verdict}}`` for every
    alpha with an assessable series. Alphas with no / too-thin PnL are simply
    absent (callers treat missing as "unassessable").
    """
    if pd is None or not rows:
        return {}
    df = pd.DataFrame(rows, columns=["aid", "date", "pnl"])
    if df.empty:
        return {}
    out: Dict[int, Dict[str, Any]] = {}
    for aid, g in df.sort_values("date").groupby("aid"):
        series = [float(x) for x in g["pnl"].tolist() if x is not None]
        m = robustness_metrics(series, k=k, min_overlap=min_overlap)
        if m is None:
            continue
        m["robustness_score"] = robustness_score(m, worst_ref=worst_ref)
        m["robustness_verdict"] = robustness_verdict(
            m,
            robust_min_sub=robust_min_sub,
            robust_min_frac=robust_min_frac,
            fragile_min_sub=fragile_min_sub,
            fragile_max_frac=fragile_max_frac,
        )
        out[int(aid)] = m
    return out
