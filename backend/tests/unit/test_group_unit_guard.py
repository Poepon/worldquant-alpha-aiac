"""Tests for the group_* UNIT pre-sim guard (2026-05-22).

BRAIN rejects group misuse with "Incompatible unit ... expected Unit[Group:1]".
The guard catches two patterns pre-simulate (field-type-aware):
  A. group_neutralize(value, G) where G is a non-group data field;
  B. a GROUP-classified field used as a plain value input.
Hard only when reject_unknown_operators (mining pre-sim) + a field catalog is
present; inert / soft otherwise so it can't hard-reject a valid alpha blindly.
"""
from backend.alpha_semantic_validator import AlphaSemanticValidator, RuleId

_FIELDS = [
    {"id": "sector_grp", "type": "GROUP"},
    {"id": "industry_adjusted_doubtful_receivables", "type": "MATRIX"},
    {"id": "close", "type": "MATRIX"},
]


def _val(reject=True, fields=_FIELDS):
    return AlphaSemanticValidator(
        fields=fields,
        operators=["group_neutralize", "rank", "ts_delta", "ts_zscore"],
        strict_field_check=False,
        reject_unknown_operators=reject,
    )


def _gum(r):
    return [f for f in r.findings if f.rule_id == RuleId.GROUP_UNIT_MISMATCH]


class TestCheckA_GroupArg:
    def test_data_field_as_group_arg_rejected(self):
        # The exact reported error.
        r = _val().validate(
            "group_neutralize(rank(close), industry_adjusted_doubtful_receivables)"
        )
        assert _gum(r), "non-group field as grouping arg must be flagged"
        assert r.valid is False

    def test_standard_group_identifier_ok(self):
        r = _val().validate("group_neutralize(rank(close), sector)")
        assert not _gum(r)

    def test_group_field_arg_ok(self):
        r = _val().validate("group_neutralize(rank(close), sector_grp)")
        assert not _gum(r)


class TestCheckB_GroupAsValue:
    def test_group_field_in_ts_op_rejected(self):
        r = _val().validate("ts_delta(sector_grp, 120)")
        assert _gum(r), "GROUP field as ts_* value input must be flagged"
        assert r.valid is False

    def test_matrix_field_in_ts_op_ok(self):
        r = _val().validate("ts_delta(close, 120)")
        assert not _gum(r)


class TestSafety:
    def test_inert_without_field_catalog(self):
        # No fields → field_map empty → cannot know types → never flags.
        r = _val(fields=None).validate(
            "group_neutralize(rank(close), industry_adjusted_doubtful_receivables)"
        )
        assert not _gum(r)

    def test_soft_when_not_pre_sim(self):
        # reject_unknown_operators=False → finding is soft, does not invalidate.
        r = _val(reject=False).validate(
            "group_neutralize(rank(close), industry_adjusted_doubtful_receivables)"
        )
        gum = _gum(r)
        assert gum and gum[0].severity == "soft"
        assert r.valid is not False  # soft must not hard-reject
