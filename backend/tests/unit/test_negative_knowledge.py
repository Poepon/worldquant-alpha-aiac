"""Unit tests for P2-D negative-knowledge pure-function module.

来源: docs/alphagbm_skills_research_2026-05-15.md skills `take-profit`/
`health-check`.

Tests pure functions only (``backend.negative_knowledge``) — no DB / no
Celery / no FS. aiosqlite-safe.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import pytest

from backend.negative_knowledge import (
    FailureSignature,
    _REMEDIATION_HINTS,
    _merge_examples,
    _pattern_text_for,
    _skeletonize,
    aggregate_signatures,
    compute_signature_key,
    extract_failures_from_alpha,
    extract_failures_from_alpha_failure,
    extract_failures_from_hypothesis_round,
)


# ---------------------------------------------------------------------------
# Builders — minimal stand-ins for ORM rows (the extractors use getattr)
# ---------------------------------------------------------------------------
@dataclass
class MockAlpha:
    alpha_id: str = "a-1"
    id: int = 1
    region: str = "USA"
    expression: str = ""
    metrics: Optional[Dict[str, Any]] = None


@dataclass
class MockAlphaFailure:
    id: int = 1
    expression: str = ""
    error_type: str = ""
    error_message: str = ""
    _resolved_region: str = ""


@dataclass
class MockHypothesis:
    id: int = 1
    region: str = "USA"
    statement: str = ""
    trigger_detail: Optional[Dict[str, Any]] = None


@dataclass
class MockRoundStats:
    attribution: Optional[str] = None
    attribution_reason: str = ""


_FROZEN_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# U1 / U2: skeletonize
# ---------------------------------------------------------------------------
class TestSkeletonize:

    def test_skeletonize_equivalence(self):
        """U1: Same structural shape with different field names yields the
        same skeleton."""
        s1 = _skeletonize("ts_rank(ts_delta(close, 5), 20)")
        s2 = _skeletonize("ts_rank(ts_delta(volume, 5), 20)")
        assert s1 == s2
        assert "ts_rank" in s1
        assert "ts_delta" in s1

    def test_skeletonize_empty_and_unparseable(self):
        """U2: Empty / None / pure garbage all degrade to ``"UNKNOWN"``
        without raising."""
        assert _skeletonize("") == "UNKNOWN"
        assert _skeletonize(None) == "UNKNOWN"  # type: ignore[arg-type]
        assert _skeletonize("    ") == "UNKNOWN"
        # malformed parens — extractor should not throw
        result = _skeletonize("ts_rank(((")
        assert isinstance(result, str)  # never raises


# ---------------------------------------------------------------------------
# U3: signature key stability + region differentiation
# ---------------------------------------------------------------------------
class TestSignatureKey:

    def test_signature_key_stability(self):
        """U3: Identical (rule_id, skeleton, region) → identical hex;
        differing region → different hex."""
        k1 = compute_signature_key("RISK_DIV", "ts_rank(...)", "USA")
        k2 = compute_signature_key("RISK_DIV", "ts_rank(...)", "USA")
        k3 = compute_signature_key("RISK_DIV", "ts_rank(...)", "EUR")
        assert k1 == k2
        assert k1 != k3
        assert len(k1) == 16
        # Different rule_id → different key too
        k4 = compute_signature_key("STATIC_OVERFIT", "ts_rank(...)", "USA")
        assert k1 != k4


# ---------------------------------------------------------------------------
# U4: extract from alpha — multi-source signatures
# ---------------------------------------------------------------------------
class TestExtractFromAlpha:

    def test_extract_alpha_multi_signature(self):
        """U4: An alpha with static + threshold + robustness findings yields
        ≥3 distinct signatures."""
        a = MockAlpha(
            alpha_id="multi-1",
            region="USA",
            expression="ts_rank(close, 20)",
            metrics={
                "_validation_findings": [
                    {"rule_id": "RISK_DIVIDE_BY_VOLATILE_DENOM",
                     "severity": "orange",
                     "message": "Volatile denom"},
                ],
                "failed_tests": [
                    {"rule": "sharpe_below_min", "severity": "orange",
                     "message": "Sharpe 0.7 < 1.25"},
                ],
                "_robustness_failed": [
                    {"name": "subuniv_drop", "severity": "red"},
                ],
            },
        )
        sigs = extract_failures_from_alpha(a, now_utc=_FROZEN_NOW)
        cats = {s.category for s in sigs}
        assert "static_finding" in cats
        assert "threshold" in cats
        assert "robustness" in cats
        assert len(sigs) >= 3
        # All carry the same region & skeleton
        for s in sigs:
            assert s.region == "USA"
            assert "ts_rank" in s.skeleton

    def test_extract_alpha_dedups_within_alpha(self):
        """U5: The SAME rule_id appearing twice in one alpha's
        _validation_findings collapses to ONE signature (failure_count=1
        — incrementing is upsert-layer's job, not the extractor's)."""
        a = MockAlpha(
            alpha_id="dupe-1",
            region="USA",
            expression="ts_rank(close, 20)",
            metrics={
                "_validation_findings": [
                    {"rule_id": "STATIC_OVERFIT_WINDOW", "severity": "orange",
                     "message": "Window 250"},
                    {"rule_id": "STATIC_OVERFIT_WINDOW", "severity": "orange",
                     "message": "Window 252"},  # SAME rule, different msg
                ],
            },
        )
        sigs = extract_failures_from_alpha(a, now_utc=_FROZEN_NOW)
        # Only one signature for STATIC_OVERFIT_WINDOW
        sof = [s for s in sigs if s.rule_id == "STATIC_OVERFIT_WINDOW"]
        assert len(sof) == 1
        assert sof[0].failure_count == 1

    def test_extract_hypothesis_round_attribution_filter(self):
        """U6: attribution='implementation' yields zero signatures;
        attribution='hypothesis' yields exactly one."""
        h = MockHypothesis(id=42, region="USA", statement="momentum-decay")
        rs_impl = MockRoundStats(attribution="implementation",
                                 attribution_reason="syntax")
        rs_hyp = MockRoundStats(attribution="hypothesis",
                                attribution_reason="thesis broke regime")

        out_impl = extract_failures_from_hypothesis_round(
            rs_impl, h, now_utc=_FROZEN_NOW,
        )
        out_hyp = extract_failures_from_hypothesis_round(
            rs_hyp, h, now_utc=_FROZEN_NOW,
        )
        assert out_impl == []
        assert len(out_hyp) == 1
        assert out_hyp[0].category == "attribution"
        assert out_hyp[0].region == "USA"


# ---------------------------------------------------------------------------
# U7: aggregate_signatures + _merge_examples reservoir
# ---------------------------------------------------------------------------
class TestAggregate:

    def test_aggregate_reservoir_sample(self):
        """U7: 8 signatures sharing the same key → ONE aggregated entry,
        failure_count=8, top_examples capped at 5 with S6 reservoir rule
        (first 3 + last 2). _merge_examples must give the same answer
        across aggregate + upsert paths (consistency guarantee S6)."""
        base = datetime(2026, 5, 1, tzinfo=timezone.utc)
        sigs = []
        for i in range(8):
            when = (base + timedelta(hours=i)).isoformat()
            sigs.append(FailureSignature(
                signature_key="abcdef0123456789",
                rule_id="RISK_TEST",
                skeleton="ts_rank(...)",
                region="USA",
                category="static_finding",
                severity="orange",
                expected_signal="msg",
                remediation_hint="fix",
                failure_count=1,
                top_examples=[{"alpha_id": f"a-{i}",
                               "expression": f"expr-{i}", "at": when}],
                first_seen_at=when,
                last_seen_at=when,
            ))
        agg = aggregate_signatures(sigs)
        assert len(agg) == 1
        merged = agg["abcdef0123456789"]
        assert merged.failure_count == 8
        # top_examples capped at 5
        assert len(merged.top_examples) == 5
        # S6 invariant: first 3 oldest + last 2 newest
        alpha_ids = [e["alpha_id"] for e in merged.top_examples]
        assert "a-0" in alpha_ids  # oldest preserved
        assert "a-7" in alpha_ids  # newest preserved
        # min/max seen_at preserved
        assert merged.first_seen_at == base.isoformat()
        assert merged.last_seen_at == (base + timedelta(hours=7)).isoformat()

        # S6 consistency: directly calling _merge_examples on the same
        # examples gives the same set
        flat_examples = [s.top_examples[0] for s in sigs]
        direct = _merge_examples([], flat_examples, keep=5)
        assert len(direct) == 5
        # Same alpha_ids present
        assert {e["alpha_id"] for e in direct} == set(alpha_ids)


# ---------------------------------------------------------------------------
# Sanity checks that the plan-mandated invariants hold
# ---------------------------------------------------------------------------
class TestPlanInvariants:

    def test_remediation_hints_keys_match_hypothesis_health_service_literals(
        self,
    ):
        """M1 invariant: the 5 hyp_trigger keys must use the LITERAL
        trigger.type strings emitted by hypothesis_health_service.py
        L176/210/262/295/323."""
        expected = {
            "hyp_trigger_dropped_sharpe_pct",
            "hyp_trigger_no_pass_in_n_rounds",
            "hyp_trigger_pass_rate_drop",
            "hyp_trigger_attribution_hypothesis_dominant",
            "hyp_trigger_stale_alphas",
        }
        assert expected.issubset(_REMEDIATION_HINTS.keys())

    def test_pattern_text_is_signature_key_only(self):
        """S7 invariant: _pattern_text_for must use signature_key ONLY —
        no skeleton suffix (avoids hash collisions on long shared
        skeleton prefixes)."""
        sig = FailureSignature(
            signature_key="1234567890abcdef",
            rule_id="RISK_TEST",
            skeleton="ts_rank(ts_delta(ts_zscore(ts_mean(ts_kurtosis(...)))))",
            region="USA",
            category="static_finding",
            severity="orange",
            expected_signal="x",
            remediation_hint="x",
        )
        text = _pattern_text_for(sig)
        assert text == "PITFALL::1234567890abcdef"
        # MUST NOT include skeleton
        assert "ts_rank" not in text
        assert "..." not in text

    def test_alpha_failure_no_region_falls_back_to_resolved(self):
        """S5 invariant: AlphaFailure with empty _resolved_region carries
        region="" — fetch_top_pitfalls then routes via the sim_error
        cross-region clause."""
        f = MockAlphaFailure(
            id=99,
            expression="ts_rank(close, 20)",
            error_type="SYNTAX_ERROR",
            error_message="unexpected token",
            _resolved_region="",
        )
        out = extract_failures_from_alpha_failure(f, now_utc=_FROZEN_NOW)
        assert len(out) == 1
        assert out[0].region == ""
        assert out[0].category == "sim_error"

        # And when service layer DID resolve it
        f2 = MockAlphaFailure(
            id=100,
            expression="ts_rank(close, 20)",
            error_type="FIELD_NOT_FOUND",
            _resolved_region="EUR",
        )
        out2 = extract_failures_from_alpha_failure(f2, now_utc=_FROZEN_NOW)
        assert out2[0].region == "EUR"
