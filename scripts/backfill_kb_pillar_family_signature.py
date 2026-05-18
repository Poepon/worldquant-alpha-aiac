"""Phase 3 R8 PR1: backfill KnowledgeEntry.meta_data with pillar +
family_signature + fields_used for legacy 3K+ entries (2026-05-18).

Per plan v1.0 §3 + §12: existing KB entries (Q1/Q2/Q3/Q6/Q9/forum
imports) predate R8 schema concepts. Hierarchical RAG L1 (pillar) +
L2 (family_signature) JOIN on these meta_data keys; without backfill
they'd miss those rows.

Idempotent: re-runnable. Sets meta_data['pattern_operators_pending']=False
after processing, and recomputes from scratch if family_signature missing.

Run:
    python scripts/backfill_kb_pillar_family_signature.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Repo root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from backend.config import settings
from backend.models.knowledge import KnowledgeEntry

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")


def _needs_backfill(entry: KnowledgeEntry) -> bool:
    """True when meta_data missing any of pillar/family_signature/fields_used."""
    md = entry.meta_data if isinstance(entry.meta_data, dict) else {}
    return (
        md.get("pillar_classified") is None
        or md.get("family_signature") is None
        or md.get("fields_used") is None
        or md.get("pattern_operators_pending") is True
    )


def _compute_backfill(entry: KnowledgeEntry) -> dict:
    """Compute R8 meta_data keys from pattern text. Returns merge dict."""
    from backend.pillar_classifier import infer_pillar
    from backend.family_classifier import family_signature
    from backend.agents.hierarchical_rag import extract_fields_for_rag

    pattern = entry.pattern or ""
    try:
        pillar = infer_pillar(expression=pattern)
    except Exception:
        pillar = "other"
    try:
        fam_sig = family_signature(pattern)
    except Exception:
        fam_sig = "<empty>"
    try:
        fields = extract_fields_for_rag(pattern)
    except Exception:
        fields = []

    return {
        "pillar_classified": pillar,
        "family_signature": fam_sig,
        "fields_used": fields,
        "pattern_operators_pending": False,
        "r8_backfilled_at": "2026-05-18",
    }


async def backfill(dry_run: bool = False) -> dict:
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    stats = {"scanned": 0, "needs_backfill": 0, "updated": 0, "errors": 0}

    async with maker() as db:
        # Stream all active entries
        rows = (await db.execute(
            select(KnowledgeEntry).where(KnowledgeEntry.is_active == True)  # noqa: E712
        )).scalars().all()
        stats["scanned"] = len(rows)
        logger.info(f"Scanning {len(rows)} active KB entries")

        for entry in rows:
            if not _needs_backfill(entry):
                continue
            stats["needs_backfill"] += 1
            try:
                merge = _compute_backfill(entry)
                if dry_run:
                    logger.debug(f"  [dry] id={entry.id} → {merge}")
                else:
                    new_md = dict(entry.meta_data) if isinstance(entry.meta_data, dict) else {}
                    new_md.update(merge)
                    entry.meta_data = new_md
                    stats["updated"] += 1
            except Exception as e:
                stats["errors"] += 1
                logger.warning(f"  id={entry.id} backfill failed: {e}")
        if not dry_run:
            await db.commit()

    await engine.dispose()
    logger.info(f"Backfill done: {stats}")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview without DB writes")
    args = parser.parse_args()
    stats = asyncio.run(backfill(dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
