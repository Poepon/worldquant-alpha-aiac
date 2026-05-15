"""P2-A macro_narratives pure-function unit tests (2026-05-16).

5 unit cases covering:
  - U1 SEED_FIELD_NARRATIVES populated (≥6) + non-empty mechanism / transmission
  - U2 SEED_CATEGORY_NARRATIVES populated (≥5) + category ∈ allowed set
  - U3 compute_narrative_hash stability + source distinction
  - U4 narrative_to_kb_payload field-scope schema
  - U5 narrative_to_kb_payload category-scope schema + vwap M11 regression
"""
from __future__ import annotations

import pytest

from backend.macro_narratives import (
    MacroNarrative,
    SEED_CATEGORY_NARRATIVES,
    SEED_FIELD_NARRATIVES,
    compute_narrative_hash,
    get_all_seeds,
    narrative_to_kb_payload,
)


# ---------------------------------------------------------------------------
# U1: field seeds populated, mechanism + transmission non-empty
# ---------------------------------------------------------------------------
def test_seed_field_narratives_populated_and_nonempty():
    assert len(SEED_FIELD_NARRATIVES) >= 6, (
        "P2-A spec requires ≥6 field-scope seeds"
    )
    for n in SEED_FIELD_NARRATIVES:
        assert isinstance(n, MacroNarrative)
        assert n.field_id, f"field-scope seed missing field_id: {n}"
        assert n.mechanism.strip(), (
            f"field-scope seed {n.field_id} has empty mechanism"
        )
        assert n.transmission_channel.strip(), (
            f"field-scope seed {n.field_id} has empty transmission_channel"
        )
        assert n.source == "seed"
        assert n.expected_signal_hint in {
            "momentum", "mean_reversion", "value", "quality",
            "volatility", "sentiment",
        }, (
            f"unknown expected_signal_hint={n.expected_signal_hint!r} "
            f"for field={n.field_id}"
        )


# ---------------------------------------------------------------------------
# U2: category seeds populated, dataset_category ∈ allowed set
# ---------------------------------------------------------------------------
def test_seed_category_narratives_populated_and_categories_known():
    assert len(SEED_CATEGORY_NARRATIVES) >= 5, (
        "P2-A spec requires ≥5 category-scope seeds"
    )
    allowed = {"pv", "analyst", "fundamental", "news", "macro"}
    seen = set()
    for n in SEED_CATEGORY_NARRATIVES:
        assert isinstance(n, MacroNarrative)
        assert n.field_id is None, (
            f"category-scope seed must NOT carry field_id: {n}"
        )
        assert n.dataset_category in allowed, (
            f"category-scope seed has unmapped category={n.dataset_category!r}"
        )
        assert n.mechanism.strip()
        assert n.transmission_channel.strip()
        seen.add(n.dataset_category)
    assert seen == allowed, (
        f"category seed bank should cover {allowed}, missing={allowed - seen}"
    )


# ---------------------------------------------------------------------------
# U3: compute_narrative_hash stability + source distinction
# ---------------------------------------------------------------------------
def test_compute_narrative_hash_stable_and_source_distinguished():
    # Two identical inputs → identical hash
    h_a = compute_narrative_hash(
        field_id="close", dataset_category="pv", region="*",
        source="seed", dataset_id=None,
    )
    h_b = compute_narrative_hash(
        field_id="close", dataset_category="pv", region="*",
        source="seed", dataset_id=None,
    )
    assert h_a == h_b
    assert len(h_a) == 32  # compute_pattern_hash truncates sha256 to 32 hex

    # Same field, different source → different hash (so seed + llm coexist)
    h_llm = compute_narrative_hash(
        field_id="close", dataset_category="pv", region="*",
        source="llm", dataset_id=None,
    )
    assert h_a != h_llm

    # Category scope vs field scope: same key text, different scope → different
    h_cat = compute_narrative_hash(
        field_id=None, dataset_category="close", region="*",
        source="seed", dataset_id=None,
    )
    assert h_a != h_cat


# ---------------------------------------------------------------------------
# U4: narrative_to_kb_payload field-scope schema
# ---------------------------------------------------------------------------
def test_narrative_to_kb_payload_field_scope_shape():
    n = SEED_FIELD_NARRATIVES[0]  # "close"
    p = narrative_to_kb_payload(n)
    assert p["entry_type"] == "MACRO_NARRATIVE"
    assert p["is_active"] is True
    assert p["created_by"] == "P2A_MACRO"
    assert p["pattern"].startswith("MACRO_NARRATIVE::field::close::seed")
    assert len(p["pattern_hash"]) == 32
    md = p["meta_data"]
    assert md["scope"] == "field"
    assert md["field_id"] == "close"
    assert md["dataset_category"] == "pv"
    assert md["region"] == "*"
    assert md["source"] == "seed"
    assert isinstance(md["confidence"], float)
    assert md["mechanism"]
    assert md["transmission_channel"]
    # S2 cap: ≤500 chars
    assert len(p["description"]) <= 500
    assert len(md["mechanism"]) <= 500
    assert len(md["transmission_channel"]) <= 500


# ---------------------------------------------------------------------------
# U5: category-scope payload + M11 vwap regression
# ---------------------------------------------------------------------------
def test_narrative_to_kb_payload_category_scope_and_vwap_consistency():
    # Category scope
    n_cat = SEED_CATEGORY_NARRATIVES[0]
    p_cat = narrative_to_kb_payload(n_cat)
    md_cat = p_cat["meta_data"]
    assert md_cat["scope"] == "category"
    assert md_cat["field_id"] is None
    assert md_cat["dataset_category"] in {"pv", "analyst", "fundamental",
                                          "news", "macro"}
    assert p_cat["pattern"].startswith(
        f"MACRO_NARRATIVE::category::{md_cat['dataset_category']}::seed"
    )

    # M11 regression: vwap seed is consistently mean_reversion (NOT momentum)
    vwap = next((n for n in SEED_FIELD_NARRATIVES if n.field_id == "vwap"), None)
    assert vwap is not None, "vwap seed missing from SEED_FIELD_NARRATIVES"
    assert vwap.expected_signal_hint == "mean_reversion", (
        f"M11 vwap consistency violated — expected_signal_hint must be "
        f"'mean_reversion', got {vwap.expected_signal_hint!r}"
    )
    # M11 transmission must NOT mention "动量" / "momentum" / "趋势"
    haystack = (vwap.transmission_channel + " " + vwap.mechanism).lower()
    for forbidden in ("动量", "momentum", "趋势加强"):
        assert forbidden not in haystack, (
            f"M11 vwap transmission still mentions {forbidden!r}: "
            f"{vwap.transmission_channel}"
        )

    # get_all_seeds: concatenation of both lists
    all_seeds = get_all_seeds()
    assert len(all_seeds) == len(SEED_FIELD_NARRATIVES) + len(SEED_CATEGORY_NARRATIVES)
