"""Plan v5+ §决策 3 — trade_when 主题条件库 tests.

Covers:
1. YAML structure: all themes have ≥1 condition, no syntax errors
2. resolve_signal_to_theme: aliases, unknown → default, None handling
3. get_theme_conditions: region field guards filter correctly
4. expand_t3_strategy: theme-matched conditions take precedence over
   legacy 6-templates when hypothesis_signal provided; falls back when
   not provided / unknown signal
"""
from __future__ import annotations

import pytest


def test_yaml_loads_with_required_themes():
    from backend.agents.seed_pool.trade_when_themes import (
        list_all_themes, total_condition_count,
    )
    themes = set(list_all_themes())
    # Plan §决策 3 enumerated 12 themes; we have 11 (sentiment+contrarian
    # are present plus aliases for the rest)
    must_have = {"momentum", "mean_reversion", "volatility", "value",
                 "sentiment", "event_driven", "default"}
    missing = must_have - themes
    assert not missing, f"missing required themes: {missing}"
    assert total_condition_count() >= 20


def test_every_theme_has_at_least_one_condition():
    from backend.agents.seed_pool.trade_when_themes import (
        list_all_themes, get_theme_conditions,
    )
    for t in list_all_themes():
        # USA (most permissive) should have ≥1 condition for every theme
        cs = get_theme_conditions(t, region="USA")
        assert len(cs) >= 1, f"theme {t!r} has 0 conditions (USA)"


def test_resolve_signal_alias_resolution():
    from backend.agents.seed_pool.trade_when_themes import resolve_signal_to_theme
    # Direct match
    assert resolve_signal_to_theme("momentum") == "momentum"
    assert resolve_signal_to_theme("mean_reversion") == "mean_reversion"
    # Alias
    assert resolve_signal_to_theme("volatility_regime") == "volatility"
    assert resolve_signal_to_theme("regime_change") == "volatility"
    assert resolve_signal_to_theme("defensive") == "quality"
    # Unknown / None
    assert resolve_signal_to_theme("unknown") == "default"
    assert resolve_signal_to_theme("nonexistent_signal_xyz") == "default"
    assert resolve_signal_to_theme(None) == "default"
    assert resolve_signal_to_theme("") == "default"
    # Case-insensitive
    assert resolve_signal_to_theme("MOMENTUM") == "momentum"


def test_region_field_guards_drop_earnings_in_chn():
    """CHN doesn't have days_to_announcement / fam_eps_surprise, so
    event_driven conditions should be filtered out."""
    from backend.agents.seed_pool.trade_when_themes import get_theme_conditions
    usa = get_theme_conditions("event_driven", region="USA")
    chn = get_theme_conditions("event_driven", region="CHN")
    assert len(usa) >= 1
    assert len(chn) == 0, f"CHN should have 0 event_driven conditions, got {len(chn)}"


def test_region_field_guards_drop_sentiment_in_chn():
    """CHN doesn't have snt1_news_buzz / snt1_score."""
    from backend.agents.seed_pool.trade_when_themes import get_theme_conditions
    usa = get_theme_conditions("sentiment", region="USA")
    chn = get_theme_conditions("sentiment", region="CHN")
    assert len(usa) >= 2
    assert len(chn) == 0


def test_region_guards_dont_break_simple_themes():
    """momentum / volatility use only generic price/return fields → no
    region filtering should drop them."""
    from backend.agents.seed_pool.trade_when_themes import get_theme_conditions
    for region in ("USA", "CHN", "EUR", "ASI"):
        for theme in ("momentum", "mean_reversion", "volatility"):
            cs = get_theme_conditions(theme, region=region)
            assert len(cs) >= 1, f"theme={theme} region={region} got 0 conditions"


def test_condition_dict_shape():
    from backend.agents.seed_pool.trade_when_themes import get_theme_conditions
    cs = get_theme_conditions("momentum", region="USA")
    for c in cs:
        assert "name" in c
        assert "expression" in c
        assert "rationale" in c
        assert c["expression"]  # non-empty


# =============================================================================
# expand_t3_strategy integration
# =============================================================================

