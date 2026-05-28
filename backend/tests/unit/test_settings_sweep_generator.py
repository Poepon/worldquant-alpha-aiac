"""SettingsSweepGenerator — Stage A grid + dedup + tag invariants.

The generator is Stage A's only generator and the 15621→15720 anchor case
runs through its first variant. Tests pin:

  - Exact 10-cell hand-picked grid order (anchor first)
  - SUBINDUSTRY never appears (dropped per plan §6)
  - Deterministic (same alpha → byte-identical variant list twice in a row)
  - Window axis rewrites the expression only when baseline has a ``, N)``
    numeric window
  - Settings dict has the 7 BRAIN-ready keys
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from backend.services.optimization.generators.settings_sweep import (
    SettingsSweepGenerator,
    _extract_first_window,
    _substitute_first_window,
)


@dataclass
class FakeAlpha:
    """Minimal duck type — generator reads only .expression / settings attrs."""

    expression: str
    region: str = "USA"
    universe: str = "TOP3000"
    delay: int = 1
    truncation: float = 0.08


_15621_EXPR = (
    "group_neutralize(rank(ts_zscore(divide(cashflow_op, enterprise_value), 60)), "
    "industry)"
)
_NO_TS_EXPR = "rank(divide(cashflow_op, enterprise_value))"


# ---------------------------------------------------------------------------
# Window extract / substitute helpers
# ---------------------------------------------------------------------------


def test_extract_first_window_finds_60_inside_ts_zscore():
    assert _extract_first_window(_15621_EXPR) == 60


def test_extract_first_window_returns_none_for_no_ts():
    assert _extract_first_window(_NO_TS_EXPR) is None


def test_substitute_first_window_replaces_60_with_20():
    new_expr = _substitute_first_window(_15621_EXPR, 20)
    assert ", 20)" in new_expr
    assert ", 60)" not in new_expr  # only one window → fully replaced


def test_substitute_first_window_idempotent_when_no_ts():
    assert _substitute_first_window(_NO_TS_EXPR, 20) == _NO_TS_EXPR


# ---------------------------------------------------------------------------
# Generator output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_15621_alpha_generates_10_variants():
    gen = SettingsSweepGenerator()
    alpha = FakeAlpha(expression=_15621_EXPR, delay=0, truncation=0.08)
    variants = await gen.generate(alpha)
    # 15621 has a real ts_window (60) so every grid row produces a unique
    # (expr, settings) pair — full 10 variants survive dedup.
    assert len(variants) == 10


@pytest.mark.asyncio
async def test_first_variant_is_15621_winner_anchor():
    gen = SettingsSweepGenerator()
    alpha = FakeAlpha(expression=_15621_EXPR, delay=0)
    variants = await gen.generate(alpha)
    v0 = variants[0]
    # 15621 winner shape: neut=INDUSTRY, decay=4
    assert v0.settings["neutralization"] == "INDUSTRY"
    assert v0.settings["decay"] == 4
    assert "neut=INDUSTRY" in v0.tag
    assert "decay=4" in v0.tag
    assert v0.generator_name == "settings_sweep"
    assert v0.generation == 0


@pytest.mark.asyncio
async def test_no_variant_uses_subindustry_neut():
    gen = SettingsSweepGenerator()
    alpha = FakeAlpha(expression=_15621_EXPR, delay=0)
    variants = await gen.generate(alpha)
    neuts = {v.settings["neutralization"] for v in variants}
    assert "SUBINDUSTRY" not in neuts
    assert neuts == {"INDUSTRY", "SECTOR"}


@pytest.mark.asyncio
async def test_generator_is_deterministic():
    gen = SettingsSweepGenerator()
    alpha = FakeAlpha(expression=_15621_EXPR, delay=0)
    v1 = await gen.generate(alpha)
    v2 = await gen.generate(alpha)
    # Same order, same expressions, same settings — byte-identical
    assert len(v1) == len(v2)
    for a, b in zip(v1, v2):
        assert a.expression == b.expression
        assert a.settings == b.settings
        assert a.tag == b.tag


@pytest.mark.asyncio
async def test_settings_dict_has_required_brain_keys():
    gen = SettingsSweepGenerator()
    alpha = FakeAlpha(expression=_15621_EXPR, delay=0, region="USA", universe="TOP3000")
    variants = await gen.generate(alpha)
    required = {
        "region", "universe", "delay", "decay",
        "neutralization", "truncation", "test_period",
    }
    for v in variants:
        assert required.issubset(v.settings.keys())
        assert v.settings["region"] == "USA"
        assert v.settings["universe"] == "TOP3000"
        assert v.settings["delay"] == 0  # honors parent alpha's delay


@pytest.mark.asyncio
async def test_window_axis_rewrites_expression_when_baseline_has_window():
    gen = SettingsSweepGenerator()
    alpha = FakeAlpha(expression=_15621_EXPR, delay=0)
    variants = await gen.generate(alpha)
    # Find the variants explicitly tagged with window=
    window_variants = [v for v in variants if "window=" in v.tag]
    # Grid has 3 window overrides {20, 40, 120}, all distinct from baseline 60
    assert len(window_variants) == 3
    for wv in window_variants:
        # Baseline 60 must be replaced
        assert ", 60)" not in wv.expression


@pytest.mark.asyncio
async def test_alpha_without_ts_window_dedups_to_fewer_variants():
    """When the expression has no ts_* window, window-axis variants collapse
    into the same (expression, settings) bucket as the matching decay/neut
    cell — dedup pass should fold them away."""
    gen = SettingsSweepGenerator()
    alpha = FakeAlpha(expression=_NO_TS_EXPR, delay=1)
    variants = await gen.generate(alpha)
    # The 3 window-override rows all use (neut=INDUSTRY, decay=4) — same
    # bucket as row 1. So we lose 3 variants → 10 - 3 = 7.
    assert len(variants) == 7


@pytest.mark.asyncio
async def test_tag_format_is_pipe_delimited():
    gen = SettingsSweepGenerator()
    alpha = FakeAlpha(expression=_15621_EXPR, delay=0)
    variants = await gen.generate(alpha)
    for v in variants:
        assert "|" in v.tag
        # Every tag must include both decay and neut axes
        assert "decay=" in v.tag
        assert "neut=" in v.tag


@pytest.mark.asyncio
async def test_generator_name_attribute_matches_protocol():
    assert SettingsSweepGenerator.name == "settings_sweep"
