"""一键只读诊断:task 为什么不产 alpha / 回测 step 为什么失败 (2026-05-21).

动机:task 3329/3332 排查时,靠"手测 authenticate()"诊断反而踢掉了运行中的
BRAIN session、制造了正在查的现象。这个脚本沿固定的 Q0→Q3 决策树走,
**全程只读、绝不新登录**(账号探测只用 Redis 里已有的共享 session 验证),
30 秒给出"根因在哪一层"的确定结论,不靠猜、不踢 session。

用法:
    python scripts/diagnose_mining_stall.py <task_id>
    python scripts/diagnose_mining_stall.py <task_id> --no-net   # 跳过 BRAIN 只读探测

决策树:
  Q0  alpha 卡在哪一层?  (real-sim / PRESIM_SKIP / DEDUP_SKIP / PENDING-retryable)
  Q1  账号/共享 session   (只读 GET /authentication, 用 Redis 现有 cookie)
  Q2  circuit:brain_auth  (state / trip_count / reopens_in)
  → 结论 + 下一步
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from backend.database import AsyncSessionLocal  # noqa: E402

BASE_URL = "https://api.worldquantbrain.com"
_HDR = {
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://platform.worldquantbrain.com",
    "Referer": "https://platform.worldquantbrain.com/",
    "Accept": "application/json;version=2.0",
}


def _sync_redis():
    try:
        import redis
        from backend.config import settings
        return redis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception as e:
        print(f"  (redis unavailable: {e})")
        return None


def _mins_ago(ts) -> str:
    if ts is None:
        return "n/a"
    try:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        return f"{int(delta.total_seconds() // 60)} min ago"
    except Exception:
        return "?"


async def _q0_layer(db, task_id: int) -> None:
    print("\n[Q0] alpha 卡在哪一层?  (最近活动)")
    # 真跑过 BRAIN 的 alpha(有 sharpe = simulate 真返回了 metrics)
    real = (await db.execute(text("""
        select count(*) from alphas
        where task_id=:t and is_sharpe is not null
    """), {"t": task_id})).scalar() or 0
    pend_retry = (await db.execute(text("""
        select count(*) from alphas
        where task_id=:t and (metrics->>'_sim_retryable')='true'
    """), {"t": task_id})).scalar() or 0
    presim_a = (await db.execute(text("""
        select count(*) from alphas
        where task_id=:t and (metrics->>'_pre_brain_skip')='true'
    """), {"t": task_id})).scalar() or 0
    try:
        af = dict((r[0], r[1]) for r in (await db.execute(text("""
            select error_type, count(*) from alpha_failures where task_id=:t group by error_type
        """), {"t": task_id})).all())
    except Exception:
        af = {}
    presim = presim_a + af.get("PRESIM_SKIP", 0)
    dedup = af.get("DEDUP_SKIP", 0)

    print(f"   real BRAIN sim (有 sharpe):           {real}")
    print(f"   PENDING + retryable (BRAIN 401/429):  {pend_retry}   <-- 真碰了 BRAIN 又失败")
    print(f"   PRESIM_SKIP (本地预筛, 没碰 BRAIN):    {presim}")
    print(f"   DEDUP_SKIP  (重复生成, 没碰 BRAIN):    {dedup}")
    if af:
        print(f"   alpha_failures error_type: {af}")

    # 判定
    if pend_retry > 0 and pend_retry >= max(presim, dedup):
        print("   => 判定: BRAIN 侧 (simulate 真打到 BRAIN 又 401/429) -> 看 Q1/Q2")
    elif dedup > 0 and dedup >= presim:
        print("   => 判定: 重复生成 (多样性枯竭) -> 治上游, 与 BRAIN 无关")
    elif presim > 0:
        print("   => 判定: 本地预筛拦截 (pre-sim filter) -> 没碰 BRAIN, 查 pass_probability/阈值")
    elif real > 0:
        print("   => 判定: 真跑了 BRAIN, 多为质量 FAIL -> 是 alpha 质量问题, BRAIN 正常")
    else:
        print("   => 判定: 该 task 暂无可分类的 alpha 活动")


def _q1_session_probe(r, do_net: bool) -> None:
    print("\n[Q1] 账号 / 共享 session  (只读, 绝不新登录)")
    if r is None:
        print("   redis 不可用, 跳过")
        return
    raw = r.get("brain_session:cookies")
    ttl = r.ttl("brain_session:cookies")
    print(f"   redis brain_session:cookies: present={bool(raw)} ttl={ttl}")
    if not raw:
        print("   => 共享 session 为空! 没有任何进程成功写入 session "
              "(可能 _save 失败 / 死锁期没人补) -> 多源互踢或 cookie 写入失败")
        return
    if not do_net:
        print("   (--no-net: 跳过 BRAIN 只读探测)")
        return
    try:
        import httpx
        ck = json.loads(raw)
        c = httpx.Client(headers=_HDR, timeout=15)
        c.cookies.update(ck)
        resp = c.get(f"{BASE_URL}/authentication")  # 只读, 不创建新 session
        if resp.status_code == 200:
            exp = resp.json().get("token", {}).get("expiry")
            print(f"   只读 GET /authentication -> 200 (expiry={exp}s)  共享 session 有效")
            print("   => 账号 OK 且共享 session 健康. 若 task 仍失败, 多半是 Q0 上游或瞬时被踢")
        else:
            print(f"   只读 GET /authentication -> {resp.status_code} {resp.text[:120]}")
            print("   => 共享 session 已失效(被踢/过期). 账号本身另需单独确认 "
                  "(本脚本不新登录以免再踢)")
    except Exception as e:
        print(f"   BRAIN 探测失败: {e}")


def _q2_circuit(r) -> None:
    print("\n[Q2] circuit:brain_auth")
    if r is None:
        print("   redis 不可用, 跳过")
        return
    raw = r.get("circuit:brain_auth")
    if not raw:
        print("   state=closed (healthy)")
        return
    try:
        d = json.loads(raw)
        reopen = max(0, int((d.get("until_ts") or 0) - time.time()))
        print(f"   state={d.get('state')} trip_count={d.get('trip_count')} "
              f"reason={d.get('last_failure_reason')!r} reopens_in={reopen}s")
        print("   => circuit 非 closed: 有进程的 simulate 真打 BRAIN 后 401 被 trip. "
              "结合 Q1: session 有效却仍 trip = 有源在踢(uvicorn/外部); "
              "session 为空 = 写入失败/死锁")
    except Exception:
        print(f"   raw={raw[:120]}")


async def main() -> int:
    ap = argparse.ArgumentParser(description="只读诊断 mining task 不产 alpha 的根因")
    ap.add_argument("task_id", type=int)
    ap.add_argument("--no-net", action="store_true", help="跳过 BRAIN 只读探测(纯 DB+Redis)")
    args = ap.parse_args()

    print(f"=== diagnose_mining_stall  task={args.task_id}  (READ-ONLY, 不新登录) ===")
    async with AsyncSessionLocal() as db:
        row = (await db.execute(text("""
            select status, schedule, region, last_alpha_persisted_at, updated_at
            from mining_tasks where id=:t
        """), {"t": args.task_id})).first()
        if not row:
            print(f"task {args.task_id} NOT FOUND")
            return 1
        print(f"[task] status={row[0]} schedule={row[1]} region={row[2]}")
        print(f"       last_alpha_persisted={row[3]} ({_mins_ago(row[3])})")
        print(f"       updated={row[4]} ({_mins_ago(row[4])})")
        await _q0_layer(db, args.task_id)

    r = _sync_redis()
    _q1_session_probe(r, do_net=not args.no_net)
    _q2_circuit(r)
    print("\n=== 下一步: 若 Q0=BRAIN 且 Q2 反复 trip -> 找踢源(grep authenticate / 确认外部登录); "
          "若 Q0=重复生成/预筛 -> 治上游, 与 BRAIN 无关. 全程未触碰 session. ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
