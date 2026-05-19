"""Phase 4 Sprint 1 A4 — AQR Kelly KB seed unit + integration tests.

Coverage:
  - JSON schema validation:
    - each entry has required keys (pattern, description)
    - confidence in [0, 1]
    - source_url looks like SSRN/AQR URL
    - category in known set
  - _load_entries() filters header/_meta blocks
  - Idempotency: re-running import on in-memory aiosqlite KB does NOT
    duplicate rows (relies on _pattern_hash_exists)
  - All 5 papers represented (at least 1 entry per SSRN/AQR ID)
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEED_JSON = _REPO_ROOT / "backend" / "data" / "aqr_kelly_seed.json"


def _load_raw_json():
    return json.loads(_SEED_JSON.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# JSON schema validation
# ---------------------------------------------------------------------------


def test_seed_json_file_present_and_parses():
    assert _SEED_JSON.exists(), f"missing seed file at {_SEED_JSON}"
    raw = _load_raw_json()
    assert isinstance(raw, list) and len(raw) > 0


def test_load_entries_filters_header_blocks():
    """_load_entries() should drop header/_meta blocks (keys starting _)."""
    from scripts.seed_aqr_kelly_paper import _load_entries

    entries = _load_entries()
    # JSON has 14 top-level items (2 header + 12 real); _load_entries
    # should return only the 12 real ones
    assert len(entries) == 12, f"expected 12 real entries after filter, got {len(entries)}"


def test_every_entry_has_required_fields():
    raw = _load_raw_json()
    for item in raw:
        if not isinstance(item, dict):
            continue
        if all(k.startswith("_") for k in item.keys()):
            continue
        assert "pattern" in item, f"missing 'pattern' in {item}"
        assert "description" in item, f"missing 'description' in {item}"
        assert isinstance(item["pattern"], str) and item["pattern"].strip()
        assert isinstance(item["description"], str) and item["description"].strip()


def test_confidence_in_unit_interval():
    raw = _load_raw_json()
    for item in raw:
        if not isinstance(item, dict) or all(k.startswith("_") for k in item.keys()):
            continue
        c = item.get("confidence")
        if c is None:
            continue  # tolerated — script defaults to 0.75
        assert 0.0 <= float(c) <= 1.0, f"confidence out of [0,1]: {c} in {item}"


def test_source_url_well_formed():
    """source_url either empty or looks like SSRN or AQR working-paper URL."""
    raw = _load_raw_json()
    pattern = re.compile(
        r"^https?://(papers\.ssrn\.com|www\.aqr\.com|link\.springer\.com|arxiv\.org)/"
    )
    for item in raw:
        if not isinstance(item, dict) or all(k.startswith("_") for k in item.keys()):
            continue
        url = item.get("source_url", "")
        if not url:
            continue
        assert pattern.search(url), f"unexpected source_url: {url}"


def test_category_in_known_set():
    known = {"pv", "fundamental", "analyst", "macro", "other"}
    raw = _load_raw_json()
    for item in raw:
        if not isinstance(item, dict) or all(k.startswith("_") for k in item.keys()):
            continue
        cat = item.get("category", "other")
        assert cat in known, f"unknown category {cat!r}"


def test_all_five_papers_represented():
    """Every one of the 5 SSRN/AQR sources should have at least 1 entry."""
    raw = _load_raw_json()
    paper_anchors = set()
    for item in raw:
        if not isinstance(item, dict) or all(k.startswith("_") for k in item.keys()):
            continue
        anchor = item.get("theoretical_anchor", "")
        # Reduce to paper-family prefix
        if "Giglio-Kelly-Xiu" in anchor:
            paper_anchors.add("giglio_kelly_xiu_2022")
        elif "Kelly-Xiu 2023" in anchor:
            paper_anchors.add("kelly_xiu_2023_finml")
        elif "Kelly large" in anchor or "large+deep" in anchor:
            paper_anchors.add("kelly_large_deep")
        elif "Chen-Kelly-Xiu" in anchor:
            paper_anchors.add("chen_kelly_xiu_llm")
        elif "Gu-Kelly-Xiu" in anchor or "autoencoder" in anchor.lower():
            paper_anchors.add("gu_kelly_xiu_autoencoder")
    # Composite entries (last entry) reference 2 papers — that's OK as
    # long as each individual paper has at least 1 dedicated entry.
    assert len(paper_anchors) >= 5, (
        f"expected ≥5 papers represented, only got: {paper_anchors}"
    )


def test_anchor_metadata_minority():
    """Anchor-only entries should be at most 50% of total (else operator
    can't actually mine from this seed — they're just notes)."""
    raw = _load_raw_json()
    total = 0
    anchors = 0
    for item in raw:
        if not isinstance(item, dict) or all(k.startswith("_") for k in item.keys()):
            continue
        total += 1
        if item.get("is_anchor_metadata") is True:
            anchors += 1
    assert anchors <= total / 2 + 1, (
        f"too many anchor_metadata ({anchors}/{total}) — should mostly be operational"
    )


# ---------------------------------------------------------------------------
# Idempotency — real in-memory aiosqlite (per [[feedback_orm_constructor_real_test]])
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_import_on_real_kb(db_session):
    """Re-running import on the same DB MUST NOT duplicate rows."""
    from backend.external_knowledge import ExternalKnowledgeSyncer
    from backend.models import KnowledgeEntry
    from scripts.seed_aqr_kelly_paper import _load_entries, IMPORT_BATCH_TAG
    from sqlalchemy import select, func

    entries = _load_entries()
    assert len(entries) == 12

    # First import
    syncer1 = ExternalKnowledgeSyncer(db_session)
    n1 = await syncer1.import_curated_patterns(entries, batch_id=IMPORT_BATCH_TAG)
    assert n1 == 12, f"first import should add all 12 rows, got {n1}"

    # Count actual KB rows tagged with our batch
    count_q = select(func.count(KnowledgeEntry.id)).where(
        KnowledgeEntry.is_active == True,
    )
    total_after_first = (await db_session.execute(count_q)).scalar() or 0
    assert total_after_first == 12

    # Second import (idempotency check) — _pattern_hash_exists must dedupe
    syncer2 = ExternalKnowledgeSyncer(db_session)
    n2 = await syncer2.import_curated_patterns(entries, batch_id=IMPORT_BATCH_TAG)
    assert n2 == 0, f"second import should add 0 rows (idempotent), got {n2}"
    total_after_second = (await db_session.execute(count_q)).scalar() or 0
    assert total_after_second == 12


@pytest.mark.asyncio
async def test_imported_rows_split_into_success_pattern_and_anchor_metadata(
    db_session,
):
    """After import, the dual-path entry_type partition matches our JSON:
    7 SUCCESS_PATTERN + 5 ANCHOR_METADATA."""
    from backend.external_knowledge import ExternalKnowledgeSyncer
    from backend.models import KnowledgeEntry
    from scripts.seed_aqr_kelly_paper import _load_entries, IMPORT_BATCH_TAG
    from sqlalchemy import select, func

    entries = _load_entries()
    syncer = ExternalKnowledgeSyncer(db_session)
    await syncer.import_curated_patterns(entries, batch_id=IMPORT_BATCH_TAG)

    by_type_q = select(
        KnowledgeEntry.entry_type,
        func.count(KnowledgeEntry.id),
    ).group_by(KnowledgeEntry.entry_type)
    rows = (await db_session.execute(by_type_q)).all()
    by_type = {t: int(c) for t, c in rows}
    assert by_type.get("SUCCESS_PATTERN", 0) == 7, by_type
    assert by_type.get("ANCHOR_METADATA", 0) == 5, by_type


@pytest.mark.asyncio
async def test_import_batch_tag_set_on_every_row(db_session):
    """Every imported row carries meta_data['import_batch'] for precise
    rollback per [[feedback_forward_compat_metadata_hook]]."""
    from backend.external_knowledge import ExternalKnowledgeSyncer
    from backend.models import KnowledgeEntry
    from scripts.seed_aqr_kelly_paper import _load_entries, IMPORT_BATCH_TAG
    from sqlalchemy import select

    entries = _load_entries()
    syncer = ExternalKnowledgeSyncer(db_session)
    await syncer.import_curated_patterns(entries, batch_id=IMPORT_BATCH_TAG)

    rows = (await db_session.execute(select(KnowledgeEntry))).scalars().all()
    assert len(rows) == 12
    for r in rows:
        m = r.meta_data or {}
        assert m.get("import_batch") == IMPORT_BATCH_TAG, (
            f"row {r.id} missing import_batch tag: {m}"
        )
        # paper_citation + theoretical_anchor forward-compat fields present
        assert m.get("paper_citation"), f"missing paper_citation: {m}"
        assert m.get("theoretical_anchor"), f"missing theoretical_anchor: {m}"
