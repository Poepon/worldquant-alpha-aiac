"""Field-coverage exploration: field_score + proportional sampling (PR-B core).

The heart of the orthogonal-breadth field bandit. Pure + dependency-free so the
reward + sampling are unit-testable without DB/BRAIN. Consumed by the pool
scheduler (gated ENABLE_FIELD_SCREENING) to pick a TARGET FIELD that steers HG
generation off the ~886 crowded fields toward the under-explored ~89%.

reward (design §0.2, post adversarial-review wf2tanq33):
    field_score = novelty(times_mined) × signal_quality(signal_p90, band_pass_count)

  - novelty       = max(floor, 1/√(times_mined+1))   → untouched (times=0)=1.0,
                    decays as a field saturates (negative feedback to crowding).
  - signal_quality= normalized(signal_p90 × can_submit_rate)  — DENSE, but ×
                    can_submit_rate to kill CONCENTRATED_WEIGHT fool's gold
                    (a field with p90 Sharpe 19.89 but 0/110 can_submit scores ~0).
                    Untouched (no history) → OPTIMISTIC prior (optimism-under-
                    uncertainty) so the 89% gets explored; collapses to observed
                    after K mines → dead fields self-prune.

orthogonality is NOT in the reward (the marginal-to-13-pool just relabels the
positive-feedback loop, review §0.2). De-correlation is enforced downstream as a
self_corr<gate HARD门 on the produced alpha, not as a field reward.
"""
from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Optional, Sequence


# Optimistic prior for an unmined field's signal_quality (mid-high so it gets
# tried; observed value replaces it once mined ≥ a few times → self-pruning).
OPTIMISTIC_SIGNAL = 0.5
# signal_p90 normaliser: a healthy submittable field's p90 IS Sharpe ~1.5; cap
# the ratio at 1 so a fool's-gold spike (19.89) can't dominate via the p90 term
# (the can_submit_rate multiplier is the real fool's-gold guard).
SIGNAL_P90_REF = 1.5


def novelty(times_mined: int, floor: float = 0.05) -> float:
    """UCB-style exploration bonus. 1.0 at times_mined=0, decays ~1/√n, floored."""
    n = max(0, int(times_mined or 0))
    return max(float(floor), 1.0 / math.sqrt(n + 1))


def orthogonality_credible(orthogonality: Optional[float], distinct_alphas: Optional[int],
                           k_orth: int = 4) -> float:
    """Credibility-horizoned orthogonality (PR-C, 致命修法 — design coherent-loop §2.2).

    Orthogonality MUST be in the field reward (not only a downstream self_corr gate),
    else novelty×signal cannot reduce CROWDING — an untouched field that defines the
    same latent factor as pv1 (different data source) scores high on novelty+p90 but
    its alphas self_corr≈0.92 with the pool → bulk-rejected → budget hemorrhage.

    But the submitted pool is tiny (~13, pv1-heavy) → orthogonality from <K_orth field
    alphas is noise. Credibility horizon: an under-sampled field gets the OPTIMISTIC
    prior (1.0 → still explored, not pre-punished); only once distinct_alphas ≥ K_orth
    do we trust the observed orthogonality. ``orthogonality`` None (no self_corr data
    yet) → optimistic too. Clamped [0,1]."""
    n = int(distinct_alphas or 0)
    if orthogonality is None or n < max(1, int(k_orth)):
        return 1.0  # optimistic-under-uncertainty: explore, don't pre-punish
    return max(0.0, min(1.0, float(orthogonality)))


def signal_quality(times_mined: int, signal_p90: Optional[float],
                   band_pass_count: Optional[int]) -> float:
    """Dense quality signal, fool's-gold-guarded.

    Unmined field → OPTIMISTIC prior. Mined → (clamped p90/ref) × can_submit_rate.
    can_submit_rate = band_pass_count / times_mined → a high-p90-but-0-pass field
    (CONCENTRATED_WEIGHT) collapses to ~0, exactly what dataset bandit v6 learned.
    """
    n = max(0, int(times_mined or 0))
    if n == 0 or signal_p90 is None:
        return OPTIMISTIC_SIGNAL
    p90_term = max(0.0, min(1.0, float(signal_p90) / SIGNAL_P90_REF))
    cs_rate = max(0.0, min(1.0, (int(band_pass_count or 0) / n)))
    # blend: even a 0-pass field keeps a tiny floor so it isn't permanently 0
    # (regime may revive it); but it's heavily discounted vs a submitting field.
    return p90_term * (0.05 + 0.95 * cs_rate)


def field_score(cell: Dict[str, Any], *, novelty_floor: float = 0.05,
                k_orth: int = 4) -> float:
    """field_score = novelty × signal_quality × orthogonality_credible (PR-C).

    ``cell`` carries the PR-A ledger columns: times_mined, signal_p90,
    band_pass_count, orthogonality, distinct_alphas. Three orthogonal factors:
      - novelty       → explore (under-mined fields)
      - signal_quality→ exploit individual IS quality (fool's-gold-guarded)
      - orthogonality → exploit PORTFOLIO breadth (de-crowding) — credibility-
        horizoned so a small-pool noisy estimate can't pre-punish a new field.
    Restoring orthogonality HERE (not only as a downstream self_corr gate) is the
    PR-C fatal fix: novelty×signal alone solves coverage, not crowding."""
    nv = novelty(cell.get("times_mined", 0), floor=novelty_floor)
    sq = signal_quality(cell.get("times_mined", 0), cell.get("signal_p90"),
                        cell.get("band_pass_count"))
    oc = orthogonality_credible(cell.get("orthogonality"),
                                cell.get("distinct_alphas"), k_orth=k_orth)
    return nv * sq * oc


def sample_target_field(candidates: Sequence[Dict[str, Any]], *,
                        novelty_floor: float = 0.05, k_orth: int = 4,
                        rng: Optional[random.Random] = None) -> Optional[Dict[str, Any]]:
    """Proportional (GFlowNet/Thompson-style) sample of ONE candidate field ∝
    field_score — NOT argmax (diversity: don't always pick the single top field).

    Each candidate is a dict with field_id + ledger columns. Returns the chosen
    dict (with an added ``_field_score``) or None if empty/all-zero."""
    cands = list(candidates or [])
    if not cands:
        return None
    rng = rng or random.Random()
    scored = [(c, field_score(c, novelty_floor=novelty_floor, k_orth=k_orth)) for c in cands]
    total = sum(s for _, s in scored)
    if total <= 0:
        chosen = rng.choice(cands)
        chosen = dict(chosen); chosen["_field_score"] = 0.0
        return chosen
    r = rng.random() * total
    acc = 0.0
    for c, s in scored:
        acc += s
        if r <= acc:
            out = dict(c); out["_field_score"] = round(s, 4)
            return out
    out = dict(scored[-1][0]); out["_field_score"] = round(scored[-1][1], 4)
    return out
