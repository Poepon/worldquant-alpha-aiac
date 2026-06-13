import asyncio, asyncpg, os
from datetime import datetime, timedelta

async def main():
    conn = await asyncpg.connect(
        user="postgres", password="postgres", host="localhost",
        port=5433, database="alpha_gpt")
    # 1. worker restart 锚点：今天 fresh alpha 总量 + 时间范围
    row = await conn.fetchrow("""
        SELECT count(*) n, min(created_at) mn, max(created_at) mx
        FROM alphas WHERE created_at >= now() - interval '36 hours' AND task_id IS NOT NULL
    """)
    print(f"[36h fresh] n={row['n']} from={row['mn']} to={row['mx']}")

    # 2. per-dataset sharpe / can_submit 分布 (fresh 36h)
    print("\n[per-dataset, fresh 36h] dataset | n | mean_sh | max_sh | pos_sh% | can_submit")
    rows = await conn.fetch("""
        SELECT dataset_id,
               count(*) n,
               round(avg((metrics->>'sharpe')::float)::numeric,3) mean_sh,
               round(max((metrics->>'sharpe')::float)::numeric,3) max_sh,
               round((100.0*sum(CASE WHEN (metrics->>'sharpe')::float>0 THEN 1 ELSE 0 END)/count(*))::numeric,1) pos_pct,
               sum(CASE WHEN (metrics->>'can_submit')::boolean THEN 1 ELSE 0 END) cs
        FROM alphas
        WHERE created_at >= now() - interval '36 hours' AND task_id IS NOT NULL
          AND metrics->>'sharpe' IS NOT NULL
        GROUP BY dataset_id ORDER BY n DESC LIMIT 25
    """)
    for r in rows:
        print(f"  {str(r['dataset_id'])[:20]:20} | {r['n']:4} | {r['mean_sh']} | {r['max_sh']} | {r['pos_pct']}% | cs={r['cs']}")

    # 3. 整体 fresh yield: can_submit 率 + mean sharpe
    row = await conn.fetchrow("""
        SELECT count(*) n,
               round(avg((metrics->>'sharpe')::float)::numeric,3) mean_sh,
               sum(CASE WHEN (metrics->>'can_submit')::boolean THEN 1 ELSE 0 END) cs,
               round((100.0*sum(CASE WHEN (metrics->>'sharpe')::float>0 THEN 1 ELSE 0 END)/count(*))::numeric,2) pos_pct
        FROM alphas WHERE created_at >= now() - interval '36 hours' AND task_id IS NOT NULL
          AND metrics->>'sharpe' IS NOT NULL
    """)
    print(f"\n[OVERALL fresh 36h] n={row['n']} mean_sh={row['mean_sh']} pos%={row['pos_pct']} can_submit={row['cs']}  yield={100.0*(row['cs'] or 0)/max(row['n'],1):.2f}%")

    # 4. |sharpe| 分布 (标定 SHARPE_SCALE 用)
    row = await conn.fetchrow("""
        SELECT round(percentile_cont(0.5) WITHIN GROUP (ORDER BY abs((metrics->>'sharpe')::float))::numeric,3) p50,
               round(percentile_cont(0.9) WITHIN GROUP (ORDER BY abs((metrics->>'sharpe')::float))::numeric,3) p90,
               round(percentile_cont(0.95) WITHIN GROUP (ORDER BY abs((metrics->>'sharpe')::float))::numeric,3) p95
        FROM alphas WHERE created_at >= now() - interval '36 hours' AND task_id IS NOT NULL
          AND metrics->>'sharpe' IS NOT NULL
    """)
    print(f"[|sharpe| dist fresh] p50={row['p50']} p90={row['p90']} p95={row['p95']}")

    # 5. 对比基线: 36h-前 vs 36h-72h (regime check)
    row = await conn.fetchrow("""
        SELECT count(*) n, round(avg((metrics->>'sharpe')::float)::numeric,3) mean_sh,
               sum(CASE WHEN (metrics->>'can_submit')::boolean THEN 1 ELSE 0 END) cs
        FROM alphas WHERE created_at >= now() - interval '72 hours' AND created_at < now() - interval '36 hours'
          AND task_id IS NOT NULL AND metrics->>'sharpe' IS NOT NULL
    """)
    print(f"[prior 36-72h baseline] n={row['n']} mean_sh={row['mean_sh']} can_submit={row['cs']}")

    await conn.close()

asyncio.run(main())
