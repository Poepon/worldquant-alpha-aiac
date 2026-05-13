"""V-24.E — Soft-deactivate historical FIELD_INSIGHT / HYPOTHESIS_INSIGHT.

Companion to the V-24.E feedback_agent gate (WRITE_FIELD_HYPOTHESIS_INSIGHTS).
With new writes blocked, the existing 4170 entries (708 FIELD + 3462
HYPOTHESIS) are dead weight in KB queries — they show up in scans, take
up rows, and provide no signal because rag_service has no retrieve path
for these entry_type values.

Soft-delete via is_active=False instead of DELETE so they remain in DB
for forensic auditing. RAG queries already filter is_active=True so the
patterns become invisible without losing history.

Manual run (idempotent, dry-run by default):
  venv/Scripts/python.exe scripts/deactivate_write_only_insights.py
  venv/Scripts/python.exe scripts/deactivate_write_only_insights.py --apply

Re-activate later if you wire a retrieve path:
  UPDATE knowledge_entries SET is_active=True
   WHERE entry_type IN ('FIELD_INSIGHT', 'HYPOTHESIS_INSIGHT')
     AND meta_data->>'_v24e_deactivated_at' IS NOT NULL;
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from backend.database import AsyncSessionLocal


TARGET_TYPES = ("FIELD_INSIGHT", "HYPOTHESIS_INSIGHT")


async def main(apply_changes: bool) -> int:
    async with AsyncSessionLocal() as db:
        # Preview count
        r = await db.execute(
            text(
                """
                SELECT entry_type, COUNT(*) AS n,
                       COUNT(*) FILTER (WHERE is_active=true) AS active_n
                FROM knowledge_entries
                WHERE entry_type = ANY(:types)
                GROUP BY entry_type
                """
            ),
            {"types": list(TARGET_TYPES)},
        )
        rows = list(r.all())
        print("Current state:")
        total_active = 0
        for row in rows:
            print(f"  {row.entry_type:<22s} total={row.n} active={row.active_n}")
            total_active += row.active_n
        print()
        if total_active == 0:
            print("Nothing to deactivate — all rows already inactive.")
            return 0

        if not apply_changes:
            print(f"Would deactivate {total_active} active rows.")
            print("Pass --apply to actually run the UPDATE.")
            return 0

        # Tag in meta_data + flip is_active. Explicit casts because asyncpg
        # cannot infer parameter types inside jsonb_build_object / ANY().
        marker = datetime.now(timezone.utc).isoformat()
        result = await db.execute(
            text(
                """
                UPDATE knowledge_entries
                SET is_active = false,
                    meta_data = COALESCE(meta_data, '{}'::jsonb) ||
                                jsonb_build_object('_v24e_deactivated_at',
                                                   CAST(:marker AS text))
                WHERE entry_type = ANY(CAST(:types AS text[]))
                  AND is_active = true
                """
            ),
            {"types": list(TARGET_TYPES), "marker": marker},
        )
        await db.commit()
        print(f"Deactivated {result.rowcount} rows. Marker: {marker}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Actually run UPDATE; default is dry-run")
    args = p.parse_args()
    raise SystemExit(asyncio.run(main(args.apply)))
