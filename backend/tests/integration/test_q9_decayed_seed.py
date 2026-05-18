"""Integration: Phase 2 Q9 Decayed Alpha seed (2026-05-18).

Tests per master plan §4.4 Q9:
  1. JSON file loads + entries count >= 50
  2. Each entry has required fields (name, pattern, description, decay_pct,
     failure_mode, theoretical_anchor, t_stat_orig)
  3. decay_pct values are negative (decay convention)
  4. failure_mode values are in known set
  5. import_batch marker assignable per [[feedback_forward_compat_metadata_hook]]
  6. pattern_hash uniqueness — no duplicate patterns within seed file
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.models.knowledge import compute_pattern_hash


SEED_FILE = Path(__file__).resolve().parents[3] / "backend" / "data" / "decayed_alphas_seed.json"


@pytest.fixture(scope="module")
def seed_data() -> dict:
    with SEED_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Test 1-2: file loads + entry count >= 50
# ---------------------------------------------------------------------------

def test_seed_file_exists():
    assert SEED_FILE.exists(), f"missing {SEED_FILE}"


def test_seed_file_has_at_least_50_entries(seed_data):
    """Master plan §4.4 Q9 spec: 50+ Decayed Alpha seeds."""
    assert "entries" in seed_data
    assert len(seed_data["entries"]) >= 50, \
        f"plan §4.4 Q9 requires >= 50 entries, got {len(seed_data['entries'])}"


def test_seed_file_has_meta_header(seed_data):
    """File _meta should document source + selection criteria."""
    assert "_meta" in seed_data
    meta = seed_data["_meta"]
    assert "source" in meta
    assert "selection_criteria" in meta
    assert "import_batch" in meta
    # Per session convention
    assert meta["import_batch"] == "phase2_q9_decayed_2026_05_18"


# ---------------------------------------------------------------------------
# Test 3: required fields per entry
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = (
    "name", "pattern", "description",
    "decay_pct", "failure_mode", "theoretical_anchor", "t_stat_orig",
)


def test_each_entry_has_required_fields(seed_data):
    """All 7 fields per plan §4.4 Q9 must be present on every entry."""
    for i, entry in enumerate(seed_data["entries"]):
        for f in REQUIRED_FIELDS:
            assert f in entry, f"entry #{i} ({entry.get('name', '?')}) missing field {f!r}"


def test_decay_pct_is_negative(seed_data):
    """decay_pct convention: negative = post-pub Sharpe drop."""
    for entry in seed_data["entries"]:
        assert isinstance(entry["decay_pct"], (int, float))
        assert entry["decay_pct"] <= 0, \
            f"entry {entry['name']} decay_pct={entry['decay_pct']} should be <= 0"


def test_decay_pct_in_realistic_range(seed_data):
    """Decay >= -100% (alpha can't disappear more than fully)."""
    for entry in seed_data["entries"]:
        assert entry["decay_pct"] >= -100, \
            f"entry {entry['name']} decay_pct={entry['decay_pct']} should be >= -100"


def test_t_stat_orig_is_positive(seed_data):
    """Original published t-stat should be > 0 (pre-pub significance)."""
    for entry in seed_data["entries"]:
        assert isinstance(entry["t_stat_orig"], (int, float))
        assert entry["t_stat_orig"] > 0, \
            f"entry {entry['name']} t_stat_orig={entry['t_stat_orig']} should be > 0"


# ---------------------------------------------------------------------------
# Test 4: failure_mode values in known set
# ---------------------------------------------------------------------------

KNOWN_FAILURE_MODES = {
    "arbitrage",                 # smart money traded it away
    "arbitrage_speed",           # algo trading reduced edge
    "data_mining",               # original was likely spurious (HXZ failed replication)
    "limits_to_arb_disappeared", # institutional changes erased frictions
    "regime_crash_risk",         # works on average but crashes hard
    "regime_sensitive",          # works in some regimes not others
    "regime_growth_dominance",   # growth-stock era reduced premium
    "structural_change",         # market structure (HFT/ETF) reduced edge
    "microstructure_change",     # tick size / decimal change
    "tax_regime_change",         # tax law changed payoff
    "regulatory_arbitrage",      # disclosure rules reduced asym info
    "factor_subsumption",        # subsumed by another factor
}


def test_failure_mode_in_known_set(seed_data):
    """failure_mode should be a documented category for analytics."""
    unknown = []
    for entry in seed_data["entries"]:
        if entry["failure_mode"] not in KNOWN_FAILURE_MODES:
            unknown.append((entry["name"], entry["failure_mode"]))
    assert not unknown, \
        f"Unknown failure_modes: {unknown}. Extend KNOWN_FAILURE_MODES if intended."


# ---------------------------------------------------------------------------
# Test 5: pattern uniqueness (no duplicate patterns within seed)
# ---------------------------------------------------------------------------

def test_no_duplicate_pattern_hashes_within_seed(seed_data):
    """pattern_hash UNIQUE constraint at DB level — verify seed itself has no dupes."""
    seen = {}
    for entry in seed_data["entries"]:
        phash = compute_pattern_hash(entry["pattern"], None, None)
        if phash in seen:
            pytest.fail(
                f"duplicate pattern_hash {phash[:16]} between "
                f"{seen[phash]!r} and {entry['name']!r}"
            )
        seen[phash] = entry["name"]


def test_no_duplicate_names_within_seed(seed_data):
    names = [e["name"] for e in seed_data["entries"]]
    assert len(names) == len(set(names)), \
        f"duplicate names: {[n for n in names if names.count(n) > 1]}"


# ---------------------------------------------------------------------------
# Test 6: theoretical anchors reference real papers
# ---------------------------------------------------------------------------

def test_theoretical_anchor_non_empty(seed_data):
    for entry in seed_data["entries"]:
        anchor = entry["theoretical_anchor"]
        assert isinstance(anchor, str) and len(anchor) >= 10, \
            f"entry {entry['name']} anchor too short: {anchor!r}"


def test_pattern_non_empty(seed_data):
    for entry in seed_data["entries"]:
        assert entry["pattern"] and len(entry["pattern"]) >= 5, \
            f"entry {entry['name']} pattern too short"
