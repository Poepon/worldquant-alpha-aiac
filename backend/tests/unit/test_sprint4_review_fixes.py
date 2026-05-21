"""Sprint 4 F1-F8 review-fix verification tests (2026-05-20).

3-round fresh agent review (R1 correctness + R2 failure-mode + R3
integration) found 5 MUST + 9 SHOULD. These tests pin each fix.
"""
from __future__ import annotations

import pytest

from backend.services import grammar_validator as gv


# ---------------------------------------------------------------------------
# F1: grammar widening — comparison / ternary / logical / power / sci-notation
#     / string literal / dotted field
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("expr", [
    "trade_when(volume > 0, rank(close), -1)",   # comparison in call
    "if_else(close > open, 1, -1)",              # comparison in call
    "rank(close) > 0.5 ? 1 : 0",                 # ternary
    "a > 0 && b < 1",                            # logical AND
    "a > 0 || b < 1",                            # logical OR
    "close ** 2",                                # power
    "1.5e-3",                                    # scientific notation
    "1e10",                                      # scientific notation no decimal
    "group_neutralize(close, 'sector')",         # string literal
    'group_neutralize(close, "industry")',       # double-quote string
    "fnd6.assets",                               # dotted field ref
    "ts_rank(fnd6.assets / fnd6.liabilities, 60)",  # dotted in arithmetic
])
def test_f1_widened_grammar_accepts_previously_rejected(expr):
    """All 12 shapes were rejected by the original grammar (verified by
    R1+R2 review). Widened grammar must accept them, else flag-ON drops
    valid alphas."""
    res = gv.validate(expr)
    assert res.ok, f"expected ok, got: {res.error_msg}"


@pytest.mark.parametrize("expr", [
    "rank(close",            # unclosed paren still fails
    "ts_rank(close,, 60)",   # double comma still fails
    "* close",               # leading binary op still fails
    "rank(close) ?",         # incomplete ternary
])
def test_f1_widened_grammar_still_rejects_malformed(expr):
    """Widening must NOT make the grammar accept genuinely broken input."""
    res = gv.validate(expr)
    assert not res.ok, f"expected fail, got ok for: {expr}"


# ---------------------------------------------------------------------------
# F2/F3: min-pass-rate degrade-open floor + state telemetry counters
# ---------------------------------------------------------------------------

def test_f2_f3_state_has_g3v2_telemetry_fields():
    from backend.agents.graph.state import MiningState
    fields = MiningState.model_fields
    assert "g3v2_parse_fail_count" in fields
    assert "g3v2_total_validated" in fields


def test_f3_node_code_gen_has_degrade_open_floor():
    """node_code_gen must buffer parse-fail candidates + degrade-open
    above a 50% drop floor (re-include them) so a too-narrow grammar
    can't zero out production."""
    import inspect
    from backend.agents.graph.nodes import generation as gen
    src = inspect.getsource(gen.node_code_gen)
    assert "_g3v2_parse_fail_buffer" in src
    assert "degrade-open" in src or "degrade_open" in src
    assert "_g3v2_drop_rate > 0.5" in src
    # telemetry to state (reachable, unlike dropped candidate metrics)
    assert "g3v2_parse_fail_count" in src


def test_f2_canary_sop_doc_corrected():
    """Canary SOP row 31 must NOT claim alpha.metrics['_g3v2_parse_failed']
    count (unreachable) — should reference log lines / state counter."""
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[3]
    sop = (repo_root / "docs" / "production_canary_sop_2026_05_18.md").read_text(encoding="utf-8")
    # The G3-v2 row should mention the log-based observable
    assert "grep worker logs" in sop or "g3v2_parse_fail_count" in sop


# ---------------------------------------------------------------------------
# F4/F5: RETRY_MAX + retry_with_whole_output_hint marked RESERVED
# ---------------------------------------------------------------------------

def test_f4_retry_max_marked_reserved():
    """GRAMMAR_VALIDATOR_RETRY_MAX has no production reader — config +
    flagspec must honestly mark it RESERVED."""
    import inspect
    from backend.services import feature_flag_service as ffs
    spec = ffs.SUPPORTED_FLAGS.get("GRAMMAR_VALIDATOR_RETRY_MAX")
    assert spec is not None
    assert "RESERVED" in spec.description


def test_f5_retry_hint_docstring_marks_reserved():
    """retry_with_whole_output_hint has no production caller — docstring
    must mark it RESERVED so it doesn't read as live integration."""
    doc = gv.retry_with_whole_output_hint.__doc__ or ""
    assert "RESERVED" in doc


# ---------------------------------------------------------------------------
# F14: G10 inject telemetry reachable (stamped on persisted candidates)
# ---------------------------------------------------------------------------

def test_f14_state_has_g10_injected_field():
    from backend.agents.graph.state import MiningState
    fields = MiningState.model_fields
    assert "g10_injected_entries_n" in fields


def test_f14_node_code_gen_stamps_g10_injected():
    """node_code_gen must stamp candidate.metrics['_g10_injected'] from
    state.g10_injected_entries_n (reachable persist path, unlike the
    dropped G3-v2 candidates)."""
    import inspect
    from backend.agents.graph.nodes import generation as gen
    src = inspect.getsource(gen.node_code_gen)
    assert "_g10_injected" in src
    assert "g10_injected_entries_n" in src


# ---------------------------------------------------------------------------
# F13: sentinel matrix rationale documented for Sprint-4 flags
# ---------------------------------------------------------------------------

def test_f13_sentinel_rationale_documented():
    """config.py must explain why G10/GRAMMAR are NOT in sentinel list
    (the asymmetry vs G8/AST-gate would otherwise look like an oversight)."""
    import inspect
    from backend import config
    src = inspect.getsource(config)
    assert "ENABLE_G10_LOGIC_INJECT: NOT a sentinel" in src
    assert "ENABLE_GRAMMAR_VALIDATOR: NOT a sentinel" in src


# ---------------------------------------------------------------------------
# F7: build_distilled_logic_block honors max_entries (not hard-coded 5)
# ---------------------------------------------------------------------------

def test_f7_build_block_honors_max_entries():
    from backend.services.logic_distill_service import build_distilled_logic_block
    entries = [
        {"pillar": "momentum", "logic_text": f"Logic {i}", "source_alpha_count": 1}
        for i in range(8)
    ]
    # max_entries=8 → all 8 rendered (was hard-capped at 5)
    block = build_distilled_logic_block(entries, max_entries=8)
    assert "Logic 7" in block
    # max_entries=3 → only first 3
    block3 = build_distilled_logic_block(entries, max_entries=3)
    assert "Logic 2" in block3
    assert "Logic 3" not in block3


def test_f7_build_block_default_max_entries_5():
    """Default max_entries=5 preserves prior behavior."""
    from backend.services.logic_distill_service import build_distilled_logic_block
    entries = [
        {"pillar": "momentum", "logic_text": f"Logic {i}", "source_alpha_count": 1}
        for i in range(8)
    ]
    block = build_distilled_logic_block(entries)
    assert "Logic 4" in block
    assert "Logic 5" not in block
