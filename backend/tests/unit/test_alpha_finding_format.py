"""Unit tests for P1-E structured-finding format.

Covers:
    - `Finding` dataclass round-trip + S-8 from_dict KeyError-safe
    - `SemanticValidationResult` derived properties (errors/warnings/error_messages)
    - 5-rule catalog (empty_expression / unknown_operator / field_not_found /
      low_coverage_field / type_mismatch_vector_ts)
    - 4-rule risk-bound inference (R1 divide-by-volatile-denom / R2 high-exp
      signed_power / R3 short-decay momentum / R4 extreme winsorize std)
    - M-6 paren-walk verification on nested expressions
    - `_aggregate_risk_bounds` max-loss-hint / rationale / confidence /
      severity_distribution

Run with: pytest backend/tests/unit/test_alpha_finding_format.py -v
"""

import pytest

from backend.alpha_semantic_validator import (
    AlphaSemanticValidator,
    Finding,
    RuleId,
    SemanticValidationResult,
    _aggregate_risk_bounds,
    _infer_risk_findings,
    _walk_call_args,
)


# =============================================================================
# 1) Finding dataclass round-trip & S-8 from_dict tolerance
# =============================================================================


class TestFindingDataclass:
    def test_to_from_dict_roundtrip(self):
        f = Finding(
            rule_id="risk_divide_by_volatile_denom",
            severity="info",
            message="msg",
            category="risk",
            location="divide(_, …volume…)",
            metadata={"denom_field": "volume"},
        )
        d = f.to_dict()
        f2 = Finding.from_dict(d)
        assert f2 is not None
        assert f2.rule_id == f.rule_id
        assert f2.severity == f.severity
        assert f2.message == f.message
        assert f2.category == f.category
        assert f2.location == f.location
        assert f2.metadata == f.metadata

    def test_default_category_semantics(self):
        f = Finding(rule_id="other", severity="info", message="x")
        assert f.category == "semantics"
        assert f.location is None
        assert f.metadata == {}

    def test_from_dict_skip_when_no_rule_id(self):
        # S-8: legacy KB rows without rule_id must return None, not raise.
        assert Finding.from_dict({}) is None
        assert Finding.from_dict({"severity": "hard"}) is None
        # Non-dict input — defensive.
        assert Finding.from_dict(None) is None  # type: ignore[arg-type]
        # Missing severity defaults to "info" (S-8).
        f = Finding.from_dict({"rule_id": "x"})
        assert f is not None
        assert f.severity == "info"
        assert f.message == ""
        assert f.category == "semantics"


# =============================================================================
# 2) SemanticValidationResult derived properties
# =============================================================================


class TestSemanticValidationResultProperties:
    def test_errors_returns_only_hard_findings(self):
        r = SemanticValidationResult()
        r._emit_finding(rule_id="x", severity="hard", message="a")
        r._emit_finding(rule_id="y", severity="soft", message="b")
        r._emit_finding(rule_id="z", severity="info", message="c")
        assert len(r.errors) == 1
        assert r.errors[0].rule_id == "x"

    def test_warnings_returns_soft_plus_info(self):
        r = SemanticValidationResult()
        r._emit_finding(rule_id="x", severity="hard", message="a")
        r._emit_finding(rule_id="y", severity="soft", message="b")
        r._emit_finding(rule_id="z", severity="info", message="c")
        warnings = r.warnings
        assert len(warnings) == 2
        rule_ids = {f.rule_id for f in warnings}
        assert rule_ids == {"y", "z"}

    def test_error_messages_returns_str_list(self):
        r = SemanticValidationResult()
        r._emit_finding(rule_id="x", severity="hard", message="alpha-msg")
        r._emit_finding(rule_id="y", severity="hard", message="beta-msg")
        msgs = r.error_messages
        assert msgs == ["alpha-msg", "beta-msg"]
        assert all(isinstance(m, str) for m in msgs)

    def test_valid_flips_to_false_on_hard_finding(self):
        r = SemanticValidationResult()
        assert r.valid is True
        r._emit_finding(rule_id="x", severity="soft", message="m")
        assert r.valid is True  # soft does not flip
        r._emit_finding(rule_id="y", severity="info", message="m")
        assert r.valid is True  # info does not flip
        r._emit_finding(rule_id="z", severity="hard", message="m")
        assert r.valid is False  # hard flips

    def test_deprecated_add_error_routes_through_emit_finding(self):
        r = SemanticValidationResult()
        r.add_error("legacy")
        assert len(r.errors) == 1
        assert r.errors[0].rule_id == RuleId.OTHER
        assert r.errors[0].severity == "hard"


