"""P1 — auto ts_decay_linear(., 4) wrapper for T1 candidates (2026-05-07).

Manual decay sweep on pk=6606 (close-open intraday return) showed:
  - raw: sh=1.58 fit=0.85 to=0.81 → BRAIN reject (HIGH_TURNOVER + LOW_FITNESS)
  - ts_decay_linear(., 4): sh=1.45 fit=1.47 to=0.51 → BRAIN can_submit=true

P1 makes this auto: every T1 ts_op candidate gets a decay=4 wrapped twin.
Doubles BRAIN budget consumption but should multiply PASS+submittable yield.

Tests cover:
  1. Decay flag default-ON produces twin candidates
  2. Decay flag OFF preserves legacy single-candidate behavior
  3. Twin shapes are correctly tiered (raw=T1, decay=T2)
  4. _dedup_and_validate allowed_tiers={1,2} keeps both
  5. _dedup_and_validate without allowed_tiers (legacy) keeps T1 only
"""
from backend.factor_generation import expand_t1_strategy, T1Strategy
from backend.factor_tier_classifier import _dedup_and_validate, classify_tier


def _make_strategy() -> T1Strategy:
    return T1Strategy(
        signal_velocity="MEDIUM",
        window_scale="SHORT",
        preferred_ts_ops=["ts_rank"],  # 1 op
        promising_fields=["close"],     # 1 field
        n_promising_fields=1,
        rationale="test P1 decay wrapper",
        economic_hypothesis="P1 unit test for auto decay variant emission",
    )


class TestDecayCandidateEmission:
    def test_decay_enabled_emits_twin(self, monkeypatch):
        from backend.config import settings
        monkeypatch.setattr(settings, "T1_AUTO_DECAY_WRAPPER", True)
        monkeypatch.setattr(settings, "T1_AUTO_DECAY_VALUE", 4)

        strategy = _make_strategy()
        # daily_goal high enough to keep all candidates (no stratified pruning)
        result = expand_t1_strategy(strategy, daily_goal=100, region="USA",
                                    target_multiplier=2.0)
        exprs = [c["expression"] for c in result]

        # SHORT window scale = [5, 10] from WINDOW_SCALE_MAP. So we should
        # see for each window: 1 raw + 1 decay = 2 candidates × 2 windows = 4.
        # Validator may drop some; at minimum we expect ≥1 raw and ≥1 decay.
        has_raw = any("ts_rank(close, 5)" == e or "ts_rank(close, 10)" == e for e in exprs)
        has_decay = any("ts_decay_linear(ts_rank(close" in e for e in exprs)
        assert has_raw, f"missing raw T1 candidate in {exprs}"
        assert has_decay, f"missing decay-wrapped twin in {exprs}"

    def test_decay_disabled_no_twin(self, monkeypatch):
        from backend.config import settings
        monkeypatch.setattr(settings, "T1_AUTO_DECAY_WRAPPER", False)

        strategy = _make_strategy()
        result = expand_t1_strategy(strategy, daily_goal=100, region="USA",
                                    target_multiplier=2.0)
        exprs = [c["expression"] for c in result]
        has_decay = any("ts_decay_linear(ts_rank" in e for e in exprs)
        assert not has_decay, f"decay flag OFF should not emit twin: {exprs}"

    def test_decay_value_respected(self, monkeypatch):
        from backend.config import settings
        monkeypatch.setattr(settings, "T1_AUTO_DECAY_WRAPPER", True)
        monkeypatch.setattr(settings, "T1_AUTO_DECAY_VALUE", 8)

        strategy = _make_strategy()
        result = expand_t1_strategy(strategy, daily_goal=100, region="USA",
                                    target_multiplier=2.0)
        exprs = [c["expression"] for c in result]
        has_d8 = any("ts_decay_linear(ts_rank" in e and ", 8)" in e for e in exprs)
        assert has_d8, f"decay=8 expected: {exprs}"


class TestDedupValidateAllowedTiers:
    def test_allowed_tiers_default_keeps_only_target(self):
        """Legacy behavior: target_tier=1 → only T1 kept."""
        variants = [
            {"expression": "ts_rank(close, 20)"},  # T1
            {"expression": "ts_decay_linear(ts_rank(close, 20), 4)"},  # T2
        ]
        out = _dedup_and_validate(variants, target_tier=1, region="USA")
        kept = {v["expression"] for v in out}
        assert "ts_rank(close, 20)" in kept
        assert "ts_decay_linear(ts_rank(close, 20), 4)" not in kept

    def test_allowed_tiers_set_keeps_both(self):
        """P1 path: target_tier=1 + allowed_tiers={1,2} → keep both."""
        variants = [
            {"expression": "ts_rank(close, 20)"},  # T1
            {"expression": "ts_decay_linear(ts_rank(close, 20), 4)"},  # T2
        ]
        out = _dedup_and_validate(
            variants, target_tier=1, region="USA",
            allowed_tiers={1, 2},
        )
        kept_exprs = {v["expression"] for v in out}
        assert "ts_rank(close, 20)" in kept_exprs
        assert "ts_decay_linear(ts_rank(close, 20), 4)" in kept_exprs

    def test_factor_tier_field_populated(self):
        """Returned variants carry their actual tier classification."""
        variants = [
            {"expression": "ts_rank(close, 20)", "op": "ts_rank"},
            {"expression": "ts_decay_linear(ts_rank(close, 20), 4)", "op": "decay4_ts_rank"},
        ]
        out = _dedup_and_validate(
            variants, target_tier=1, region="USA",
            allowed_tiers={1, 2},
        )
        by_expr = {v["expression"]: v for v in out}
        assert by_expr["ts_rank(close, 20)"]["factor_tier"] == 1
        assert by_expr["ts_decay_linear(ts_rank(close, 20), 4)"]["factor_tier"] == 2


class TestDecayActualClassification:
    """Sanity: ts_decay_linear-wrapped T1 must actually classify as T2,
    not get rejected as None or accidentally classified as T1."""

    def test_decay_wraps_to_t2(self):
        assert classify_tier("ts_decay_linear(ts_rank(close, 20), 4)") == 2
        assert classify_tier("ts_decay_linear(ts_zscore(returns, 60), 4)") == 2

    def test_raw_t1_unchanged(self):
        assert classify_tier("ts_rank(close, 20)") == 1
        assert classify_tier("ts_zscore(returns, 60)") == 1
