"""Tests for backend.sim_settings.smart_simulation_settings."""
from __future__ import annotations

from backend.sim_settings import settings_reason, smart_simulation_settings


class TestT1Defaults:
    """T1 raw ts_op signals — no special handling, defaults apply."""

    def test_simple_ts_rank(self):
        s = smart_simulation_settings("ts_rank(close, 20)", tier=1)
        assert s["neutralization"] == "SUBINDUSTRY"
        assert s["decay"] == 4
        assert s["delay"] == 1
        assert s["truncation"] == 0.08
        assert s["test_period"] == "P2Y0M"

    def test_ts_zscore_no_change(self):
        s = smart_simulation_settings("ts_zscore(returns, 5)", tier=1)
        assert s["neutralization"] == "SUBINDUSTRY"
        assert s["decay"] == 4

    def test_passes_region_universe(self):
        s = smart_simulation_settings("ts_rank(close, 20)", tier=1,
                                       region="CHN", universe="TOP2000U")
        assert s["region"] == "CHN"
        assert s["universe"] == "TOP2000U"


class TestT2GroupNeutralizationOverride:
    """T2 wrappers that already neutralize — disable BRAIN neutralization."""

    def test_group_neutralize_industry(self):
        s = smart_simulation_settings(
            "group_neutralize(ts_rank(close, 20), industry)", tier=2
        )
        assert s["neutralization"] == "NONE"
        # decay still default (no trade_when, no field hint)
        assert s["decay"] == 4

    def test_group_demean(self):
        s = smart_simulation_settings(
            "group_demean(ts_zscore(returns, 5), sector)", tier=2
        )
        assert s["neutralization"] == "NONE"

    def test_group_zscore(self):
        s = smart_simulation_settings(
            "group_zscore(ts_rank(close, 20), market)", tier=2
        )
        assert s["neutralization"] == "NONE"

    def test_group_rank(self):
        s = smart_simulation_settings(
            "group_rank(ts_zscore(returns, 5), industry)", tier=2
        )
        assert s["neutralization"] == "NONE"

    def test_group_normalize(self):
        s = smart_simulation_settings(
            "group_normalize(ts_mean(volume, 20), sector)", tier=2
        )
        assert s["neutralization"] == "NONE"


class TestT2NonGroupWrappers:
    """T2 wrappers that DON'T neutralize — keep BRAIN SUBINDUSTRY default."""

    def test_pure_xs_rank(self):
        s = smart_simulation_settings("rank(ts_rank(close, 20))", tier=2)
        assert s["neutralization"] == "SUBINDUSTRY"

    def test_pure_xs_zscore(self):
        s = smart_simulation_settings("zscore(ts_zscore(returns, 5))", tier=2)
        assert s["neutralization"] == "SUBINDUSTRY"

    def test_winsorize(self):
        s = smart_simulation_settings(
            "winsorize(ts_zscore(returns, 5), std=4)", tier=2
        )
        assert s["neutralization"] == "SUBINDUSTRY"

    def test_smoothing_ts_decay_linear(self):
        s = smart_simulation_settings(
            "ts_decay_linear(ts_rank(close, 5), 10)", tier=2
        )
        assert s["neutralization"] == "SUBINDUSTRY"


class TestT3TradeWhen:
    """T3 trade_when entry filter — decay=0, may also disable neut."""

    def test_trade_when_alone(self):
        s = smart_simulation_settings(
            "trade_when(volume > ts_mean(volume, 240), ts_rank(close, 20), -1)",
            tier=3,
        )
        assert s["decay"] == 0
        # Inner is plain ts_rank (T1) → leave SUBINDUSTRY
        assert s["neutralization"] == "SUBINDUSTRY"

    def test_trade_when_wrapping_group_neutralize(self):
        s = smart_simulation_settings(
            "trade_when(volume > ts_mean(volume, 240), "
            "group_neutralize(ts_rank(close, 20), industry), -1)",
            tier=3,
        )
        assert s["decay"] == 0
        assert s["neutralization"] == "NONE"

    def test_trade_when_wrapping_pure_xs(self):
        s = smart_simulation_settings(
            "trade_when(volume > ts_mean(volume, 240), "
            "rank(ts_rank(close, 20)), -1)",
            tier=3,
        )
        assert s["decay"] == 0
        # Inner is rank — pure xs, doesn't intrinsic-neut
        assert s["neutralization"] == "SUBINDUSTRY"


