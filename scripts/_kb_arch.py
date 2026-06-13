import asyncio, asyncpg
async def main():
    c=await asyncpg.connect(user='postgres',password='postgres',host='localhost',port=5433,database='alpha_gpt')
    # KB 相关表 + 行数 + 最后写
    tabs=await c.fetch("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND (table_name ILIKE '%knowledge%' OR table_name ILIKE '%hypoth%' OR table_name ILIKE '%r1a%' OR table_name ILIKE '%r8%' OR table_name ILIKE '%rag%' OR table_name ILIKE '%pattern%' OR table_name ILIKE '%feedback%' OR table_name ILIKE '%attribution%' OR table_name ILIKE '%failure%') ORDER BY table_name")
    print("=== KB 相关表 (行数 / 最后写) ===")
    for t in [r['table_name'] for r in tabs]:
        try:
            has_created = await c.fetchval(f"SELECT 1 FROM information_schema.columns WHERE table_name='{t}' AND column_name='created_at'")
            if has_created:
                r=await c.fetchrow(f"SELECT count(*) n, max(created_at) last FROM {t}")
                live='🟢' if r['last'] and str(r['last'])>='2026-06-06' else '⚪'
                print(f"  {live} {t}: {r['n']} rows, last {str(r['last'])[:19]}")
            else:
                n=await c.fetchval(f"SELECT count(*) FROM {t}")
                print(f"  ? {t}: {n} rows (no created_at)")
        except Exception as e: print(f"  {t}: err {str(e)[:40]}")
    # knowledge_entries 按 type 拆
    print("\n=== knowledge_entries by entry_type ===")
    cols=await c.fetch("SELECT column_name FROM information_schema.columns WHERE table_name='knowledge_entries'")
    cn=[r['column_name'] for r in cols]
    typecol = 'entry_type' if 'entry_type' in cn else ('knowledge_type' if 'knowledge_type' in cn else ('type' if 'type' in cn else None))
    print(f"  (cols: {cn})")
    if typecol:
        rows=await c.fetch(f"SELECT {typecol} t, count(*) n, max(created_at) last FROM knowledge_entries GROUP BY {typecol} ORDER BY n DESC")
        for r in rows: print(f"  {r['t']}: {r['n']} (last {str(r['last'])[:19]})")
    await c.close()
asyncio.run(main())
