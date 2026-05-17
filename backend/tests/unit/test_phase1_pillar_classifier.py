"""Phase 1 Q4 + Q5 (2026-05-17) unit tests.

Q4: pillar_classifier Qlib operator alias — LLM-generated alphas或外部 KB
import 可能 emit Qlib-style 大写算子名 (Mean/Std/Rank/Delta/...);
_extract_operators lowercases 后查 OPERATOR_TO_PILLAR,Phase 1 Q4 加 lowercase
alias 让 LLM 写 ``Mean(close, 20)`` 也能正确归 momentum/value。

Q5: Five Pillars × theoretical_anchor — 每个 pillar 加 academic anchor 名单
(FF5 / Carhart / Novy-Marx / BAB 等),为 Phase 1 R8 RAG / 未来 R5 LLM judge
提供锚定 context。
"""
from __future__ import annotations

import pytest

from backend.pillar_classifier import (
    OPERATOR_TO_PILLAR,
    PILLAR_VALUES,
    THEORETICAL_ANCHORS,
    get_theoretical_anchor,
    infer_pillar,
    normalize_pillar,
)


# ---------------------------------------------------------------------------
# Q4 — Qlib operator alias coverage
# ---------------------------------------------------------------------------

class TestQ4QlibOperatorAlias:
    def test_qlib_mean_alias_matches_ts_mean(self):
        assert OPERATOR_TO_PILLAR["mean"] == OPERATOR_TO_PILLAR["ts_mean"]
        assert OPERATOR_TO_PILLAR["mean"] == {"momentum", "value"}

    def test_qlib_std_alias_matches_ts_std_dev(self):
        assert OPERATOR_TO_PILLAR["std"] == OPERATOR_TO_PILLAR["ts_std_dev"]
        assert OPERATOR_TO_PILLAR["std"] == {"volatility"}

    def test_qlib_var_maps_to_volatility(self):
        # Qlib Var ≈ std² semantically (qlib_translator maps Var→ts_std_dev)
        assert OPERATOR_TO_PILLAR["var"] == {"volatility"}

    def test_qlib_delta_alias_matches_ts_delta(self):
        assert OPERATOR_TO_PILLAR["delta"] == OPERATOR_TO_PILLAR["ts_delta"]
        assert OPERATOR_TO_PILLAR["delta"] == {"momentum"}

    def test_qlib_corr_alias_matches_ts_corr(self):
        assert OPERATOR_TO_PILLAR["corr"] == OPERATOR_TO_PILLAR["ts_corr"]
        assert OPERATOR_TO_PILLAR["corr"] == {"quality", "sentiment"}

    def test_qlib_idxmax_idxmin_momentum(self):
        assert OPERATOR_TO_PILLAR["idxmax"] == {"momentum"}
        assert OPERATOR_TO_PILLAR["idxmin"] == {"momentum"}

    def test_qlib_wma_ema_volatility(self):
        # Decay variants — Qlib WMA/EMA map to BRAIN ts_decay_linear/ts_decay_exp,
        # both volatility-aligned in current OPERATOR_TO_PILLAR.
        assert OPERATOR_TO_PILLAR["wma"] == {"volatility"}
        assert OPERATOR_TO_PILLAR["ema"] == {"volatility"}

    def test_qlib_slope_alias_matches_ts_regression(self):
        assert OPERATOR_TO_PILLAR["slope"] == OPERATOR_TO_PILLAR["ts_regression"]
        assert OPERATOR_TO_PILLAR["slope"] == {"quality"}

    def test_qlib_skew_kurt_volatility(self):
        assert OPERATOR_TO_PILLAR["skew"] == {"volatility"}
        assert OPERATOR_TO_PILLAR["kurt"] == {"volatility"}

    def test_qlib_quantile_sentiment(self):
        assert OPERATOR_TO_PILLAR["quantile"] == OPERATOR_TO_PILLAR["ts_quantile"]
        assert OPERATOR_TO_PILLAR["quantile"] == {"sentiment"}

    def test_qlib_ref_neutral(self):
        # ts_delay is neutral in BRAIN; Qlib Ref same semantic
        assert OPERATOR_TO_PILLAR["ref"] == set()

    def test_qlib_alias_full_expression_routing_momentum(self):
        # `Mean(close, 20)` in raw form — _extract_operators lowercases
        # → "mean" hits alias → vote → momentum (or value, both single ops)
        # close field also votes momentum
        pillar = infer_pillar("Mean(close, 20)")
        assert pillar in {"momentum", "value"}

    def test_qlib_alias_full_expression_routing_volatility(self):
        pillar = infer_pillar("Std(returns, 30)")
        assert pillar == "volatility"

    def test_qlib_alias_does_not_override_brain_neutral_zscore(self):
        # BRAIN zscore (cross-sectional) was already in dict as neutral set()
        # Q4 alias for `zscore` would CONFLICT — we explicitly skip it (Qlib
        # ZScore lowercased to "zscore" collides with BRAIN cross-sectional
        # zscore which is operator-neutral). Preserve BRAIN semantics.
        assert OPERATOR_TO_PILLAR["zscore"] == set()
        assert OPERATOR_TO_PILLAR["rank"] == set()  # same reasoning

    def test_legacy_brain_operators_unchanged(self):
        # Q4 must NOT modify existing ts_* entries
        assert OPERATOR_TO_PILLAR["ts_delta"] == {"momentum"}
        assert OPERATOR_TO_PILLAR["ts_std_dev"] == {"volatility"}
        assert OPERATOR_TO_PILLAR["ts_corr"] == {"quality", "sentiment"}

    def test_all_alias_pillars_are_subsets_of_pillar_values(self):
        # Forward-compat: catalog invariant must still hold after Q4 expansion
        _pillars_set = set(PILLAR_VALUES)
        for op, pillars in OPERATOR_TO_PILLAR.items():
            assert pillars <= _pillars_set, (
                f"Q4 alias {op!r} maps to non-pillar values: "
                f"{pillars - _pillars_set}"
            )


