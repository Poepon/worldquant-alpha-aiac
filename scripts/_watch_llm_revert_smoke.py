"""Watch USA mining productivity post LLM revert (kimi-k2.6, 2026-06-01 19:31).

Sliding 6h window + per-hour rate. Writes a snapshot to
docs/llm_revert_smoke_2026-06-01.md on every tick.

Compares against baseline:
  pre-LLM-switch (< 5-19): 89-100% valuable rate
  reasoning-models (5-19→6-01): 0.5-1.6%
  expectation post-kimi-revert: should regress to ≥30%, ideally ≥50%
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.database import AsyncSessionLocal  # noqa: E402

REPORT = Path(__file__).resolve().parent.parent / "docs" / "llm_revert_smoke_2026-06-01.md"
TICK_SEC = 300  # 5 min


async def _snapshot():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT
              TO_CHAR(created_at, 'MM-DD HH24:MI') t,
              dataset_id,
              quality_status,
              ROUND(COALESCE(is_sharpe,0)::numeric, 2) sh,
              ROUND(COALESCE(is_fitness,0)::numeric, 2) ft
            FROM alphas
            WHERE region='USA' AND task_id IS NOT NULL
              AND created_at >= '2026-06-01 19:30'
            ORDER BY created_at DESC LIMIT 100
        """))).all()
        agg = (await db.execute(text("""
            SELECT
              COUNT(*) total,
              SUM(CASE WHEN quality_status='PASS' THEN 1 ELSE 0 END) p,
              SUM(CASE WHEN quality_status='PASS_PROVISIONAL' THEN 1 ELSE 0 END) prov,
              COUNT(DISTINCT dataset_id) n_ds
            FROM alphas
            WHERE region='USA' AND task_id IS NOT NULL
              AND created_at >= '2026-06-01 19:30'
        """))).first()
        return rows, agg


def _write(rows, agg):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    total, p, prov, n_ds = agg or (0, 0, 0, 0)
    val_pct = round(100.0 * (p + prov) / total, 1) if total else 0.0

    lines = [
        f"# LLM revert smoke watch (auto-generated)",
        "",
        f"- 最后刷新: {now}",
        f"- 窗口: 6-01 19:30(`7034050` commit)之后",
        f"- 期望: ≥30% 有价值率(预期);≥50%(回 5-19 前基线)",
        "",
        "## 当前累计",
        "",
        f"- total: **{total}**",
        f"- PASS: **{p}**",
        f"- PASS_PROVISIONAL: **{prov}**",
        f"- 有价值率: **{val_pct}%**",
        f"- 涉及 dataset: {n_ds}",
        "",
        "## 基线对比",
        "",
        f"| 窗口 | 有价值率 |",
        f"|---|---|",
        f"| < 5-15(kimi 前)| 89% |",
        f"| 5-19→6-01(reasoning)| **0.8-1.6%** |",
        f"| **6-01 19:30 后(kimi-k2.6 revert)** | **{val_pct}%** |",
        "",
        "## 最近 alpha(top 30)",
        "",
        f"| 时间 | dataset | quality | sh | ft |",
        f"|---|---|---|---|---|",
    ]
    for r in rows[:30]:
        lines.append(f"| {r[0]} | {r[1] or '-'} | {r[2]} | {r[3]} | {r[4]} |")

    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main():
    while True:
        try:
            rows, agg = await _snapshot()
            _write(rows, agg)
            ts = datetime.now().strftime('%H:%M:%S')
            total, p, prov, n_ds = agg or (0, 0, 0, 0)
            val = round(100.0 * (p + prov) / total, 1) if total else 0.0
            print(f"[{ts}] total={total} P={p} PROV={prov} val%={val} ds={n_ds}")
        except Exception as ex:  # noqa: BLE001
            print(f"[err] {ex}")
        await asyncio.sleep(TICK_SEC)


if __name__ == "__main__":
    asyncio.run(main())
