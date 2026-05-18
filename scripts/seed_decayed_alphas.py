"""Phase 2 Q9 — Seed Decayed Alpha KB entries (one-shot, 2026-05-18).

Loads backend/data/decayed_alphas_seed.json and UPSERTs each entry into
knowledge_entries with entry_type='FAILURE_PITFALL' (semantically a
"post-pub decayed pattern — avoid"). Idempotent via pattern_hash UNIQUE.

Per master plan §4.4 Q9:
- 50+ Decayed Alpha seed (McLean-Pontiff 2016 + Hou-Xue-Zhang 2020 + others)
- meta_data carries decay_pct + failure_mode + theoretical_anchor + t_stat_orig
- forward-compat metadata hook per [[feedback_forward_compat_metadata_hook]]

Run:
    python scripts/seed_decayed_alphas.py [--dry-run]

GO gate (master plan §9.4 R5 + Q9):
    SELECT count(*) FROM knowledge_entries
     WHERE meta_data->>'import_batch' = 'phase2_q9_decayed_2026_05_18'
       AND is_active = true;
    -- expect: 50+
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Repo root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker

from backend.config import settings
from backend.models.knowledge import KnowledgeEntry, compute_pattern_hash
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")

DATA_FILE = Path(__file__).resolve().parents[1] / "backend" / "data" / "decayed_alphas_seed.json"
IMPORT_BATCH = "phase2_q9_decayed_2026_05_18"
# McLean-Pontiff 2016 + Hou-Xue-Zhang 2020 reference set is academically US-focused
# (CRSP/Compustat). Default region "USA" unless an entry overrides via "region"/"regions".
DEFAULT_REGION = "USA"


def _load_seed_data() -> dict:
    with DATA_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_regions(entry: dict) -> list[str]:
    """Resolve region list for an entry.

    Priority:
      1. entry["regions"]: List[str]  (explicit list)
      2. entry["region"]: str          (single region)
      3. DEFAULT_REGION                (USA — academic decayed reference set)

    Returns a non-empty List[str] of uppercased region codes.
    """
    raw_regions = entry.get("regions")
    if isinstance(raw_regions, list) and raw_regions:
        return [str(r).upper() for r in raw_regions]
    raw_region = entry.get("region")
    if isinstance(raw_region, str) and raw_region.strip():
        return [raw_region.strip().upper()]
    return [DEFAULT_REGION]


async def seed(dry_run: bool = False) -> dict:
    data = _load_seed_data()
    entries = data["entries"]
    logger.info(f"Loaded {len(entries)} Decayed Alpha entries from {DATA_FILE.name}")

    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    inserted = 0
    updated = 0
    skipped = 0

    async with maker() as db:
        for entry in entries:
            pattern = entry["pattern"]
            regions = _resolve_regions(entry)
            # Per-region row: pattern_hash includes region so cross-region
            # re-seed INSERTs new rows instead of UPDATE-overwriting the
            # USA row's meta_data (fix HIGH-#1 from code review a425937..HEAD).
            primary_region = regions[0]
            phash = compute_pattern_hash(pattern, primary_region, None)

            # Idempotent check via pattern_hash (now per-region scoped)
            existing = (await db.execute(
                select(KnowledgeEntry).where(KnowledgeEntry.pattern_hash == phash)
            )).scalar_one_or_none()

            meta_data = {
                "import_batch": IMPORT_BATCH,
                "source": "decayed_alpha_seed",
                "name": entry["name"],
                "decay_pct": entry["decay_pct"],
                "failure_mode": entry["failure_mode"],
                "theoretical_anchor": entry["theoretical_anchor"],
                "t_stat_orig": entry["t_stat_orig"],
                # String "true" to match the convention used by
                # backend.agents.hierarchical_rag DECAYED_KEY check
                # (`str(md.get(DECAYED_KEY, "")).lower() == "true"`) and
                # all existing test fixtures in test_rag_hierarchical_pr1.py.
                "decayed": "true",
                # Region tagging so R8 hierarchical RAG L3 region filter
                # (backend/agents/hierarchical_rag.py:228) treats these
                # entries as region-scoped instead of region-agnostic.
                # Convention from backend/agents/knowledge_seed.py — list form.
                "region": primary_region,
                "regions": regions,
                # Forward-compat metadata hook (per [[feedback_forward_compat_metadata_hook]])
                # pattern_operators populated lazily by future re-classify pass
                "pattern_operators_pending": True,
            }

            if existing is not None:
                if dry_run:
                    logger.info(f"  [dry-run] would UPDATE id={existing.id} name={entry['name']}")
                else:
                    existing.meta_data = {**(existing.meta_data or {}), **meta_data}
                    existing.description = entry["description"]
                    existing.is_active = True
                    updated += 1
            else:
                if dry_run:
                    logger.info(f"  [dry-run] would INSERT name={entry['name']} pattern={pattern[:60]}...")
                else:
                    db.add(KnowledgeEntry(
                        entry_type="FAILURE_PITFALL",
                        pattern=pattern,
                        pattern_hash=phash,
                        description=entry["description"],
                        meta_data=meta_data,
                        is_active=True,
                        created_by="DECAYED_ALPHA_SEED",
                    ))
                    inserted += 1
        if not dry_run:
            await db.commit()

    await engine.dispose()

    result = {
        "total_entries": len(entries),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "dry_run": dry_run,
    }
    logger.info(f"Done: {result}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview without DB writes")
    args = parser.parse_args()

    result = asyncio.run(seed(dry_run=args.dry_run))
    return 0 if (result["inserted"] + result["updated"] + result["skipped"]) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
