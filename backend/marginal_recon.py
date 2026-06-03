"""Marginal-value reconciliation (methodology-audit P0, 2026-06-03).

The audit's single most load-bearing gap: the platform routes/ranks alphas by an
OFFLINE marginal ΔSharpe (``marginal_drain.marginal_delta_sharpe`` against a local
12-alpha OS-backtest pool) that has NEVER been reconciled against an authoritative
marginal-value signal. Without that reconciliation the whole "optimize for
marginal portfolio value" first principle is unsupported.

DATA REALITY (verified 2026-06-03): the truly-live realized ground truth is
structurally UNAVAILABLE today — BRAIN ``before-and-after-performance`` returns 400
for already-SUBMITTED alphas, and local PnL ends at the 2019-2023 OS-backtest
window (no live post-submission PnL). The best obtainable authoritative signal is
BRAIN ``before-and-after-performance`` for CAN_SUBMIT (not-yet-submitted) alphas —
BRAIN's own merge-marginal on the REAL submitted portfolio. So the runnable
validity test is: does our cheap OFFLINE local-pool ΔSharpe AGREE (in sign + rank)
with BRAIN's authoritative before-and-after ΔSharpe? This is NECESSARY-not-
sufficient (both are backtest-merge estimates, not live-realized) — the live loop
needs months of post-submission PnL — but a low agreement here already falsifies
the offline proxy.

This module is the pure statistic. ``sign_agreement_stats`` is the kill-switch:
per the audit, sign-agreement ≤ 60% (≈ coin flip) over ≥ ~15 pairs ⇒ the offline
ΔSharpe is NOT a valid proxy ⇒ STOP using it to rank/route.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore


# Audit kill-switch threshold: sign-agreement at/below this is no better than a
# coin flip ⇒ the offline ΔSharpe is not predictive of the authoritative marginal.
KILL_SIGN_AGREEMENT = 0.60
MIN_PAIRS_FOR_VERDICT = 15      # below this the verdict is "insufficient sample"

# Verdicts under which the offline ΔSharpe SIGN is trustworthy enough to ROUTE on
# (i.e. to split candidates into additive/dilutive tiers). FAIL-CLOSED: only an
# AFFIRMATIVELY non-falsified verdict (≥ MIN_PAIRS_FOR_VERDICT pairs AND > 60%
# agreement) qualifies. ``insufficient_sample`` (too few pairs to validate) and
# ``FALSIFIED`` (≈ coin flip) both fall back to pure breadth — routing on a sign
# we have NOT validated against BRAIN is exactly the mistake the audit flagged.
_SIGN_ROUTABLE_VERDICTS = frozenset({"supported", "weak"})


def route_on_sign_verdict(verdict: Optional[str]) -> bool:
    """Whether the offline ΔSharpe sign is validated enough to route the drain on.

    FAIL-CLOSED: True only for an affirmative ``supported``/``weak`` verdict.
    ``insufficient_sample`` (no evidence the sign tracks BRAIN) and ``FALSIFIED``
    (coin flip) → False → caller falls back to breadth-only ordering.
    """
    return verdict in _SIGN_ROUTABLE_VERDICTS


def _spearman(xs: List[float], ys: List[float]) -> Optional[float]:
    """Spearman rank correlation (pure; ties via average rank). None if < 3 pts
    or numpy missing or a side is constant."""
    if np is None or len(xs) < 3:
        return None

    def _rank(vals: List[float]):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(vals):
            j = i
            while j + 1 < len(vals) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0  # 1-based average rank over the tie group
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks

    rx, ry = _rank(xs), _rank(ys)
    ax, ay = np.asarray(rx), np.asarray(ry)
    if ax.std() == 0 or ay.std() == 0:
        return None
    return float(np.corrcoef(ax, ay)[0, 1])


def sign_agreement_stats(
    pairs: List[Tuple[Optional[float], Optional[float]]],
    *,
    eps: float = 1e-9,
) -> Dict[str, Any]:
    """Compare predicted (offline ΔSharpe) vs authoritative (BRAIN before-after
    ΔSharpe). ``pairs`` = [(predicted, authoritative), ...].

    Returns the audit kill-switch payload: n, sign_agreement_rate, spearman,
    verdict. Sign agreement counts a pair as agreeing when both have the SAME
    sign (both >0 or both <0); near-zero values (|v|<eps) are dropped from the
    sign test (their sign is meaningless) but kept for the rank correlation.
    """
    clean = [
        (float(p), float(a)) for p, a in pairs
        if p is not None and a is not None
        and not math.isnan(float(p)) and not math.isnan(float(a))
    ]
    n = len(clean)
    out: Dict[str, Any] = {
        "n_pairs": n,
        "sign_agreement_rate": None,
        "n_sign_compared": 0,
        "spearman": None,
        "verdict": "insufficient_sample",
        "kill_threshold": KILL_SIGN_AGREEMENT,
    }
    if n == 0:
        return out

    signed = [(p, a) for p, a in clean if abs(p) > eps and abs(a) > eps]
    if signed:
        agree = sum(1 for p, a in signed if (p > 0) == (a > 0))
        out["n_sign_compared"] = len(signed)
        out["sign_agreement_rate"] = round(agree / len(signed), 4)

    out["spearman"] = (
        round(s, 4) if (s := _spearman([p for p, _ in clean], [a for _, a in clean])) is not None
        else None
    )

    rate = out["sign_agreement_rate"]
    if out["n_sign_compared"] < MIN_PAIRS_FOR_VERDICT or rate is None:
        out["verdict"] = "insufficient_sample"
    elif rate <= KILL_SIGN_AGREEMENT:
        out["verdict"] = "FALSIFIED"   # offline ΔSharpe ≈ coin flip → stop routing on it
    elif rate < 0.70:
        out["verdict"] = "weak"
    else:
        out["verdict"] = "supported"
    return out
