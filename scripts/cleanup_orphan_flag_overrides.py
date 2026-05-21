"""One-shot cleanup for orphan feature_flag_overrides rows.

After flag retirements (e.g. 2026-05-19 consolidation that removed
ENABLE_CASCADE_LEGACY / ENABLE_HIERARCHICAL_RAG_CACHE / ENABLE_R5_L2_RANKING /
ENABLE_REGIME_INFERENCE / ENABLE_REGIME_AWARE_THRESHOLDS /
ENABLE_STYLE_PRESET_GUIDANCE from SUPPORTED_FLAGS), the DB may still contain
override rows for those names. They no-op silently (filtered out by
``FeatureFlagService.load_overrides_into_cache``) but pollute the ops UI
audit history and confuse new operators.

Usage:
    # Dry-run — print orphans, no DB writes
    python scripts/cleanup_orphan_flag_overrides.py

    # Actually delete
    python scripts/cleanup_orphan_flag_overrides.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import delete, select

from backend.database import AsyncSessionLocal
from backend.models.config import FeatureFlagOverride
from backend.services.feature_flag_service import SUPPORTED_FLAGS


async def _run(apply: bool) -> int:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(FeatureFlagOverride))).scalars().all()
        orphans = [r for r in rows if r.flag_name not in SUPPORTED_FLAGS]

        if not orphans:
            print("No orphan flag overrides found.")
            return 0

        print(f"Found {len(orphans)} orphan override row(s):")
        for r in orphans:
            print(
                f"  - {r.flag_name} = {r.flag_value} "
                f"(updated_by={r.updated_by!r}, at={r.updated_at})"
            )

        if not apply:
            print("\n(dry-run — re-run with --apply to delete)")
            return 0

        orphan_names = [r.flag_name for r in orphans]
        await db.execute(
            delete(FeatureFlagOverride).where(
                FeatureFlagOverride.flag_name.in_(orphan_names)
            )
        )
        await db.commit()
        print(f"\nDeleted {len(orphans)} orphan row(s).")
        return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--apply", action="store_true",
        help="Actually delete the orphan rows (default: dry-run only).",
    )
    args = p.parse_args()
    return asyncio.run(_run(apply=args.apply))


if __name__ == "__main__":
    sys.exit(main())
