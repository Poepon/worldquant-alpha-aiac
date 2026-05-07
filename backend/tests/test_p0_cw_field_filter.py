"""P0 — anti-CONCENTRATED_WEIGHT field filter in T1 strategy prompt (2026-05-07).

Symptom: 4 mining batches (276-291) produced 38 PROV+PASS alpha, 0 of which
were BRAIN-submittable. 3/3 PROV in last clean batch (288-291) failed on
CONCENTRATED_WEIGHT. Pattern: low-coverage fields (IV, derivative, bbg,
dvd) drive bare T1 ts_op alpha to concentrated positions because data
exists only on a small subset of the universe.

Fix: filter CW-prone fields out of available_fields BEFORE prompting LLM.
LLM literally can't pick what it doesn't see. Hard floor at 30% retained
(or min 8) prevents empty-pool deadlock when whole dataset is CW-prone.
"""
from backend.agents.prompts.strategy_prompts import build_t1_strategy_user_prompt


def _f(fid: str, cov: float = 0.9, ftype: str = "MATRIX") -> dict:
    return {"id": fid, "type": ftype, "coverage": cov, "description": ""}


class TestCWFieldFilter:
    def test_low_coverage_filtered(self):
        """Fields with coverage < 0.5 dropped from prompt."""
        fields = [
            _f("close", 0.99),
            _f("volume", 0.99),
            _f("returns", 0.99),
            _f("open", 0.99),
            _f("high", 0.99),
            _f("low", 0.99),
            _f("vwap", 0.95),
            _f("amount", 0.95),
            _f("cap", 0.95),
            _f("low_cov_field_a", 0.30),  # ← should drop
            _f("low_cov_field_b", 0.45),  # ← should drop
        ]
        prompt = build_t1_strategy_user_prompt(
            dataset_id="pv1", region="USA", available_fields=fields,
        )
        assert "close" in prompt
        assert "low_cov_field_a" not in prompt, "0.30 coverage field should be filtered"
        assert "low_cov_field_b" not in prompt, "0.45 coverage field should be filtered"

    def test_iv_class_filtered(self):
        """implied_volatility_* fields filtered (CW-magnets)."""
        fields = [
            _f("close", 0.99),
            _f("volume", 0.99),
            _f("returns", 0.99),
            _f("vwap", 0.95),
            _f("cap", 0.95),
            _f("amount", 0.95),
            _f("open", 0.99),
            _f("high", 0.99),
            _f("low", 0.99),
            _f("implied_volatility_call_30", 0.95),  # ← high cov but CW pattern
            _f("implied_volatility_put_30", 0.95),
            _f("implied_volatility_call_180", 0.95),
        ]
        prompt = build_t1_strategy_user_prompt(
            dataset_id="opt1", region="USA", available_fields=fields,
        )
        assert "close" in prompt
        assert "implied_volatility_call_30" not in prompt
        assert "implied_volatility_put_30" not in prompt

    def test_derivative_suffix_filtered(self):
        """*_derivative / *_rank_derivative fields filtered."""
        fields = [
            _f("close", 0.99),
            _f("volume", 0.99),
            _f("returns", 0.99),
            _f("vwap", 0.95),
            _f("cap", 0.95),
            _f("amount", 0.95),
            _f("open", 0.99),
            _f("high", 0.99),
            _f("low", 0.99),
            _f("composite_factor_score_derivative", 0.99),
            _f("analyst_revision_rank_derivative", 0.99),
        ]
        prompt = build_t1_strategy_user_prompt(
            dataset_id="pv1", region="USA", available_fields=fields,
        )
        assert "close" in prompt
        assert "composite_factor_score_derivative" not in prompt
        assert "analyst_revision_rank_derivative" not in prompt

    def test_pv96_bbg_dvd_filtered(self):
        """pv96 bbg dividend fields filtered (account permission issue —
        BRAIN returns 'Invalid data field' for these)."""
        fields = [
            _f("close", 0.99),
            _f("volume", 0.99),
            _f("returns", 0.99),
            _f("vwap", 0.95),
            _f("cap", 0.95),
            _f("amount", 0.95),
            _f("open", 0.99),
            _f("high", 0.99),
            _f("low", 0.99),
            _f("pv96_bbg_dvd_cash_cg_amt", 0.50),
            _f("pv96_bbg_dvd_cash_cpd", 0.50),
        ]
        prompt = build_t1_strategy_user_prompt(
            dataset_id="pv96", region="USA", available_fields=fields,
        )
        assert "close" in prompt
        assert "pv96_bbg_dvd_cash_cg_amt" not in prompt
        assert "pv96_bbg_dvd_cash_cpd" not in prompt

    def test_clean_fields_passthrough(self):
        """Universe of clean fields — no filtering, all pass through."""
        fields = [
            _f("close", 0.99), _f("volume", 0.99), _f("returns", 0.99),
            _f("vwap", 0.95), _f("cap", 0.95), _f("amount", 0.95),
            _f("open", 0.99), _f("high", 0.99), _f("low", 0.99),
        ]
        prompt = build_t1_strategy_user_prompt(
            dataset_id="pv1", region="USA", available_fields=fields,
        )
        for f in fields:
            assert f["id"] in prompt

    def test_deadlock_prevention(self):
        """If filter strips 90%+ of dataset (e.g. opt1 = all IV), fall
        back to raw list rather than emptying the prompt."""
        fields = [
            _f("close", 0.99),  # only 1 clean field
            _f("implied_volatility_call_30", 0.95),
            _f("implied_volatility_call_60", 0.95),
            _f("implied_volatility_call_90", 0.95),
            _f("implied_volatility_call_180", 0.95),
            _f("implied_volatility_put_30", 0.95),
            _f("implied_volatility_put_60", 0.95),
            _f("implied_volatility_put_90", 0.95),
            _f("implied_volatility_put_180", 0.95),
            _f("implied_volatility_call_30d_skew", 0.95),
        ]
        # 1/10 fields clean = 10% — below 30% floor (and below 8 absolute)
        # → should NOT filter (return raw); prompt warning still in system.
        prompt = build_t1_strategy_user_prompt(
            dataset_id="opt1", region="USA", available_fields=fields,
        )
        # Verify some IV fields still surface (deadlock avoided)
        assert "implied_volatility_call_30" in prompt or "implied_volatility_put_30" in prompt

    def test_partial_filter_above_threshold(self):
        """If filter retains >= max(8, 30%) fields, filter applies."""
        # 10 clean + 5 IV = 67% retained (>30%, ≥8) → filter applies
        clean_fields = [_f(n, 0.99) for n in
                        ("close", "volume", "returns", "open", "high", "low",
                         "vwap", "cap", "amount", "industry_returns")]
        iv_fields = [_f(f"implied_volatility_call_{w}", 0.95)
                     for w in (30, 60, 90, 180, 360)]
        fields = clean_fields + iv_fields
        prompt = build_t1_strategy_user_prompt(
            dataset_id="mixed", region="USA", available_fields=fields,
        )
        # Clean fields present
        assert "close" in prompt
        assert "industry_returns" in prompt
        # IV fields filtered
        assert "implied_volatility_call_30" not in prompt
        assert "implied_volatility_call_180" not in prompt
