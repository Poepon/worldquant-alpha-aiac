"""Shadow dashboard for an ENABLE_SIM_PIPELINE FLAT session.

Usage:  python scripts/_watch_shadow.py <task_id> [util_samples]

(1) BRAIN sim-slot utilization — samples brain:concurrent_sims a few times so
    you can see how saturated the slots are (pipeline target: hover near the
    limit; legacy: sporadic 1-2).
(2) The shadow task's output: alpha count, sharpe / margin distribution,
    can_submit, delay-column verification, per-iteration SIMULATE presence.
For the R14 reward/budget calibration the margin + cost columns matter.
"""
import os
import sys
import time

import psycopg2

try:
    import redis as _redis
except Exception:  # noqa: BLE001
    _redis = None

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = {}
with open(os.path.join(root, ".env"), encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")

tid = int(sys.argv[1]) if len(sys.argv) > 1 else None
n_samples = int(sys.argv[2]) if len(sys.argv) > 2 else 10

conn = psycopg2.connect(
    host=env.get("POSTGRES_SERVER", "localhost"),
    port=int(env.get("POSTGRES_PORT", "5433")),
    user=env.get("POSTGRES_USER", "postgres"),
    password=env.get("POSTGRES_PASSWORD", ""),
    dbname=env.get("POSTGRES_DB", "alpha_gpt"),
)
cur = conn.cursor()

print("=== (1) sim-slot 利用率(brain:concurrent_sims 采样)===")
if _redis is not None:
    url = env.get("REDIS_URL") or f"redis://{env.get('REDIS_HOST','localhost')}:{env.get('REDIS_PORT','6379')}/0"
    r = _redis.from_url(url)
    samples = []
    for i in range(n_samples):
        v = r.get("brain:concurrent_sims")
        v = int(v) if v else 0
        samples.append(v)
        print(f"  t+{i*3}s: concurrent_sims={v}")
        if i < n_samples - 1:
            time.sleep(3)
    if samples:
        print(f"  → avg={sum(samples)/len(samples):.2f} max={max(samples)} "
              f"(USER 上限=3;饱和=接近 3;legacy 多为零星 1-2)")
else:
    print("  (redis 模块不可用,跳过)")

if tid is None:
    print("\n用法: python scripts/_watch_shadow.py <task_id> [util_samples]")
    cur.close(); conn.close(); sys.exit(0)

print(f"\n=== (2) shadow task {tid} 产出 ===")
cur.execute("SELECT status, config, created_at, updated_at FROM mining_tasks WHERE id=%s", (tid,))
row = cur.fetchone()
if not row:
    print(f"  task {tid} 不存在"); cur.close(); conn.close(); sys.exit(0)
status, config, created, updated = row
cfg = config or {}
print(f"  status={status} delay={cfg.get('delay','<1>')} "
      f"pipeline={cfg.get('enable_sim_pipeline', False)} "
      f"created={created:%m-%d %H:%M} updated={updated:%m-%d %H:%M}")

cur.execute(
    """SELECT count(*),
              max(abs(is_sharpe)),
              sum(CASE WHEN abs(is_sharpe)>0.5 THEN 1 ELSE 0 END),
              sum(CASE WHEN abs(is_sharpe)>1.0 THEN 1 ELSE 0 END),
              percentile_cont(0.5) WITHIN GROUP (ORDER BY is_margin),
              sum(CASE WHEN is_margin > 0.0005 THEN 1 ELSE 0 END),
              sum(CASE WHEN can_submit IS TRUE THEN 1 ELSE 0 END)
       FROM alphas WHERE task_id=%s AND is_sharpe IS NOT NULL""",
    (tid,),
)
n, mx, g05, g10, med_margin, gt5bps, cansub = cur.fetchone()
if n:
    print(f"  alpha={n}  max|sh|={mx:.3f}  >0.5={g05}({100*g05/n:.0f}%)  >1.0={g10}({100*g10/n:.0f}%)")
    print(f"  margin 中位={med_margin if med_margin is None else round(med_margin,6)}  "
          f">5bps={gt5bps}/{n}  can_submit={cansub}  "
          f"(对照 delay-1 基线 >0.5≈62% >1.0≈25%)")
else:
    print("  暂无带 sharpe 的 alpha(可能还在第一轮生成/sim)")

# delay 列 + 每 iter SIMULATE(全修复持续生效 + 无挂死)
cur.execute("SELECT delay, count(*) FROM alphas WHERE task_id=%s GROUP BY delay", (tid,))
print(f"  delay 列分布={dict(cur.fetchall())}  (delay-0 session 应全=0)")
cur.execute(
    """SELECT iteration, bool_or(step_type='SIMULATE') FROM trace_steps
       WHERE task_id=%s GROUP BY iteration ORDER BY iteration""",
    (tid,),
)
its = cur.fetchall()
no_sim = [it for it, has in its if not has]
print(f"  iters={len(its)}  无 SIMULATE 的 iter(挂死?)={no_sim or '无'}")

# 最近 SIMULATE 时长(sim 墙钟)
cur.execute(
    """SELECT duration_ms FROM trace_steps WHERE task_id=%s AND step_type='SIMULATE'
       AND duration_ms>0 ORDER BY created_at DESC LIMIT 5""",
    (tid,),
)
durs = [d[0]/1000.0 for d in cur.fetchall()]
if durs:
    print(f"  近 5 次 SIMULATE 时长(s)={[round(d) for d in durs]}")

cur.close(); conn.close()