# =============================================================================
# 3) Rule-id catalog coverage (5 minimal-case rules)
# =============================================================================


class TestRuleIdCatalogCoverage:
    def test_empty_expression_emits_hard_syntax(self):
        v = AlphaSemanticValidator()
        r = v.validate("")
        emp = [f for f in r.findings if f.rule_id == RuleId.EMPTY_EXPRESSION]
        assert len(emp) == 1
        assert emp[0].severity == "hard"
        assert emp[0].category == "syntax"
        assert r.valid is False

    def test_unknown_operator_stays_soft(self):
        # M-1: unknown_operator must remain soft (Q1 unchanged).
        # Force `allowed_operators` to a single op so any other operator
        # used in the expression becomes "unknown".
        v = AlphaSemanticValidator(operators=["rank"])
        r = v.validate("frobnicate(close)")
        unk = [f for f in r.findings if f.rule_id == RuleId.UNKNOWN_OPERATOR]
        assert len(unk) == 1
        assert unk[0].severity == "soft"
        assert unk[0].location == "frobnicate"
        # Result remains "valid" because soft never invalidates.
        # (Note: in strict mode the missing field `close` may still emit
        # field_not_found — we only assert about the unknown_operator finding.)

    def test_field_not_found_strict_is_hard(self):
        # M-2: severity is strict-mode-dependent. strict_field_check=True → hard.
        v = AlphaSemanticValidator(
            fields=[{"id": "close", "type": "MATRIX"}],
            strict_field_check=True,
        )
        r = v.validate("rank(unknown_field_xyz)")
        ff = [f for f in r.findings if f.rule_id == RuleId.FIELD_NOT_FOUND]
        assert len(ff) == 1
        assert ff[0].severity == "hard"
        assert ff[0].location == "unknown_field_xyz"
        assert r.valid is False

    def test_field_not_found_lenient_is_soft(self):
        # M-2: strict_field_check=False → soft.
        v = AlphaSemanticValidator(
            fields=[{"id": "close", "type": "MATRIX"}],
            strict_field_check=False,
        )
        r = v.validate("rank(unknown_field_xyz)")
        ff = [f for f in r.findings if f.rule_id == RuleId.FIELD_NOT_FOUND]
        assert len(ff) == 1
        assert ff[0].severity == "soft"
        # Result valid because soft does not invalidate.
        assert r.valid is True

    def test_low_coverage_field_emits_soft_with_metadata(self):
        v = AlphaSemanticValidator(
            fields=[{"id": "rare_field", "type": "MATRIX", "coverage": 0.2}],
        )
        r = v.validate("ts_rank(rare_field, 20)")
        lcf = [f for f in r.findings if f.rule_id == RuleId.LOW_COVERAGE_FIELD]
        assert len(lcf) == 1
        assert lcf[0].severity == "soft"
        assert lcf[0].metadata.get("coverage") == pytest.approx(0.2)
        assert lcf[0].location == "rare_field"

    def test_type_mismatch_vector_ts_emits_hard_when_strict(self):
        v = AlphaSemanticValidator(
            fields=[
                {"id": "vec_field", "type": "VECTOR", "coverage": 1.0},
            ],
            strict_field_check=False,
            strict_type_check=True,
        )
        r = v.validate("ts_delta(vec_field, 5)")
        tm = [f for f in r.findings if f.rule_id == RuleId.TYPE_MISMATCH_VECTOR_TS]
        assert len(tm) == 1
        assert tm[0].severity == "hard"


# =============================================================================
# 4) Risk-bound inference — R1-R4 (15 cases incl. paren-walk verification)
# =============================================================================


