"""One-shot / poll verification probe for a FLAT pipeline session.

Usage: python scripts/_poll_session.py <task_id> [poll]
  no 2nd arg  -> one-shot snapshot
  "poll"      -> poll every 60s until status != RUNNING (max ~55min), then snapshot

Verifies: Option C breadth (distinct datasets among alphas + trace JSONB),
SAVE_RESULTS trace tail, orphan-run (exactly 1 run, not stuck RUNNING).
"""
import json
import os
import sys
import time

import psycopg2


def load_env(path):
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = load_env(os.path.join(root, ".env"))
TID = int(sys.argv[1])
POLL = len(sys.argv) > 2 and sys.argv[2] == "poll"


def conn():
    return psycopg2.connect(
        host=env.get("POSTGRES_SERVER", "localhost"),
        port=int(env.get("POSTGRES_PORT", "5433")),
        user=env.get("POSTGRES_USER", "postgres"),
        password=env.get("POSTGRES_PASSWORD", ""),
        dbname=env.get("POSTGRES_DB", "alpha_gpt"),
    )


def status(cur):
    cur.execute("SELECT status FROM mining_tasks WHERE id=%s", (TID,))
    r = cur.fetchone()
    return r[0] if r else None


def snapshot(cur):
    print(f"\n========== task {TID} verification ==========")
    cur.execute("SELECT status, config FROM mining_tasks WHERE id=%s", (TID,))
    st, cfg = cur.fetchone()
    cfg = cfg if isinstance(cfg, dict) else {}
    print(f"status={st}  config.enable_sim_pipeline={cfg.get('enable_sim_pipeline')} "
          f"config.delay={cfg.get('delay')}  stop_reason={cfg.get('stop_reason')}")

    print("\n-- experiment_runs (orphan-run check: expect exactly 1, not stuck RUNNING) --")
    cur.execute("SELECT id,status,started_at,finished_at FROM experiment_runs "
                "WHERE task_id=%s ORDER BY id", (TID,))
    runs = cur.fetchall()
    for r in runs:
        print(f"  run {r[0]}: status={r[1]} started={r[2]} finished={r[3]}")
    print(f"  -> {len(runs)} run(s)")

    print("\n-- alphas (persisted PASS/PROVISIONAL) by dataset (Option C breadth) --")
    cur.execute("SELECT dataset_id, count(*), round(avg(is_sharpe)::numeric,2), "
                "round(max(is_sharpe)::numeric,2) FROM alphas WHERE task_id=%s "
                "GROUP BY dataset_id ORDER BY count(*) DESC", (TID,))
    arows = cur.fetchall()
    for ds, n, avgs, maxs in arows:
        print(f"  {ds}: n={n} avg_sharpe={avgs} max_sharpe={maxs}")
    cur.execute("SELECT count(*), count(distinct dataset_id) FROM alphas WHERE task_id=%s", (TID,))
    na, nds = cur.fetchone()
    print(f"  -> {na} alphas across {nds} distinct dataset(s)")

    print("\n-- alpha_failures (count; no dataset_id column) --")
    cur.execute("SELECT count(*), count(distinct error_type) FROM alpha_failures WHERE task_id=%s", (TID,))
    nf, _net = cur.fetchone()
    cur.execute("SELECT error_type, count(*) FROM alpha_failures WHERE task_id=%s "
                "GROUP BY error_type ORDER BY count(*) DESC LIMIT 6", (TID,))
    print(f"  total failures={nf}  by error_type: " +
          ", ".join(f"{et}={c}" for et, c in cur.fetchall()))

    print("\n-- trace_steps by type (SAVE_RESULTS tail check) --")
    cur.execute("SELECT step_type, count(*) FROM trace_steps WHERE task_id=%s "
                "GROUP BY step_type ORDER BY count(*) DESC", (TID,))
    for stp, c in cur.fetchall():
        print(f"  {stp}: {c}")
    cur.execute("SELECT count(distinct iteration) FROM trace_steps WHERE task_id=%s", (TID,))
    print(f"  -> {cur.fetchone()[0]} distinct iteration(s) (F3: one per candidate)")

    # Breadth from trace JSONB: scan input/output for a dataset_id key.
    print("\n-- breadth from trace_steps JSONB (dataset_id seen in gen trace) --")
    cur.execute("SELECT input_data, output_data FROM trace_steps WHERE task_id=%s "
                "AND step_type IN ('RAG_QUERY','HYPOTHESIS','CODE_GEN') LIMIT 400", (TID,))
    seen = {}
    for inp, outp in cur.fetchall():
        for blob in (inp, outp):
            if isinstance(blob, dict):
                for key in ("dataset_id", "dataset", "datasets"):
                    v = blob.get(key)
                    if isinstance(v, str):
                        seen[v] = seen.get(v, 0) + 1
                    elif isinstance(v, list):
                        for x in v:
                            if isinstance(x, str):
                                seen[x] = seen.get(x, 0) + 1
    if seen:
        for ds, c in sorted(seen.items(), key=lambda kv: -kv[1]):
            print(f"  {ds}: {c}")
        print(f"  -> {len(seen)} distinct dataset(s) seen in gen trace")
    else:
        print("  (no dataset_id key found in trace JSONB — breadth not measurable here)")


def main():
    c = conn()
    cur = c.cursor()
    if POLL:
        deadline = time.time() + 55 * 60
        while time.time() < deadline:
            st = status(cur)
            cur.execute("SELECT count(*) FROM alphas WHERE task_id=%s", (TID,))
            na = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM alpha_failures WHERE task_id=%s", (TID,))
            nf = cur.fetchone()[0]
            print(f"[{time.strftime('%H:%M:%S')}] status={st} alphas={na} failures={nf}", flush=True)
            if st != "RUNNING":
                print(f"\n*** session ended (status={st}) ***", flush=True)
                break
            c.commit()  # release snapshot so next poll sees fresh committed rows
            time.sleep(60)
    snapshot(cur)
    c.close()


if __name__ == "__main__":
    main()
