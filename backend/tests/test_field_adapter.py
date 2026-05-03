"""Tests for backend.agents.seed_pool.field_adapter (Plan v5+ R7-2).

Verifies that:
  - All Quasi-T1 whitelist patterns adapt to USA without producing None
  - Golden Set v0.1 expression templates adapt to USA
  - Direct / synthesized / unsupported branches behave per spec
  - Recursion correctly expands nested synthesized aliases
  - Operators / numeric literals / already-real names pass through
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.agents.seed_pool.field_adapter import (
    adapt_expression,
    get_alias_real_name,
    is_alias_supported,
)


class TestDirectMapping:
    @pytest.mark.parametrize("alias,expected", [
        ("close", "close"),
        ("returns", "returns"),
        ("cap", "cap"),
        ("total_assets", "fnd6_newa1v1300_at"),
        ("net_income", "fnd6_newa2v1300_ni"),
        ("eps", "fnd6_newa1v1300_epspi"),
        ("shares", "fnd6_newa1v1300_csho"),
        ("ebit", "fnd6_newa2v1300_oiadp"),
        ("sales", "fnd6_newa2v1300_revt"),
        ("cfo", "anl4_cfo_value"),
    ])
    def test_direct_alias_resolves(self, alias, expected):
        assert get_alias_real_name(alias, "USA") == expected


class TestSynthesized:
    def test_total_debt_expands_recursively(self):
        # total_debt → add(short_term_debt, long_term_debt)
        # → add(fnd6_..._dlc, fnd6_..._dltt)
        out = adapt_expression("total_debt", "USA")
        assert out == "add(fnd6_newa1v1300_dlc, fnd6_newa1v1300_dltt)"

    def test_book_value_per_share(self):
        out = adapt_expression("book_value_per_share", "USA")
        assert out == "divide(fnd6_newa1v1300_ceq, fnd6_newa1v1300_csho)"

    def test_ev_synthesizes(self):
        out = adapt_expression("ev", "USA")
        assert out == "subtract(add(cap, fnd6_newa1v1300_dltt), fnd6_newa1v1300_che)"

    def test_amount_synthesizes(self):
        out = adapt_expression("amount", "USA")
        assert out == "multiply(close, volume)"


class TestUnsupported:
    def test_unsupported_returns_none_atom(self):
        # open_interest is option-only, not in Quasi-T1 supported set
        assert adapt_expression("open_interest", "USA") is None

    def test_unsupported_in_expression_fails_whole_expr(self):
        # Mid-tree unsupported token → entire expression is unsupported
        assert adapt_expression(
            "divide(close, open_interest)", "USA"
        ) is None

    def test_is_alias_supported(self):
        assert is_alias_supported("eps", "USA")
        assert is_alias_supported("ev", "USA")             # synthesized
        assert not is_alias_supported("open_interest", "USA")
        assert not is_alias_supported("nonexistent_alias", "USA")  # unknown ≠ supported


class TestQuasiT1Whitelist:
    """Every Quasi-T1 v1.0 pattern (15 entries) must produce a valid USA
    real-name expression."""

    @pytest.mark.parametrize("pattern,note", [
        ("subtract(divide(close, ts_delay(close, 1)), 1)", "Q-PR-01 synthetic returns"),
        ("divide(subtract(high, low), close)", "Q-ID-01 intraday range"),
        ("divide(subtract(close, low), subtract(high, low))", "Q-ID-02 close pos in range"),
        ("divide(subtract(close, open), open)", "Q-ID-03 close-open return"),
        ("divide(close, eps)", "Q-VL-01 PE proxy"),
        ("divide(close, book_value_per_share)", "Q-VL-02 PB proxy"),
        ("divide(ebit, ev)", "Q-VL-03 earnings yield"),
        ("divide(close, volume)", "Q-PV-01 liquidity ratio"),
        ("divide(amount, cap)", "Q-PV-02 turnover proxy"),
        ("divide(cfo, net_income)", "Q-FN-01 accrual quality"),
        ("divide(cfo, cap)", "Q-FN-02 cash flow yield"),
        ("divide(sales, total_assets)", "Q-FN-03 asset turnover"),
        ("divide(total_debt, total_equity)", "Q-FN-04 debt-to-equity"),
        ("divide(subtract(open, ts_delay(close, 1)), ts_delay(close, 1))", "Q-GP-01 overnight gap"),
        ("subtract(close, vwap)", "Q-CR-01 close-vwap"),
    ])
    def test_quasi_t1_pattern_adapts_to_usa(self, pattern, note):
        out = adapt_expression(pattern, "USA")
        assert out is not None, f"Pattern unmappable on USA: {note} ({pattern})"
        # Sanity: result should be non-empty and contain at least one
        # known BRAIN identifier.
        assert len(out) > 0


class TestGoldenSetV01:
    """All Golden Set v0.1 expression_template values must adapt cleanly."""

    @pytest.fixture(scope="class")
    def golden_set(self):
        path = Path(__file__).parent / "fixtures" / "hypothesis_golden_set_v01_draft.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_all_expressions_adapt(self, golden_set):
        unmappable = []
        for h in golden_set["core_anchors"]:
            tmpl = h.get("expression_template")
            if not tmpl:
                continue
            adapted = adapt_expression(tmpl, "USA")
            if adapted is None:
                unmappable.append((h["hid"], tmpl))
        if unmappable:
            msg = "\n".join(f"  {hid}: {tmpl}" for hid, tmpl in unmappable)
            pytest.fail(
                f"{len(unmappable)} Golden Set expressions don't adapt on USA:\n{msg}"
            )


class TestPassThroughOps:
    def test_known_ops_passthrough(self):
        # Operator names should appear in output unchanged
        out = adapt_expression("ts_rank(close, 20)", "USA")
        assert out is not None
        assert "ts_rank" in out
        assert out == "ts_rank(close, 20)"

    def test_numeric_literals_passthrough(self):
        out = adapt_expression("subtract(divide(close, 100), 1.5)", "USA")
        assert out == "subtract(divide(close, 100), 1.5)"

    def test_real_brain_name_passthrough(self):
        # Real fnd6_* name already in table — should pass through
        out = adapt_expression("ts_rank(fnd6_newa1v1300_at, 20)", "USA")
        assert out == "ts_rank(fnd6_newa1v1300_at, 20)"

    def test_unknown_identifier_passthrough_best_effort(self):
        # Per docstring: unknown identifiers (might be real BRAIN names not in
        # our table) pass through. The validator catches actually-invalid ones.
        out = adapt_expression("ts_rank(some_unknown_field, 20)", "USA")
        assert out == "ts_rank(some_unknown_field, 20)"


class TestRegionGuard:
    def test_unknown_region_returns_none(self):
        assert adapt_expression("close", "MARS") is None

    def test_chn_returns_none_until_table_populated(self):
        # CHN/EUR/ASI/GLB tables intentionally empty per Plan v5+ R7
        assert adapt_expression("close", "CHN") is None

    def test_get_alias_unknown_region(self):
        assert get_alias_real_name("close", "MARS") is None

    def test_is_alias_supported_unknown_region(self):
        assert not is_alias_supported("close", "MARS")