class TestNegationTransparency:
    """multiply(-1, X) is sign-flip — settings should look through it."""

    def test_negated_t1(self):
        s = smart_simulation_settings("multiply(-1, ts_rank(close, 20))", tier=1)
        assert s["neutralization"] == "SUBINDUSTRY"
        assert s["decay"] == 4

    def test_negated_group_neutralize(self):
        # The negation is transparent; inner group_neutralize → NONE
        s = smart_simulation_settings(
            "multiply(-1, group_neutralize(ts_rank(close, 20), industry))", tier=2
        )
        assert s["neutralization"] == "NONE"

    def test_negated_via_subtract_zero(self):
        s = smart_simulation_settings(
            "subtract(0, group_demean(ts_rank(close, 20), industry))", tier=2
        )
        assert s["neutralization"] == "NONE"


class TestFieldCategoryDecayHint:
    """Field metadata can adjust decay independent of structure."""

    def test_fundamental_uses_high_decay(self):
        s = smart_simulation_settings(
            "ts_rank(fnd6_balance_sheet_xxx, 60)", tier=1, field_category="fundamental"
        )
        assert s["decay"] == 32

    def test_pv_uses_zero_decay(self):
        s = smart_simulation_settings(
            "ts_delta(volume, 1)", tier=1, field_category="pv"
        )
        assert s["decay"] == 0

    def test_unknown_category_keeps_default(self):
        s = smart_simulation_settings(
            "ts_rank(close, 20)", tier=1, field_category="weird_category"
        )
        assert s["decay"] == 4

    def test_field_category_does_not_override_structural_neut(self):
        # group_neutralize → NONE wins regardless of field category
        s = smart_simulation_settings(
            "group_neutralize(ts_rank(fnd6_xxx, 60), industry)",
            tier=2, field_category="fundamental",
        )
        assert s["neutralization"] == "NONE"
        # But decay still gets the field hint
        assert s["decay"] == 32


class TestOverridesWinLast:
    """Caller-supplied overrides take final precedence."""

    def test_override_decay(self):
        s = smart_simulation_settings(
            "ts_rank(close, 20)", tier=1, overrides={"decay": 99}
        )
        assert s["decay"] == 99

    def test_override_neutralization(self):
        s = smart_simulation_settings(
            "group_neutralize(ts_rank(close, 20), industry)",
            tier=2, overrides={"neutralization": "MARKET"},
        )
        # Override beats structural NONE
        assert s["neutralization"] == "MARKET"

    def test_override_truncation(self):
        s = smart_simulation_settings(
            "ts_rank(close, 20)", tier=1, overrides={"truncation": 0.15}
        )
        assert s["truncation"] == 0.15


class TestSettingsReason:
    """settings_reason produces a human-readable trail for telemetry."""

    def test_group_neutralize_reason(self):
        r = settings_reason(
            "group_neutralize(ts_rank(close, 20), industry)", tier=2
        )
        assert "group_neutralize" in r
        assert "neut=NONE" in r

    def test_trade_when_reason(self):
        r = settings_reason(
            "trade_when(volume > ts_mean(volume, 240), ts_rank(close, 20), -1)",
            tier=3,
        )
        assert "trade_when" in r
        assert "decay=0" in r

    def test_negation_reason(self):
        r = settings_reason(
            "multiply(-1, group_neutralize(ts_rank(close, 20), industry))",
            tier=2,
        )
        assert "negation-transparent" in r

    def test_default_reason_when_no_special_form(self):
        r = settings_reason("ts_rank(close, 20)", tier=1)
        assert "tier=1 defaults" in r