class TestRiskBoundInferenceR1DivideByVolatileDenom:
    def test_divide_by_volume_fires(self):
        findings = _infer_risk_findings("divide(close, volume)")
        r1 = [f for f in findings if f.rule_id == RuleId.RISK_DIVIDE_BY_VOLATILE_DENOM]
        assert len(r1) == 1
        assert r1[0].severity == "info"
        assert r1[0].metadata["max_loss_hint"] == "high"
        assert r1[0].metadata["denom_field"] == "volume"

    def test_divide_by_adv20_fires(self):
        findings = _infer_risk_findings("divide(close, adv20)")
        r1 = [f for f in findings if f.rule_id == RuleId.RISK_DIVIDE_BY_VOLATILE_DENOM]
        assert len(r1) == 1
        assert r1[0].metadata["denom_field"] == "adv20"

    def test_divide_by_cap_fires(self):
        findings = _infer_risk_findings("divide(net_income, cap)")
        r1 = [f for f in findings if f.rule_id == RuleId.RISK_DIVIDE_BY_VOLATILE_DENOM]
        assert len(r1) >= 1

    def test_divide_by_safe_denom_does_not_fire(self):
        findings = _infer_risk_findings("divide(close, vwap)")
        r1 = [f for f in findings if f.rule_id == RuleId.RISK_DIVIDE_BY_VOLATILE_DENOM]
        assert r1 == []


class TestRiskBoundInferenceR2HighExponentSignedPower:
    def test_exp_2_fires(self):
        findings = _infer_risk_findings("signed_power(close, 2)")
        r2 = [f for f in findings if f.rule_id == RuleId.RISK_HIGH_EXPONENT_SIGNED_POWER]
        assert len(r2) == 1
        assert r2[0].metadata["exponent"] == 2.0

    def test_exp_below_threshold_does_not_fire(self):
        findings = _infer_risk_findings("signed_power(close, 0.5)")
        r2 = [f for f in findings if f.rule_id == RuleId.RISK_HIGH_EXPONENT_SIGNED_POWER]
        assert r2 == []

    def test_negative_exp_fires_on_abs(self):
        findings = _infer_risk_findings("signed_power(close, -2)")
        r2 = [f for f in findings if f.rule_id == RuleId.RISK_HIGH_EXPONENT_SIGNED_POWER]
        assert len(r2) == 1
        assert r2[0].metadata["exponent"] == -2.0

    def test_nested_signed_power_paren_walk_picks_exponent_not_inner_arg(self):
        # M-6 critical case: nested signed_power(divide(x, y), 2). A flat
        # `[^,]+,\s*(\d+)` regex would grab the inner comma between x and y
        # and pick `y` as exp. Paren-walk must extract `2`.
        findings = _infer_risk_findings("signed_power(divide(x, y), 2)")
        r2 = [f for f in findings if f.rule_id == RuleId.RISK_HIGH_EXPONENT_SIGNED_POWER]
        assert len(r2) == 1
        assert r2[0].metadata["exponent"] == 2.0  # NOT y!


class TestRiskBoundInferenceR3ShortDecayMomentum:
    def test_decay_3_plus_ts_delta_fires(self):
        findings = _infer_risk_findings("ts_decay_linear(ts_delta(close, 5), 3)")
        r3 = [f for f in findings if f.rule_id == RuleId.RISK_SHORT_DECAY_WINDOW]
        assert len(r3) == 1
        assert r3[0].metadata["decay_window"] == 3
        assert r3[0].metadata["max_loss_hint"] == "medium"

    def test_decay_4_does_not_fire(self):
        findings = _infer_risk_findings("ts_decay_linear(ts_delta(close, 5), 4)")
        r3 = [f for f in findings if f.rule_id == RuleId.RISK_SHORT_DECAY_WINDOW]
        assert r3 == []

    def test_decay_3_without_momentum_inner_does_not_fire(self):
        findings = _infer_risk_findings("ts_decay_linear(close, 3)")
        r3 = [f for f in findings if f.rule_id == RuleId.RISK_SHORT_DECAY_WINDOW]
        assert r3 == []

    def test_nested_decay_paren_walk(self):
        # M-6: nested ts_decay_linear(divide(ts_delta(x, 5), y), 3) must
        # parse the outer decay's d-arg as `3`, not the inner ts_delta's 5.
        findings = _infer_risk_findings(
            "ts_decay_linear(divide(ts_delta(x, 5), y), 3)"
        )
        r3 = [f for f in findings if f.rule_id == RuleId.RISK_SHORT_DECAY_WINDOW]
        assert len(r3) == 1
        assert r3[0].metadata["decay_window"] == 3  # NOT 5!


