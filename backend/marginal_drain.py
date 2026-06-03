"""Set-level orthogonal backlog drain (2026-06-03 P0-1).

The methodology review + industry survey
(`docs/industry_alpha_optimization_survey_2026-06-03.md`, L3) found the platform
is EXECUTION-limited: ~67 already-submittable clean alphas sit unsubmitted while
only ~12 were ever submitted. The fix is to actually DRAIN that backlog — but to
maximise portfolio BREADTH (Grinold-Kahn: IR ≈ IC·√BR counts *independent* bets,
effective breadth ≤ 1/ρ), submissions should be ordered so each adds the most
*incremental* orthogonality, not just the highest individual score.

This module is the pure algorithm:

  - ``pairwise_corr_from_pnl`` builds the among-backlog daily-PnL correlation
    matrix from local ``alpha_pnl`` rows (zero BRAIN cost).
  - ``greedy_orthogonal_order`` is a farthest-point greedy: starting from the
    already-submitted pool (seeded via each candidate's stored self_corr), it
    repeatedly picks the candidate whose MAX correlation to the
    already-selected set is LOWEST, stopping when even the most-orthogonal
    remaining one breaches ``threshold`` (everything left is correlation-blocked).

Dependency-light (pandas only, no DB/HTTP) so it is unit-testable per the repo's
standalone-analytics-module convention. The router pulls the rows + stored
self_corr + marginal score and feeds them in.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

try:  # pandas is a hard dep elsewhere; guard only so import never crashes tooling
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None  # type: ignore

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore


# Minimum overlapping observations for a meaningful pairwise correlation —
# mirrors CorrelationService.MIN_OVERLAP_DAYS so the among-set corr is on the
# same footing as the stored self_corr (vs the submitted pool).
MIN_OVERLAP_DAYS = 60

# Trading days/yr for Sharpe annualisation (repo convention — qlib_prescreen.py:294).
_TRADING_DAYS = 252

# Methodology-audit hardening (2026-06-03): the marginal-selection ΔSharpe is a
# noisy, in-sample, multiply-tested signal — the audit measured SE(ΔSharpe)≈0.08,
# LARGER than the ±0.01–0.025 values it was routed on. So before using ΔSharpe as
# a ranking signal we (a) block-bootstrap its SE and require |ΔSharpe| > k·SE
# (one-sided 1.64 ≈ 90%) — else treat it as indistinguishable from 0; and (b)
# deflate it for the candidate-set size N (expected-max-ΔSharpe under the null,
# Bailey-López de Prado), exactly as settings-sweep Sharpe is deflated.
_SIGNIFICANCE_K = 1.64        # one-sided; |ΔSharpe| must exceed k·bootstrap-SE
_BOOT_BLOCK_DAYS = 20         # circular-block-bootstrap block length (≈1 trade-month)


def _key(a: int, b: int) -> Tuple[int, int]:
    return (a, b) if a <= b else (b, a)


def pairwise_corr_from_pnl(
    rows: List[Tuple[int, Any, float]],
    *,
    min_overlap: int = MIN_OVERLAP_DAYS,
) -> Dict[Tuple[int, int], float]:
    """Build a symmetric pairwise daily-PnL correlation map from alpha_pnl rows.

    ``rows`` = ``[(alpha_pk, trade_date, pnl_daily), ...]`` (the ``pnl`` column
    is the per-day value; correlation of daily PnL is the "do they move
    together" measure). Returns ``{(min_id, max_id): corr}`` for every pair with
    ≥ ``min_overlap`` overlapping days and a non-NaN correlation. Pairs not in
    the map are simply unmeasured (callers treat missing as 0 = orthogonal).
    """
    if pd is None or not rows:
        return {}
    df = pd.DataFrame(rows, columns=["aid", "date", "pnl"])
    if df.empty:
        return {}
    # Wide matrix: rows = trade_date, cols = alpha_pk, values = daily pnl.
    wide = df.pivot_table(index="date", columns="aid", values="pnl", aggfunc="last")
    if wide.shape[1] < 2:
        return {}
    corr = wide.corr(min_periods=int(min_overlap))
    out: Dict[Tuple[int, int], float] = {}
    cols = list(corr.columns)
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            v = corr.iat[i, j]
            if v is None or pd.isna(v):
                continue
            out[_key(int(cols[i]), int(cols[j]))] = float(v)
    return out


# ---------------------------------------------------------------------------
# Combination layer (P1 L2, 2026-06-03) — marginal ΔSharpe to the submitted pool.
#
# The industry survey (docs/industry_alpha_optimization_survey_2026-06-03.md, L2)
# found the platform has NO combination layer — it gates/submits single alphas.
# AlphaForge's entire gain is in dynamically combining; the offline analogue is:
# does adding this candidate to the already-submitted portfolio IMPROVE the
# combined Sharpe? That ΔSharpe is a quality×breadth signal (a low-corr,
# positive-return alpha lifts combined Sharpe even if its standalone Sharpe is
# modest — exactly Grinold-Kahn's breadth point). Zero BRAIN cost: both the pool
# and the candidates come from the local alpha_pnl table.
#
# Weighting: equal-VOLATILITY (each member normalised to unit daily-vol before
# summing) so book-size differences don't dominate — equal-risk = the right
# breadth framing. NOTE: PnL is the OS-backtest window (~2019-2023), so ΔSharpe
# is an OS-window estimate (pool + candidate share the same window → self-
# consistent), NOT a live OOS number.
# ---------------------------------------------------------------------------


def annualized_sharpe(
    daily_returns: Optional["pd.Series"], *, min_obs: int = MIN_OVERLAP_DAYS
) -> Optional[float]:
    """Annualised Sharpe of a daily-return series (mean/std(ddof=0)·√252 — the
    repo convention, qlib_prescreen.py:294). None when < ``min_obs`` obs or
    zero/degenerate volatility."""
    if pd is None or daily_returns is None:
        return None
    s = daily_returns.dropna()
    if len(s) < min_obs:
        return None
    sd = float(s.std(ddof=0))
    if sd <= 0:
        return None
    return float(s.mean() / sd * math.sqrt(_TRADING_DAYS))


def build_pool_returns(
    rows: List[Tuple[int, Any, float]], *, equal_vol: bool = True
) -> Optional["pd.Series"]:
    """Combine alpha_pnl daily-PnL rows ``[(alpha_pk, trade_date, daily_pnl)]``
    into ONE pool daily-return series (the submitted-pool base portfolio).

    ``equal_vol=True`` normalises each member to unit daily-vol before summing
    (equal-risk — removes book-size artifacts); ``False`` sums raw PnL
    (equal-book). Returns None if no usable member.
    """
    if pd is None or not rows:
        return None
    df = pd.DataFrame(rows, columns=["aid", "date", "pnl"])
    wide = df.pivot_table(index="date", columns="aid", values="pnl", aggfunc="last")
    if wide.shape[1] < 1:
        return None
    # Restrict to the common date window (all members present) BEFORE summing, so
    # the pool's volatility reflects market behaviour — not membership changes.
    # Without this, partial-member dates inject heteroskedasticity into every
    # ΔSharpe once submitted alphas have non-identical PnL windows (review fix).
    wide = wide.dropna(how="any")
    if wide.empty:
        return None
    if equal_vol:
        stds = wide.std(ddof=0)
        cols = [c for c in wide.columns if stds.get(c, 0.0) and float(stds[c]) > 0]
        if not cols:
            return None
        wide = wide[cols] / stds[cols]
    pool = wide.sum(axis=1)
    return pool if not pool.empty else None


def marginal_delta_sharpe(
    pool_returns: Optional["pd.Series"],
    candidate_daily: Optional["pd.Series"],
    *,
    equal_vol: bool = True,
    min_overlap: int = MIN_OVERLAP_DAYS,
) -> Optional[float]:
    """Sharpe(pool + candidate) − Sharpe(pool), on overlapping dates.

    >0 → the candidate IMPROVES the combined portfolio (worth submitting for
    breadth); <0 → it dilutes. ``equal_vol`` normalises the candidate to unit
    daily-vol before adding (matches the pool's equal-risk members). None on thin
    overlap / degenerate vol.
    """
    if pd is None or pool_returns is None or candidate_daily is None:
        return None
    cand = candidate_daily.dropna()
    if equal_vol:
        csd = float(cand.std(ddof=0)) if len(cand) else 0.0
        if csd <= 0:
            return None
        cand = cand / csd
    aligned = pd.concat(
        [pool_returns.rename("pool"), cand.rename("cand")], axis=1
    ).dropna()
    if len(aligned) < min_overlap:
        return None
    base = annualized_sharpe(aligned["pool"], min_obs=min_overlap)
    combined = annualized_sharpe(aligned["pool"] + aligned["cand"], min_obs=min_overlap)
    if base is None or combined is None:
        return None
    return round(combined - base, 4)


def _ann_sharpe_np(arr) -> float:
    """Annualised Sharpe on a numpy array (NaN if degenerate). Used by the
    bootstrap inner loop — avoids pandas overhead per resample."""
    sd = float(arr.std(ddof=0))
    if sd <= 0 or arr.size == 0:
        return float("nan")
    return float(arr.mean() / sd * math.sqrt(_TRADING_DAYS))


def bootstrap_delta_sharpe_se(
    pool_returns: Optional["pd.Series"],
    candidate_daily: Optional["pd.Series"],
    *,
    equal_vol: bool = True,
    n_boot: int = 200,
    block: int = _BOOT_BLOCK_DAYS,
    min_overlap: int = MIN_OVERLAP_DAYS,
    seed: int = 12345,
) -> Optional[float]:
    """Circular-block-bootstrap standard error of the marginal ΔSharpe.

    The audit's core finding: ΔSharpe on a 12-alpha ~4yr OS window has SE ≈ 0.08,
    bigger than the values it routed on — so a point ΔSharpe is meaningless without
    its noise floor. Block bootstrap (block ≈ 1 trading-month) preserves the daily
    autocorrelation; returns the SD of the resampled ΔSharpe distribution. None on
    thin overlap / missing numpy.
    """
    if pd is None or np is None or pool_returns is None or candidate_daily is None:
        return None
    cand = candidate_daily.dropna()
    if equal_vol:
        csd = float(cand.std(ddof=0)) if len(cand) else 0.0
        if csd <= 0:
            return None
        cand = cand / csd
    aligned = pd.concat(
        [pool_returns.rename("pool"), cand.rename("cand")], axis=1
    ).dropna()
    n = len(aligned)
    if n < min_overlap:
        return None
    p = aligned["pool"].to_numpy()
    c = aligned["cand"].to_numpy()
    rng = np.random.default_rng(seed)
    n_blocks = int(math.ceil(n / block))
    deltas = []
    for _ in range(int(n_boot)):
        starts = rng.integers(0, n, size=n_blocks)
        idx = (starts[:, None] + np.arange(block)[None, :]).ravel()[:n] % n
        pb = p[idx]
        sb = _ann_sharpe_np(pb)
        sc = _ann_sharpe_np(pb + c[idx])
        if not (math.isnan(sb) or math.isnan(sc)):
            deltas.append(sc - sb)
    if len(deltas) < 10:
        return None
    return float(np.std(np.asarray(deltas), ddof=1))


def deflated_delta_sharpe_threshold(deltas: List[Optional[float]]) -> float:
    """Expected-max ΔSharpe under the null across N candidates (the selection
    deflation the audit said was missing). Reuses the same SR0 formula the
    RobustnessFilter applies to settings-sweep winners — a candidate must beat the
    ΔSharpe the LUCKIEST of N zero-skill candidates would post given their spread.
    Returns 0.0 for N<2 / zero variance. (Lazy import to avoid module coupling.)"""
    try:
        from backend.services.optimization.robustness import expected_max_sharpe
    except Exception:  # pragma: no cover
        return 0.0
    return expected_max_sharpe([d for d in deltas if d is not None])


def is_delta_sharpe_significant(
    delta: Optional[float], se: Optional[float], *, k: float = _SIGNIFICANCE_K
) -> bool:
    """True only when |ΔSharpe| exceeds k·SE (distinguishable from noise) AND the
    SE was measurable. An insignificant ΔSharpe must NOT be used as a hard routing
    signal — it degrades to a soft prior (the candidate ranks by breadth instead)."""
    if delta is None or se is None or se <= 0:
        return False
    return abs(float(delta)) > k * float(se)


def sign_value_tier(delta: Optional[float], has_pnl: bool, *, eps: float = 1e-9) -> int:
    """Map a candidate's offline marginal ΔSharpe to a SIGN-based drain tier.

    Lower tier = drained earlier. The MAGNITUDE is noise (within its bootstrap SE),
    so only the SIGN is used, and only after the caller has validated it against
    BRAIN (see ``marginal_recon.route_on_sign_verdict``):
      0 = additive  (ΔSharpe > 0 — improves the pool, drain first)
      1 = neutral   (measurable but ΔSharpe ≈ 0 or unknown)
      2 = dilutive  (ΔSharpe < 0 — hurts the pool, drain LAST)
      3 = unmeasurable (no local PnL — among-set corr can't be verified)
    Within a tier the greedy orders by breadth (max-corr to the selected set).
    """
    if not has_pnl:
        return 3
    if delta is None:
        return 1
    if delta > eps:
        return 0
    if delta < -eps:
        return 2
    return 1


def greedy_orthogonal_order(
    candidates: List[Dict[str, Any]],
    pairwise_corr: Dict[Tuple[int, int], float],
    *,
    threshold: float = 0.7,
    objective: str = "breadth",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Greedy farthest-point ordering that maximises incremental breadth.

    ``candidates``: each dict MUST have ``id`` (int) and SHOULD have
    ``self_corr`` (max corr to the already-submitted pool; None → 0 = treated
    as orthogonal-to-submitted/unmeasured) and ``score`` (marginal/composite
    tiebreak, higher = better; default 0). Extra keys pass through untouched.

    ``pairwise_corr``: symmetric ``{(min_id,max_id): corr}`` among candidates
    (e.g. from :func:`pairwise_corr_from_pnl`). Missing pair → 0 (unmeasured).

    Returns ``(ordered, blocked)``:
      - ``ordered`` — candidates in greedy submit order, each annotated with
        ``rank`` (1-based) and ``max_corr_to_selected`` (the correlation that
        gated its pick; lower = more breadth added).
      - ``blocked`` — candidates that could not be added below ``threshold``
        (their most-orthogonal achievable max-corr already ≥ threshold),
        annotated with ``max_corr_to_selected`` at stop time.

    ``objective``:
      - ``"breadth"`` (default, P0-1): pick the candidate MINIMISING max-corr to
        the selected set (pure farthest-point); ``score`` is only a tiebreak.
      - ``"value"``: breadth becomes a HARD CONSTRAINT — among candidates still
        below ``threshold``, pick the HIGHEST ``score`` (= marginal ΔSharpe), so
        the order submits the most portfolio-improving alpha that still adds
        breadth (quality×breadth). Candidates with no ``score`` (no PnL → ΔSharpe
        unmeasurable AND among-set corr unmeasurable) are ordered LAST by breadth
        — which also fixes a breadth-mode weakness where they falsely rank first
        on max_corr=0.

    Determinism: explicit id tiebreak throughout.
    """
    def corr(a: int, b: int) -> float:
        return pairwise_corr.get(_key(a, b), 0.0)

    remaining = [dict(c) for c in candidates]
    selected_ids: List[int] = []
    ordered: List[Dict[str, Any]] = []

    def max_corr_to_selected(c: Dict[str, Any]) -> float:
        base = c.get("self_corr")
        m = float(base) if base is not None else 0.0
        cid = int(c["id"])
        for sid in selected_ids:
            v = corr(cid, sid)
            if v > m:
                m = v
        return m

    while remaining:
        metrics = [(c, max_corr_to_selected(c)) for c in remaining]
        if objective == "value":
            # Breadth as a hard constraint; ΔSharpe (score) as the objective.
            admissible = [(c, mc) for c, mc in metrics if mc < threshold]
            if not admissible:
                break  # everything left is correlation-blocked
            def _vkey(t):
                c, mc = t
                # SIGN-based tiering: the offline ΔSharpe per-candidate MAGNITUDE
                # is within its bootstrap noise floor, but its SIGN is reconciled
                # against BRAIN's authoritative marginal by the caller (the drain
                # endpoint measures the live sign-agreement each call and only
                # supplies value_tier while that verdict != FALSIFIED). So rank by
                # the validated SIGN tier (additive > neutral > dilutive >
                # unmeasurable), then by breadth; never on the noisy magnitude.
                # Falls back to the legacy significant-magnitude ordering when
                # value_tier isn't supplied.
                if "value_tier" in c:
                    return (int(c["value_tier"]), mc, int(c["id"]))
                sv = c.get("score")
                has = sv is not None
                measurable = bool(c.get("measurable", True))
                tier = 0 if has else (1 if measurable else 2)
                return (tier, -(float(sv) if has else 0.0), mc, int(c["id"]))
            admissible.sort(key=_vkey)
            best, best_mc = admissible[0]
        else:
            # breadth: minimise max-corr; tiebreak higher score then lower id.
            metrics.sort(
                key=lambda t: (t[1], -float(t[0].get("score") or 0.0), int(t[0]["id"]))
            )
            best, best_mc = metrics[0]
            if best_mc >= threshold:
                break  # everything left is correlation-blocked
        best["rank"] = len(ordered) + 1
        best["max_corr_to_selected"] = round(best_mc, 4)
        ordered.append(best)
        selected_ids.append(int(best["id"]))
        remaining = [c for c in remaining if int(c["id"]) != int(best["id"])]

    blocked: List[Dict[str, Any]] = []
    for c in remaining:
        c["max_corr_to_selected"] = round(max_corr_to_selected(c), 4)
        blocked.append(c)
    # Surface the most-submittable-but-blocked first.
    blocked.sort(key=lambda c: (c["max_corr_to_selected"], -float(c.get("score") or 0.0)))

    return ordered, blocked
