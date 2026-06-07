"""Field hygiene (#25c) — only numeric SIGNAL fields reach the code-gen LLM.

The pool field path was offering non-signal metadata (universe-membership flags,
symbols, UTC timestamps/dates, ISO/entity codes); the LLM built degenerate
expressions on them (ts_zscore(entity_country_iso_code_4), subtract(top500,
top500)=0) → 0/neg sharpe (root of the 2026-05-20 submit-yield collapse).
_is_signal_field is the pure, config-driven predicate that drops them.
"""
import pytest

from backend.tasks.fetch_helpers import _is_signal_field


class TestIsSignalField:
    @pytest.mark.parametrize("fid,ftype", [
        ("close", "MATRIX"),
        ("low", "MATRIX"),
        ("high", "MATRIX"),
        ("vwap", "MATRIX"),
        ("fscore_total", "MATRIX"),
        ("call_breakeven_120", "MATRIX"),
        ("price_at_news_time", "VECTOR"),          # legit news signal (NOT a *_time_utc)
        ("main_session_vwap_from_news", "VECTOR"),
        ("industry", "GROUP"),                     # grouping field — legit
    ])
    def test_signal_fields_kept(self, fid, ftype):
        assert _is_signal_field(fid, ftype) is True

    @pytest.mark.parametrize("fid,ftype", [
        ("top500", "UNIVERSE"),                    # universe membership flag
        ("top200", "UNIVERSE"),
        ("topsp500", "UNIVERSE"),
        ("some_symbol", "SYMBOL"),                 # symbol/identifier
        ("event_start_time_utc", "VECTOR"),        # UTC timestamp
        ("event_end_time_utc", "VECTOR"),
        ("event_start_date_utc", "VECTOR"),        # UTC date
        ("reporting_period_end_date_utc", "VECTOR"),
        ("entity_country_iso_code_4", "VECTOR"),   # ISO country code
    ])
    def test_metadata_fields_dropped(self, fid, ftype):
        assert _is_signal_field(fid, ftype) is False

    def test_case_insensitive_id_match(self):
        assert _is_signal_field("EVENT_START_TIME_UTC", "VECTOR") is False

    def test_none_safe(self):
        # Defensive: None id/type must not crash; unknown type w/ clean id = keep.
        assert _is_signal_field(None, "MATRIX") is True
        assert _is_signal_field("close", None) is True
