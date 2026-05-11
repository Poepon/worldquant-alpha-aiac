"""V-22.2 backfill — rewrite KB SUCCESS_PATTERN entries with NL pattern to canonical skeleton.

The historical feedback_agent.learn_from_round writer stored the LLM's raw
"pattern" string (often natural-language prose like "ts_decay_linear of
ts_mean with sentiment vectors") in KnowledgeEntry.pattern. The V-22 chain
update_pattern_brain_status uses expression_to_skeleton(alpha.expression) ==
KB.pattern lookup to match — NL prose collapses to "FIELD" and never matches.

This script:
  1. Scans SUCCESS_PATTERN entries whose pattern is NOT canonical-skeleton-shaped
     (heuristic: missing "(", or expression_to_skeleton(pattern) returns FIELD/
     UNKNOWN, or contains lowercase placeholders like "field"/"window").
  2. For each such entry, looks up the source alpha via meta_data.alpha_id_ref
     (or alpha_id), pulls alpha.expression, computes canonical skeleton, and
     UPDATEs pattern.
  3. If a canonical sibling already exists with the same skeleton, the legacy
     NL entry is soft-deactivated (is_active=False) instead of being merged
     blindly.
  4. Dry-run by default; --apply to commit.

Usage:
  venv/Scripts/python.exe scripts/v22_backfill_kb_skeletons.py          # dry run
  venv/Scripts/python.exe scripts/v22_backfill_kb_skeletons.py --apply  # commit
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys

from sqlalchemy import select, update

sys.path.insert(0, ".")

from backend.database import AsyncSessionLocal
from backend.knowledge_extraction import expression_to_skeleton
from backend.models import Alpha, KnowledgeEntry


# A pattern is "canonical-shaped" if it looks like an op-tree skeleton:
#   - contains balanced parens
#   - uses uppercase placeholders (FIELD/NUM) or specific operator names
# Heuristic: must contain "(" AND skeletonizing it doesn't collapse to FIELD/UNKNOWN.
def is_canonical_pattern(pattern: str) -> bool:
    if not pattern or "(" not in pattern:
        return False
    try:
        sk = expression_to_skeleton(pattern)
    except Exception:
        return False
    if sk in ("FIELD", "UNKNOWN", ""):
        return False
    return True


_LOWERCASE_PLACEHOLDER = re.compile(
    r"\b(field|window|short_window|long_window|shorter_window|longer_window|"
    r"smoothing|short_decay|long_decay|param|placeholder|x|y|z)\b",
    re.IGNORECASE,
)


def has_lowercase_placeholders(pattern: str) -> bool:
    """Detect 'field', 'window' etc. placeholders that LLM emits but that
    expression_to_skeleton does not standardize to FIELD/NUM.

    Such patterns are syntactically expression-like but functionally identical
    to NL — they cannot match real alpha skeletons via the V-22 lookup.
    """
    return bool(_LOWERCASE_PLACEHOLDER.search(pattern or ""))


async def main(apply: bool):
    print(f"=== V-22.2 KB skeleton backfill (apply={apply}) ===\n")
    stats = {
        "scanned": 0,
        "already_canonical": 0,
        "fixed": 0,
        "deactivated_dup": 0,
        "no_alpha_ref": 0,
        "alpha_not_found": 0,
        "alpha_no_expression": 0,
        "skeleton_failed": 0,
    }

    async with AsyncSessionLocal() as db:
        stmt = (
            select(KnowledgeEntry)
            .where(KnowledgeEntry.entry_type == "SUCCESS_PATTERN")
            .where(KnowledgeEntry.is_active == True)  # noqa: E712
            .order_by(KnowledgeEntry.id.desc())
        )
        rows = (await db.execute(stmt)).scalars().all()
        print(f"Scanning {len(rows)} active SUCCESS_PATTERN entries...\n")

        for entry in rows:
            stats["scanned"] += 1
            if is_canonical_pattern(entry.pattern) and not has_lowercase_placeholders(entry.pattern):
                stats["already_canonical"] += 1
                continue

            md = entry.meta_data or {}
            alpha_pk = md.get("alpha_id_ref")
            alpha_brain_id = md.get("alpha_id")  # legacy field

            alpha = None
            if isinstance(alpha_pk, int):
                alpha = await db.get(Alpha, alpha_pk)
            if alpha is None and alpha_brain_id:
                # Fallback: lookup by BRAIN alpha_id
                a_row = (
                    await db.execute(
                        select(Alpha).where(Alpha.alpha_id == alpha_brain_id).limit(1)
                    )
                ).scalar_one_or_none()
                alpha = a_row

            if alpha is None:
                stats["no_alpha_ref"] += 1 if not (alpha_pk or alpha_brain_id) else 0
                stats["alpha_not_found"] += 1 if (alpha_pk or alpha_brain_id) else 0
                if stats["scanned"] <= 30:
                    print(f"  KB#{entry.id} no source alpha (ref={alpha_pk}, brain_id={alpha_brain_id}) — skip")
                continue

            if not alpha.expression:
                stats["alpha_no_expression"] += 1
                continue

            try:
                canonical = expression_to_skeleton(alpha.expression)
            except Exception as e:
                stats["skeleton_failed"] += 1
                print(f"  KB#{entry.id} skeleton failed: {e}")
                continue

            if canonical in ("FIELD", "UNKNOWN", ""):
                stats["skeleton_failed"] += 1
                continue

            # Check if a canonical sibling already exists
            sibling = (
                await db.execute(
                    select(KnowledgeEntry)
                    .where(KnowledgeEntry.entry_type == "SUCCESS_PATTERN")
                    .where(KnowledgeEntry.pattern == canonical)
                    .where(KnowledgeEntry.is_active == True)  # noqa: E712
                    .where(KnowledgeEntry.id != entry.id)
                    .limit(1)
                )
            ).scalar_one_or_none()

            if sibling is not None:
                # Canonical version already exists — soft-deactivate the NL entry
                if apply:
                    entry.is_active = False
                    entry.meta_data = {
                        **(entry.meta_data or {}),
                        "v22_2_deactivated_reason": "duplicate of canonical sibling",
                        "v22_2_canonical_sibling_id": sibling.id,
                    }
                stats["deactivated_dup"] += 1
                if stats["deactivated_dup"] <= 10:
                    print(
                        f"  [DUP-DEACT] KB#{entry.id} {entry.pattern[:50]!r}\n"
                        f"             → sibling KB#{sibling.id} has canonical {canonical[:50]!r}"
                    )
            else:
                # Rewrite this entry's pattern to canonical
                if apply:
                    old_pattern = entry.pattern
                    entry.pattern = canonical
                    entry.meta_data = {
                        **(entry.meta_data or {}),
                        "v22_2_backfilled_at": "2026-05-11",
                        "v22_2_old_pattern": old_pattern,
                    }
                stats["fixed"] += 1
                if stats["fixed"] <= 10:
                    print(
                        f"  [FIX] KB#{entry.id} {entry.pattern[:55]!r} → {canonical[:55]!r}"
                    )

        if apply:
            await db.commit()
            print("\n[apply] DB committed.")
        else:
            print("\n[dry-run] no changes committed. Re-run with --apply to commit.")

    print("\n=== Summary ===")
    for k, v in stats.items():
        print(f"  {k:25s} {v}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="commit changes (default: dry-run)")
    args = ap.parse_args()
    asyncio.run(main(apply=args.apply))
