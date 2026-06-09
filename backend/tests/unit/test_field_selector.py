"""Unit tests for field_selector (PR-B core reward + sampling)."""
import random

from backend.field_selector import (
    novelty, signal_quality, field_score, sample_target_field,
    orthogonality_credible, OPTIMISTIC_SIGNAL,
)


def test_orthogonality_credible_horizon():
    # observed value always trusted (clamped), regardless of n
    assert orthogonality_credible(0.1, 1, k_orth=4) == 0.1      # observed wins
    assert orthogonality_credible(0.9, 10, k_orth=4) == 0.9
    assert orthogonality_credible(0.1, 10, k_orth=4) == 0.1
    assert orthogonality_credible(1.5, 10, k_orth=4) == 1.0     # clamp
    # None + under-explored → optimistic 1.0 (explore, don't pre-punish new field)
    assert orthogonality_credible(None, 2, k_orth=4) == 1.0
    # None + WELL-mined → unknown_prior 0.5 (review wuw1yxmqd fix: mined but data-
    # starved must NOT get the optimistic boost that masks crowding)
    assert orthogonality_credible(None, 100, k_orth=4) == 0.5
    assert orthogonality_credible(None, 100, k_orth=4, unknown_prior=0.3) == 0.3


def test_field_score_orthogonality_closes_crowding_loop():
    # PR-C fatal fix: two MINED fields (distinct_alphas≥K), same novelty+signal,
    # but one is orthogonal (0.9) and one crowds the pool (0.1). The crowded one
    # must score far lower → stops being re-picked (novelty×signal alone couldn't
    # do this — that was the fatal flaw).
    base = {"times_mined": 8, "signal_p90": 1.4, "band_pass_count": 4, "distinct_alphas": 8}
    orthogonal = dict(base, orthogonality=0.9)
    crowded = dict(base, orthogonality=0.1)
    assert field_score(orthogonal) > 5 * field_score(crowded)


def test_field_score_untouched_still_optimistic_on_ortho():
    # untouched field (distinct_alphas=0) keeps optimistic orthogonality=1.0 → still
    # explored (not pre-punished by a missing/zero orthogonality).
    untouched = {"times_mined": 0, "signal_p90": None, "band_pass_count": 0,
                 "distinct_alphas": 0, "orthogonality": None}
    crowded_mined = {"times_mined": 2000, "signal_p90": 2.0, "band_pass_count": 10,
                     "distinct_alphas": 2000, "orthogonality": 0.1}
    assert field_score(untouched) > field_score(crowded_mined)


def test_novelty_untouched_is_one_decays_floored():
    assert novelty(0) == 1.0
    assert novelty(3) == 0.5
    assert 0 < novelty(10_000) <= 0.05 + 1e-9     # floored
    assert novelty(10_000, floor=0.01) >= 0.01


def test_signal_quality_untouched_optimistic():
    assert signal_quality(0, None, None) == OPTIMISTIC_SIGNAL
    assert signal_quality(5, None, 0) == OPTIMISTIC_SIGNAL   # no p90 → optimistic


def test_signal_quality_fools_gold_collapses():
    # CONCENTRATED_WEIGHT: huge p90 but 0 can_submit → score ~0 (5% floor only).
    fools = signal_quality(110, 19.89, 0)
    real = signal_quality(60, 1.4, 40)
    assert fools < 0.1           # heavily discounted
    assert real > fools          # a genuinely submitting field beats fool's gold


def test_field_score_untouched_beats_crowded_lowyield():
    untouched = {"times_mined": 0, "signal_p90": None, "band_pass_count": 0}
    pv1_like = {"times_mined": 2124, "signal_p90": 2.4, "band_pass_count": 18}  # crowded, low rate
    assert field_score(untouched) > field_score(pv1_like)


def test_sample_proportional_and_deterministic_with_seed():
    cands = [
        {"field_id": "untouched_a", "times_mined": 0, "signal_p90": None, "band_pass_count": 0},
        {"field_id": "crowded_b", "times_mined": 2000, "signal_p90": 2.0, "band_pass_count": 10},
    ]
    rng = random.Random(42)
    picks = [sample_target_field(cands, rng=rng)["field_id"] for _ in range(200)]
    # untouched (much higher score) should dominate but not be the ONLY pick
    n_unt = picks.count("untouched_a")
    assert n_unt > 150            # proportional → mostly the high-score one
    assert n_unt < 200            # but not argmax-deterministic (diversity)


def test_sample_empty_returns_none():
    assert sample_target_field([]) is None
    assert sample_target_field(None) is None