class TestRiskBoundInferenceR4ExtremeWinsorize:
    def test_too_tight_fires(self):
        findings = _infer_risk_findings("winsorize(close, 0.5)")
        r4 = [f for f in findings if f.rule_id == RuleId.RISK_EXTREME_WINSORIZATION]
        assert len(r4) == 1
        assert r4[0].metadata["too_tight"] is True
        assert r4[0].metadata["winsorize_std"] == 0.5

    def test_too_loose_fires(self):
        findings = _infer_risk_findings("winsorize(close, 8)")
        r4 = [f for f in findings if f.rule_id == RuleId.RISK_EXTREME_WINSORIZATION]
        assert len(r4) == 1
        assert r4[0].metadata["too_tight"] is False
        assert r4[0].metadata["winsorize_std"] == 8.0

    def test_normal_range_does_not_fire(self):
        findings = _infer_risk_findings("winsorize(close, 4)")
        r4 = [f for f in findings if f.rule_id == RuleId.RISK_EXTREME_WINSORIZATION]
        assert r4 == []

    def test_kwarg_std_form_fires(self):
        findings = _infer_risk_findings("winsorize(close, std=0.5)")
        r4 = [f for f in findings if f.rule_id == RuleId.RISK_EXTREME_WINSORIZATION]
        assert len(r4) == 1
        assert r4[0].metadata["too_tight"] is True


# =============================================================================
# 5) Aggregation — max_loss_hint, rationale, confidence, severity_distribution
# =============================================================================


class TestRiskBoundAggregation:
    def test_max_loss_hint_picks_highest_rank(self):
        # medium + high → high
        findings = [
            Finding(
                rule_id="r_med", severity="info", message="m", category="risk",
                metadata={"max_loss_hint": "medium"},
            ),
            Finding(
                rule_id="r_high", severity="info", message="m", category="risk",
                metadata={"max_loss_hint": "high"},
            ),
        ]
        bounds = _aggregate_risk_bounds(findings)
        assert bounds["max_loss_hint"] == "high"

    def test_rationale_sorted_and_confidence_computed(self):
        findings = [
            Finding(
                rule_id=RuleId.RISK_DIVIDE_BY_VOLATILE_DENOM,
                severity="info", message="m", category="risk",
                metadata={"max_loss_hint": "high"},
            ),
            Finding(
                rule_id=RuleId.RISK_HIGH_EXPONENT_SIGNED_POWER,
                severity="info", message="m", category="risk",
                metadata={"max_loss_hint": "high"},
            ),
        ]
        bounds = _aggregate_risk_bounds(findings)
        assert bounds["rationale"] == sorted(bounds["rationale"])
        # 2 of 4 risk rules fired = 0.5 confidence (N-2: total comes from
        # _RISK_RULE_IDS tuple length).
        assert bounds["confidence"] == 0.5

    def test_severity_distribution_field_present(self):
        # N-1: severity_distribution must distinguish from finding.severity dim
        findings = [
            Finding(
                rule_id="r1", severity="info", message="m", category="risk",
                metadata={"max_loss_hint": "low"},
            ),
        ]
        bounds = _aggregate_risk_bounds(findings)
        assert "severity_distribution" in bounds
        assert bounds["severity_distribution"]["info"] == 1
        assert bounds["severity_distribution"]["hard"] == 0
        assert bounds["severity_distribution"]["soft"] == 0

    def test_empty_findings_returns_empty_dict(self):
        assert _aggregate_risk_bounds([]) == {}
        # Non-risk findings ignored:
        f = Finding(rule_id="x", severity="hard", message="m", category="semantics")
        assert _aggregate_risk_bounds([f]) == {}


# =============================================================================
# 6) _walk_call_args paren-walk regression
# =============================================================================


class TestWalkCallArgs:
    def test_simple_two_args(self):
        assert _walk_call_args("divide(close, volume)", "divide") == [["close", "volume"]]

    def test_nested_inner_call(self):
        # Inner divide's comma must not be picked as outer's split.
        result = _walk_call_args("signed_power(divide(x, y), 2)", "signed_power")
        assert result == [["divide(x, y)", "2"]]

    def test_word_boundary_left(self):
        # `xts_delta(...)` should NOT match `ts_delta(`.
        assert _walk_call_args("xts_delta(close, 5)", "ts_delta") == []

    def test_multiple_call_sites(self):
        out = _walk_call_args(
            "add(divide(a, b), divide(c, d))", "divide"
        )
        assert out == [["a", "b"], ["c", "d"]]

    def test_empty_expression(self):
        assert _walk_call_args("", "divide") == []