# ---------------------------------------------------------------------------
# Q5 — Five Pillars × theoretical_anchor
# ---------------------------------------------------------------------------

class TestQ5TheoreticalAnchor:
    def test_all_pillars_have_anchor_entries(self):
        for pillar in PILLAR_VALUES:
            assert pillar in THEORETICAL_ANCHORS, (
                f"pillar {pillar!r} missing from THEORETICAL_ANCHORS"
            )

    def test_momentum_anchor_includes_carhart(self):
        anchors = get_theoretical_anchor("momentum")
        assert any("Carhart" in a for a in anchors)
        assert any("Jegadeesh" in a for a in anchors)

    def test_value_anchor_includes_fama_french(self):
        anchors = get_theoretical_anchor("value")
        assert any("Fama-French" in a for a in anchors)

    def test_quality_anchor_includes_novy_marx(self):
        anchors = get_theoretical_anchor("quality")
        assert any("Novy-Marx" in a for a in anchors)

    def test_volatility_anchor_includes_bab(self):
        anchors = get_theoretical_anchor("volatility")
        assert any("BAB" in a or "betting against beta" in a.lower() for a in anchors)

    def test_sentiment_anchor_includes_baker_wurgler(self):
        anchors = get_theoretical_anchor("sentiment")
        assert any("Baker" in a and "Wurgler" in a for a in anchors)

    def test_other_pillar_explicit_empty(self):
        # "other" is the explicit "no anchor" bucket — distinct from "missing"
        assert get_theoretical_anchor("other") == []
        assert "other" in THEORETICAL_ANCHORS

    def test_unknown_pillar_returns_empty(self):
        # Distinct from "other" because caller can normalize "other" → []
        # while truly unknown strings also return [] without crash
        assert get_theoretical_anchor("nonexistent_pillar") == []

    def test_none_input_returns_empty(self):
        assert get_theoretical_anchor(None) == []
        assert get_theoretical_anchor("") == []

    def test_alias_input_normalized(self):
        # normalize_pillar maps "mean_reversion" → "momentum",
        # so get_theoretical_anchor("mean_reversion") should return
        # the momentum anchors (not [])
        anchors = get_theoretical_anchor("mean_reversion")
        assert len(anchors) > 0
        assert any("Carhart" in a or "Jegadeesh" in a for a in anchors)

    def test_returned_list_is_copy_not_reference(self):
        # Mutating the returned list must NOT corrupt the static dict
        anchors = get_theoretical_anchor("momentum")
        original_count = len(THEORETICAL_ANCHORS["momentum"])
        anchors.append("INJECTED")
        assert len(THEORETICAL_ANCHORS["momentum"]) == original_count
