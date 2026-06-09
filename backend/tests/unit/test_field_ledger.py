"""Unit tests for the field-ledger token extractor + p90 (PR-A, 2026-06-09)."""
from backend.tasks.field_ledger_refresh import extract_field_tokens, _p90


FIELDS = {"close", "volume", "anl4_afv4_cfps_number", "assets", "rank"}


def test_extracts_known_field_tokens():
    expr = "group_neutralize(rank(ts_mean(anl4_afv4_cfps_number, 20)), industry)"
    got = extract_field_tokens(expr, FIELDS)
    # 'rank' is in FIELDS (a field named like an op would match) AND the real field
    assert "anl4_afv4_cfps_number" in got
    assert "rank" in got  # token match is purely set-membership (caller curates set)
    assert "close" not in got


def test_only_known_fields_match():
    expr = "ts_rank(close, 22) + volume"
    got = extract_field_tokens(expr, FIELDS)
    assert got == {"close", "volume"}


def test_empty_and_none_expression():
    assert extract_field_tokens("", FIELDS) == set()
    assert extract_field_tokens(None, FIELDS) == set()


def test_no_partial_token_match():
    # 'assets' must not match inside 'total_assets_value' as a substring — token
    # boundaries matter (regex identifier tokens).
    got = extract_field_tokens("rank(total_assets_value)", FIELDS)
    assert "assets" not in got


def test_p90():
    assert _p90([]) is None
    assert _p90([1.5]) == 1.5
    assert _p90([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]) == 0.9  # nearest-rank
    # contains None → filtered
    assert _p90([None, 0.5, None]) == 0.5
