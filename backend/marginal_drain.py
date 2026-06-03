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

from typing import Any, Dict, List, Optional, Tuple

try:  # pandas is a hard dep elsewhere; guard only so import never crashes tooling
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None  # type: ignore


# Minimum overlapping observations for a meaningful pairwise correlation —
# mirrors CorrelationService.MIN_OVERLAP_DAYS so the among-set corr is on the
# same footing as the stored self_corr (vs the submitted pool).
MIN_OVERLAP_DAYS = 60


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


def greedy_orthogonal_order(
    candidates: List[Dict[str, Any]],
    pairwise_corr: Dict[Tuple[int, int], float],
    *,
    threshold: float = 0.7,
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

    Determinism: ties on max-corr broken by higher ``score`` then lower ``id``.
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
        # Pick the candidate minimising max-corr-to-selected; tie-break by
        # higher score then lower id (deterministic).
        scored = [
            (max_corr_to_selected(c), -float(c.get("score") or 0.0), int(c["id"]), c)
            for c in remaining
        ]
        scored.sort(key=lambda t: (t[0], t[1], t[2]))
        best_mc, _, _, best = scored[0]
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
