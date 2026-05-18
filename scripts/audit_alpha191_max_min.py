"""Phase 1 Q6 review M9 (2026-05-18) — audit Alpha191 KB rows whose
``ts_max(`` / ``ts_min(`` patterns silently translated an element-wise
``MAX(a, b)`` / ``MIN(a, b)`` from the JoinQuant Alpha191 source as a
rolling window call.

Background:
    The pre-fix translator unconditionally mapped Alpha191 ``MAX`` /
    ``MIN`` ALLCAPS aliases to ``ts_max`` / ``ts_min`` (rolling). Alpha191
    actually overloads ``MAX``/``MIN`` for both rolling (``MAX(x, w)``)
    and element-wise (``MAX(a, b)``) use. Review M9 added an arity-aware
    handler ``_convert_max_min`` that emits ``max(a, b)`` for the
    element-wise case.

    137 alpha191_jq rows were imported before the fix; some may carry
    wrong semantics. This script re-translates each row from its
    persisted ``meta_data["qlib_origin"]`` using the fixed translator
    and compares against the stored ``pattern``. Diffs indicate the row
    was affected by the M9 trap.

Usage:
    # Read-only (default): print summary + first 10 affected rows
    python scripts/audit_alpha191_max_min.py

    # Apply: UPDATE knowledge_entries.pattern + meta_data['m9_audit_fixed']=true
    python scripts/audit_alpha191_max_min.py --apply

Connection:
    Uses the standard `backend.config.settings.SQLALCHEMY_DATABASE_URI`
    so it picks up whatever .env the rest of the backend uses. No
    secrets are read by this script directly.

Idempotent: re-runnable. Rows already tagged
``meta_data['m9_audit_fixed'] = True`` are skipped in --apply mode.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import List, Tuple

# Repo root on sys.path so backend.* imports work
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.config import settings  # noqa: E402
from backend.models.knowledge import KnowledgeEntry  # noqa: E402
from backend.qlib_translator import translate  # noqa: E402

logger = logging.getLogger("audit_alpha191_max_min")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s"
)


def _retranslate(qlib_origin: str) -> Tuple[str, str]:
    """Run the fixed translator on a qlib_origin string. Returns
    ``(new_brain_expr, error_msg)``. error_msg is empty on success.

    Mirrors the normalize → translate pipeline that
    ``scripts/extract_alpha191.py`` applied at import time, so the
    comparison is apples-to-apples.
    """
    try:
        # Reuse the same normalizer the original extractor used so we
        # only diff against translator behavior changes, not normalize
        # changes.
        from scripts.extract_alpha191 import normalize_formula
        normalized = normalize_formula(qlib_origin)
        new_expr = translate(normalized)
        return new_expr, ""
    except Exception as ex:  # noqa: BLE001
        return "", f"{type(ex).__name__}: {str(ex)[:120]}"


async def audit(apply: bool) -> dict:
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    stats = {
        "scanned": 0,
        "alpha191_rows": 0,
        "no_origin": 0,
        "retranslate_failed": 0,
        "no_diff": 0,
        "affected": 0,
        "updated": 0,
        "already_fixed": 0,
    }
    examples: List[Tuple[int, str, str, str]] = []  # (id, old_pattern, new_pattern, qlib_origin)

    async with maker() as db:
        # Pull all active KB entries; filter to alpha191 source in Python
        # since the JSONB filter syntax varies by dialect (this script
        # runs in dev too, where sqlite may stand in).
        rows = (
            await db.execute(
                select(KnowledgeEntry).where(KnowledgeEntry.is_active == True)  # noqa: E712
            )
        ).scalars().all()
        stats["scanned"] = len(rows)

        for entry in rows:
            md = entry.meta_data if isinstance(entry.meta_data, dict) else {}
            source = md.get("source") or ""
            if source != "alpha191_jq":
                continue
            stats["alpha191_rows"] += 1

            if md.get("m9_audit_fixed") is True:
                stats["already_fixed"] += 1
                continue

            qlib_origin = md.get("qlib_origin")
            if not qlib_origin:
                stats["no_origin"] += 1
                continue

            new_expr, err = _retranslate(qlib_origin)
            if err:
                stats["retranslate_failed"] += 1
                logger.debug(f"  id={entry.id} retranslate failed: {err}")
                continue

            # Compare with stored pattern. Both strings should already
            # be in the same canonical form (the original extractor
            # also called translate()), so a literal string diff is the
            # right comparator.
            if (entry.pattern or "").strip() == new_expr.strip():
                stats["no_diff"] += 1
                continue

            stats["affected"] += 1
            if len(examples) < 10:
                examples.append((entry.id, entry.pattern or "", new_expr, qlib_origin))

            if apply:
                # In-place update + meta_data tag for idempotency.
                from sqlalchemy.orm.attributes import flag_modified
                entry.pattern = new_expr
                new_md = dict(md)
                new_md["m9_audit_fixed"] = True
                new_md["m9_audit_old_pattern"] = entry.pattern  # for rollback
                entry.meta_data = new_md
                flag_modified(entry, "meta_data")
                stats["updated"] += 1

        if apply and stats["updated"] > 0:
            await db.commit()

    await engine.dispose()
    return {"stats": stats, "examples": examples}


def _print_report(result: dict, apply: bool) -> None:
    stats = result["stats"]
    examples = result["examples"]
    logger.info("=" * 72)
    logger.info("Alpha191 M9 audit %s", "(APPLY)" if apply else "(READ-ONLY)")
    logger.info("=" * 72)
    for k, v in stats.items():
        logger.info("  %-22s %s", k, v)
    if examples:
        logger.info("First %d affected rows:", len(examples))
        for rid, old, new, origin in examples:
            logger.info("  id=%s", rid)
            logger.info("    qlib_origin: %s", (origin or "")[:160])
            logger.info("    OLD pattern: %s", (old or "")[:160])
            logger.info("    NEW pattern: %s", (new or "")[:160])
    else:
        logger.info("No affected rows.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually UPDATE affected rows in-place. Default: read-only.",
    )
    args = parser.parse_args()

    result = asyncio.run(audit(apply=args.apply))
    _print_report(result, apply=args.apply)
    # Also dump machine-readable summary for downstream tooling
    print(json.dumps({"stats": result["stats"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
