"""V-22.1 + V-22 end-to-end chain verification (post worker restart, T+30min)."""
import asyncio
import re
import sys
from datetime import datetime, timezone

from sqlalchemy import select, func, text, case

sys.path.insert(0, ".")

from backend.database import AsyncSessionLocal
from backend.models import Alpha, KnowledgeEntry, MiningTask


# 用户在 16:22 UTC 重启 worker — 但要注意 DB 时区:
# Postgres `timestamptz` + naive 比较时,naive 被解释为服务器本地时区(可能是 UTC+8)
# 安全做法:用 timestamptz 比较,显式 UTC
CUTOFF = datetime(2026, 5, 10, 16, 22, 0, tzinfo=timezone.utc)


def classify_family_from_expr(expr: str) -> str:
    if not expr:
        return "<empty>"
    expr_lower = expr.lower()
    if re.search(r"\bret\w*", expr_lower) or "returns" in expr_lower:
        return "RETURNS"
    if re.search(r"\b(close|open|high|low|vwap|volume|amount|cap)\b", expr_lower):
        return "PRICE_PV"
    if re.search(r"\bfnd\w*", expr_lower):
        return "FUNDAMENTAL"
    if re.search(r"\b(anl\w*|est\w*|fam_\w*)", expr_lower):
        return "ANALYST"
    if re.search(r"\b(snt\w*|news_\w*|social_\w*|nws\w*)", expr_lower):
        return "SENTIMENT"
    if re.search(r"\b(mdl\w*|model_\w*|composite_\w*)", expr_lower):
        return "FACTOR_COMPOSITE"
    if re.search(r"\b(opt\w*|option_\w*)", expr_lower):
        return "OPTION"
    return "OTHER"


async def main():
    print(f"=== V-22.1 + V-22 chain verification ===")
    print(f"Cutoff (worker restart): {CUTOFF.isoformat()}")
    print(f"Now (UTC):               {datetime.now(timezone.utc).isoformat()}\n")

    async with AsyncSessionLocal() as db:
        # (1) Task 384 state
        print("=" * 60)
        print("(1) Task 384 state")
        print("=" * 60)
        task = await db.get(MiningTask, 384)
        if task is None:
            print("  Task 384 NOT FOUND")
        else:
            print(f"  status                  = {task.status}")
            print(f"  cascade_phase           = {task.cascade_phase}")
            print(f"  cascade_round_idx       = {task.cascade_round_idx}")
            print(f"  progress_current        = {task.progress_current}")
            print(f"  last_alpha_persisted_at = {task.last_alpha_persisted_at}")
            print(f"  updated_at              = {task.updated_at}")
            if task.last_alpha_persisted_at is not None:
                lap = task.last_alpha_persisted_at
                if lap.tzinfo is None:
                    lap = lap.replace(tzinfo=timezone.utc)
                age_sec = (datetime.now(timezone.utc) - lap).total_seconds()
                print(f"  heartbeat age           = {age_sec:.0f}s ({'ALIVE' if age_sec < 600 else 'STALE'})")

        # (2) Latest 16 SUCCESS_PATTERN entries (regardless of cutoff — see what was actually recorded)
        print("\n" + "=" * 60)
        print("(2) Latest 16 SUCCESS_PATTERN entries (any time)")
        print("=" * 60)
        sql = text(
            """
            SELECT id, created_at,
                   pattern,
                   meta_data->>'alpha_id_ref' as alpha_id_ref,
                   meta_data->>'hypothesis_id' as hypothesis_id,
                   meta_data->>'experiment_variant' as variant,
                   meta_data->>'brain_check_at' as brain_check_at,
                   meta_data->>'brain_can_submit' as brain_can_submit
            FROM knowledge_entries
            WHERE entry_type = 'SUCCESS_PATTERN'
            ORDER BY created_at DESC
            LIMIT 16
            """
        )
        rows = (await db.execute(sql)).all()
        print(f"  Latest {len(rows)} SUCCESS_PATTERN entries:")
        for r in rows:
            tag_v22 = "✓V22.1" if r.alpha_id_ref else "  "
            tag_status = "✓BRAIN" if r.brain_check_at else "      "
            print(f"    [{tag_v22}|{tag_status}] KB#{r.id} created={r.created_at}")
            print(f"        pattern={(r.pattern or '')[:80]!r}")
            print(f"        alpha_ref={r.alpha_id_ref} hypothesis={r.hypothesis_id} variant={r.variant}")
            print(f"        brain_can_submit={r.brain_can_submit} brain_check_at={r.brain_check_at}")

        # (3) brain_check_at filled (V-22 evidence) — ANY time
        print("\n" + "=" * 60)
        print("(3) V-22 evidence — brain_check_at filled (any KB entry, any time)")
        print("=" * 60)
        sql = text(
            """
            SELECT count(*) as n
            FROM knowledge_entries
            WHERE meta_data->>'brain_check_at' IS NOT NULL
            """
        )
        n = (await db.execute(sql)).scalar()
        print(f"  Entries with brain_check_at filled: {n}")

        # (4) New alphas under task 384 since cutoff (use timestamptz literal)
        print("\n" + "=" * 60)
        print("(4) Task 384 alphas — recent activity")
        print("=" * 60)
        sql = text(
            """
            SELECT count(*) as total,
                   sum(case when quality_status='PASS' then 1 else 0 end) as pass_count,
                   sum(case when quality_status='PASS_PROVISIONAL' then 1 else 0 end) as prov_count,
                   sum(case when can_submit=true then 1 else 0 end) as can_sub,
                   max(created_at) as latest_created
            FROM alphas
            WHERE task_id = 384
            """
        )
        r = (await db.execute(sql)).one()
        print(f"  All-time | total={r.total} PASS={r.pass_count} PROV={r.prov_count} can_sub={r.can_sub} latest={r.latest_created}")

        sql_recent = text(
            """
            SELECT id, alpha_id, expression, quality_status, can_submit, created_at, factor_tier
            FROM alphas
            WHERE task_id = 384
            ORDER BY created_at DESC
            LIMIT 8
            """
        )
        rows = (await db.execute(sql_recent)).all()
        print(f"\n  Latest 8 alphas under task 384:")
        for r in rows:
            fam = classify_family_from_expr(r.expression or "")
            print(f"    id={r.id} alpha_id={r.alpha_id} tier=T{r.factor_tier} status={r.quality_status} can_sub={r.can_submit}")
            print(f"        family={fam} created={r.created_at} expr={(r.expression or '')[:80]}")


asyncio.run(main())
