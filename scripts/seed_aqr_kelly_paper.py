"""Phase 4 Sprint 1 A4 — AQR / Bryan Kelly research-paper KB seed script.

Plan: docs/phase4_a_b_plan_v5_2026-05-19.md §6.4

Reads backend/data/aqr_kelly_seed.json (5 SSRN papers × 1-3 hypothesis each)
and UPSERTs each entry into knowledge_entries via
ExternalKnowledgeSyncer.import_curated_patterns(). Idempotent — the
underlying _pattern_hash_exists check skips rows already imported (per
W3-frozen pattern_hash on (pattern, region, dataset_id)).

Usage
-----
::

    # Dry-run: count what would be imported, no DB writes
    python scripts/seed_aqr_kelly_paper.py --dry-run

    # Actually import
    python scripts/seed_aqr_kelly_paper.py

Both modes print a summary report. The import_batch tag
``aqr_kelly_2026_05_20`` is recorded in every entry's meta_data for
precise rollback::

    DELETE FROM knowledge_entries
    WHERE meta_data->>'import_batch' = 'aqr_kelly_2026_05_20';

Spike check post-import: scripts/sprint0_baseline_spike.py SQL #3
(`SQL_SENTINEL_STAMP_PRESENCE` is unrelated; this script verifies via
`kb_total_entries` baseline increment of 8-12 rows depending on dedup).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

# Allow running as a top-level script
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.database import AsyncSessionLocal
from backend.external_knowledge import ExternalKnowledge, ExternalKnowledgeSyncer


SEED_JSON = _REPO_ROOT / "backend" / "data" / "aqr_kelly_seed.json"
IMPORT_BATCH_TAG = "aqr_kelly_2026_05_20"


def _load_entries() -> List[ExternalKnowledge]:
    """Parse the JSON file → list of ExternalKnowledge.

    Filters out any object whose first key starts with '_' (those are
    header comments / meta blocks added for human readability).
    Tolerant of missing optional fields — required only: pattern,
    description. Source defaults to 'paper'.
    """
    if not SEED_JSON.exists():
        raise FileNotFoundError(f"Seed JSON missing: {SEED_JSON}")

    raw = json.loads(SEED_JSON.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Seed JSON top-level must be a list")

    entries: List[ExternalKnowledge] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        # Skip header / meta blocks (first key starts with _)
        if all(k.startswith("_") for k in item.keys()):
            continue

        # Required fields
        if "pattern" not in item or "description" not in item:
            print(
                f"WARNING: skipping entry without pattern/description: "
                f"{item.get('source_title', '?')}",
                file=sys.stderr,
            )
            continue

        entries.append(ExternalKnowledge(
            source=item.get("source", "paper"),
            pattern=str(item["pattern"]),
            description=str(item["description"]),
            category=item.get("category", "other"),
            confidence=float(item.get("confidence", 0.75)),
            verified=bool(item.get("verified", False)),
            source_url=item.get("source_url", ""),
            source_title=item.get("source_title", ""),
            extraction_date=datetime.now(timezone.utc).replace(tzinfo=None),
            # Q2 dual-path field — when True, written as ANCHOR_METADATA
            # (R8 SQL excludes); when False/None, written as SUCCESS_PATTERN
            is_anchor_metadata=item.get("is_anchor_metadata"),
            paper_citation=item.get("paper_citation"),
            theoretical_anchor=item.get("theoretical_anchor"),
            region=item.get("region", "USA"),
        ))
    return entries


async def _run(*, dry_run: bool = False) -> Dict[str, Any]:
    entries = _load_entries()
    summary: Dict[str, Any] = {
        "seed_json": str(SEED_JSON),
        "import_batch_tag": IMPORT_BATCH_TAG,
        "total_entries_in_json": len(entries),
        "by_entry_type": {
            "SUCCESS_PATTERN": sum(1 for e in entries if not e.is_anchor_metadata),
            "ANCHOR_METADATA": sum(1 for e in entries if e.is_anchor_metadata),
        },
        "by_category": {},
        "dry_run": dry_run,
        "imported": 0,
        "already_present": 0,
    }
    for e in entries:
        summary["by_category"][e.category] = (
            summary["by_category"].get(e.category, 0) + 1
        )

    if dry_run:
        return summary

    async with AsyncSessionLocal() as db:
        syncer = ExternalKnowledgeSyncer(db)
        imported = await syncer.import_curated_patterns(
            entries, batch_id=IMPORT_BATCH_TAG,
        )
        summary["imported"] = imported
        summary["already_present"] = len(entries) - imported
    return summary


def _print_summary(s: Dict[str, Any]) -> None:
    print("=" * 60)
    print("AQR / Bryan Kelly KB seed — Phase 4 Sprint 1 A4")
    print("=" * 60)
    print(f"Seed JSON         : {s['seed_json']}")
    print(f"Import batch tag  : {s['import_batch_tag']}")
    print(f"Entries in JSON   : {s['total_entries_in_json']}")
    print(f"  SUCCESS_PATTERN : {s['by_entry_type']['SUCCESS_PATTERN']}")
    print(f"  ANCHOR_METADATA : {s['by_entry_type']['ANCHOR_METADATA']}")
    print(f"By category       : {dict(sorted(s['by_category'].items()))}")
    print(f"Dry run           : {s['dry_run']}")
    if not s["dry_run"]:
        print(f"Imported (new)    : {s['imported']}")
        print(f"Already present   : {s['already_present']}")
    print("=" * 60)
    print("Rollback (if needed):")
    print(
        f"  DELETE FROM knowledge_entries "
        f"WHERE meta_data->>'import_batch' = '{s['import_batch_tag']}';"
    )
    print("=" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse JSON + count entries, do not write to DB.",
    )
    args = parser.parse_args()

    try:
        summary = asyncio.run(_run(dry_run=args.dry_run))
    except Exception as ex:
        print(f"FATAL: seed run failed: {ex}", file=sys.stderr)
        return 1

    _print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