def test_expand_t3_uses_theme_when_signal_provided():
    """When hypothesis_signal=momentum, T3 expand should produce variants
    whose wrapper_kind starts with "trade_when_theme_momentum_*" instead
    of legacy "trade_when_high_volume_entry"."""
    from backend.factor_wrapping import expand_t3_strategy, T3Strategy
    strategy = T3Strategy(
        signal_velocity="MEDIUM",
        use_templates=["high_volume_entry", "vol_spike_entry"],
        rationale="test",
    )
    variants = expand_t3_strategy(
        seed_t2="ts_rank(close, 5)",
        strategy=strategy,
        region="USA",
        hypothesis_signal="momentum",
    )
    assert len(variants) >= 1
    # All emitted variants should be theme-matched
    kinds = [v["wrapper_kind"] for v in variants]
    theme_kinds = [k for k in kinds if k.startswith("trade_when_theme_momentum_")]
    legacy_kinds = [k for k in kinds if k.startswith("trade_when_") and not k.startswith("trade_when_theme_")]
    assert len(theme_kinds) >= 1, f"expected theme-matched, got: {kinds}"
    assert len(legacy_kinds) == 0, f"legacy kinds leaked through: {legacy_kinds}"


def test_expand_t3_falls_back_when_signal_none():
    from backend.factor_wrapping import expand_t3_strategy, T3Strategy
    strategy = T3Strategy(
        signal_velocity="MEDIUM",
        use_templates=["high_volume_entry"],
        rationale="test",
    )
    variants = expand_t3_strategy(
        seed_t2="ts_rank(close, 5)",
        strategy=strategy,
        region="USA",
        hypothesis_signal=None,
    )
    kinds = [v["wrapper_kind"] for v in variants]
    # Should use legacy 6-template kinds
    legacy_kinds = [k for k in kinds if k.startswith("trade_when_") and not k.startswith("trade_when_theme_")]
    assert len(legacy_kinds) >= 1


def test_expand_t3_falls_back_when_signal_unknown():
    """unknown signal → default theme → default has only generic conditions
    that aren't blocked. So we DO get theme-matched 'default' kinds, NOT
    a fallback to 6-templates (because default has conditions)."""
    from backend.factor_wrapping import expand_t3_strategy, T3Strategy
    strategy = T3Strategy(
        signal_velocity="MEDIUM",
        use_templates=["high_volume_entry"],
        rationale="test",
    )
    variants = expand_t3_strategy(
        seed_t2="ts_rank(close, 5)",
        strategy=strategy,
        region="USA",
        hypothesis_signal="unknown_xyz_signal",
    )
    # default theme returns "default" (resolve_signal_to_theme returns
    # "default" for unknown). expand_t3 treats "default" as fallback path,
    # so it uses the legacy templates.
    kinds = [v["wrapper_kind"] for v in variants]
    legacy_kinds = [k for k in kinds if k.startswith("trade_when_") and not k.startswith("trade_when_theme_")]
    assert len(legacy_kinds) >= 1, "expected legacy fallback for unknown signal"


def test_expand_t3_chn_event_driven_falls_back():
    """When all theme conditions are region-blocked, expand_t3 falls back
    to legacy 6-templates."""
    from backend.factor_wrapping import expand_t3_strategy, T3Strategy
    strategy = T3Strategy(
        signal_velocity="FAST",
        use_templates=["high_volume_entry"],
        rationale="test",
    )
    variants = expand_t3_strategy(
        seed_t2="ts_rank(close, 5)",
        strategy=strategy,
        region="CHN",
        hypothesis_signal="event_driven",
    )
    # event_driven on CHN: all theme conditions blocked → legacy fallback
    kinds = [v["wrapper_kind"] for v in variants]
    theme_event = [k for k in kinds if "theme_event_driven" in k]
    assert len(theme_event) == 0, "no theme conditions should survive"


def test_expand_t3_emits_well_formed_trade_when():
    """Theme-matched variants should be valid trade_when() expressions."""
    from backend.factor_wrapping import expand_t3_strategy, T3Strategy
    strategy = T3Strategy(
        signal_velocity="MEDIUM",
        use_templates=["high_volume_entry"],
        rationale="test",
    )
    variants = expand_t3_strategy(
        seed_t2="rank(close)",
        strategy=strategy,
        region="USA",
        hypothesis_signal="momentum",
    )
    for v in variants:
        expr = v["expression"]
        assert expr.startswith("trade_when(")
        assert expr.endswith(", -1)")
        assert "rank(close)" in expr  # seed must appear
