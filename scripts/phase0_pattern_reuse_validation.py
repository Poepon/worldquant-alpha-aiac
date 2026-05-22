#!/usr/bin/env python
"""Phase 0 — proven-structure reuse validation (pivot to idea quality).

Question this answers BEFORE building any SUCCESS_PATTERN-reuse pipeline:
    Does instantiating a PROVEN alpha structure with the CURRENT dataset's
    fields carry edge (PASS-grade fitness), or does the edge live in the
    original field choice and NOT transfer through the structure?

GO  → reuse (B) clearly beats the free-form baseline → build Phase 1.
NO-GO → B ≈ baseline → edge is field-specific, structure reuse is futile.

Default is DRY-RUN (instantiate + parse-check + print, NO BRAIN). Pass
--simulate to burn quota (N + optional N sims, kept small). Prefer running
when the mining worker is paused/idle (shared BRAIN session — 401 thrash).

Usage:
    python scripts/phase0_pattern_reuse_validation.py
    python scripts/phase0_pattern_reuse_validation.py --simulate --part-a
    python scripts/phase0_pattern_reuse_validation.py --n 20 --min-sharpe 1.5 --simulate
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import re
import statistics as st
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_env() -> Dict[str, str]:
    env: Dict[str, str] = {}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, ".env"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def _connect(env: Dict[str, str]):
    import psycopg2

    return psycopg2.connect(
        host=env["POSTGRES_SERVER"], port=int(env["POSTGRES_PORT"]),
        user=env["POSTGRES_USER"], password=env["POSTGRES_PASSWORD"],
        dbname=env["POSTGRES_DB"],
    )


from backend.alpha_semantic_validator import AlphaSemanticValidator  # noqa: E402

_VALIDATOR = AlphaSemanticValidator()


def extract_fields(expression: str) -> List[str]:
    ops = _VALIDATOR._extract_operators(expression or "")
    return sorted(_VALIDATOR._extract_fields(expression or "", ops))


def substitute_fields(expression: str, mapping: Dict[str, str]) -> str:
    out = expression
    for old, new in mapping.items():
        out = re.sub(rf"\b{re.escape(old)}\b", new, out)
    return out


def grammar_ok(expression: str) -> Tuple[bool, str]:
    try:
        from backend.services.grammar_validator import validate as _gv

        r = _gv(expression)
        return bool(r.ok), (r.error_msg or "")
    except Exception as e:
        return True, f"(grammar check skipped: {e})"


def pull_proven_structures(cur, min_sharpe: float, limit: int) -> List[Tuple[str, float]]:
    cur.execute(
        """
        SELECT DISTINCT ON (expression) expression, is_sharpe
        FROM alphas
        WHERE quality_status IN ('PASS','PASS_PROVISIONAL')
          AND is_sharpe >= %s AND expression IS NOT NULL
          AND expression NOT LIKE '%%vec_%%'
          AND expression NOT LIKE '%%(...)%%'
          AND expression NOT LIKE '%%;%%'
        ORDER BY expression, is_sharpe DESC
        LIMIT %s;
        """,
        (min_sharpe, limit * 4),
    )
    rows = []
    for e, s in cur.fetchall():
        e = " ".join((e or "").split())  # (c) collapse newlines / multiple spaces
        flds = extract_fields(e)
        # (c) skip structures with a non-builtin group token (e.g. cap_group) —
        # blunt field-substitution would mangle the group-arg position.
        if not flds or any(f.endswith("_group") for f in flds):
            continue
        rows.append((e, s))
    rows.sort(key=lambda r: -(r[1] or 0))
    return rows[:limit]


def pull_field_pool(cur, region: str, universe: str) -> List[str]:
    cur.execute(
        """
        SELECT expression FROM alphas
        WHERE region = %s AND universe = %s AND is_sharpe IS NOT NULL
          AND expression NOT LIKE '%%vec_%%'
          AND created_at > now() - interval '7 days';
        """,
        (region, universe),
    )
    pool = set()
    for (e,) in cur.fetchall():
        for f in extract_fields(e or ""):
            if f.endswith("_group"):  # (c) exclude group fields as substitutes
                continue
            pool.add(f)
    return sorted(pool)


def summarize(pairs: List[Tuple[Optional[float], Optional[float]]]) -> Dict:
    sh = [p[0] for p in pairs if p[0] is not None]
    fit = [p[1] for p in pairs if p[1] is not None]
    n = len(pairs)
    passgate = sum(
        1 for s, f in pairs
        if s is not None and f is not None and s >= 1.25 and f >= 1.0
    )
    return {
        "n": n,
        "avg_sharpe": round(st.mean(sh), 3) if sh else None,
        "avg_fitness": round(st.mean(fit), 3) if fit else None,
        "max_sharpe": round(max(sh), 2) if sh else None,
        "pass_gate": passgate,
        "pass_gate_pct": round(100 * passgate / n, 1) if n else 0.0,
    }


def free_form_baseline(cur, region: str, universe: str) -> Dict:
    cur.execute(
        """
        SELECT is_sharpe, is_fitness FROM alphas
        WHERE region = %s AND universe = %s AND is_sharpe IS NOT NULL
          AND task_id IS NOT NULL AND parent_alpha_id IS NULL
          AND expression NOT LIKE 'multiply(-1,%%'
          AND created_at >= '2026-05-20';
        """,
        (region, universe),
    )
    return summarize([(s, f) for s, f in cur.fetchall()])


def instantiate(structure: str, pool: List[str], rng: random.Random) -> Optional[str]:
    flds = extract_fields(structure)
    if not flds or len(pool) < len(flds):
        return None
    chosen = rng.sample(pool, len(flds))
    return substitute_fields(structure, dict(zip(flds, chosen)))


async def simulate_batch(exprs: List[str], region: str, universe: str):
    from backend.adapters.brain_adapter import BrainAdapter
    from backend.sim_settings import smart_simulation_settings

    out = []
    async with BrainAdapter() as brain:
        for i, expr in enumerate(exprs, 1):
            try:
                s = smart_simulation_settings(expr, region=region, universe=universe)
                res = await brain.simulate_alpha(expression=expr, **s)
                if not res.get("success"):
                    out.append((expr, None, None, None))
                    note = f"sim-fail {str(res.get('error') or res.get('detail') or '')[:50]}"
                else:
                    m = res.get("metrics") or {}
                    out.append((expr, m.get("sharpe"), m.get("fitness"), m.get("turnover")))
                    note = f"decay={s.get('decay')}"
                print(f"    [{i}/{len(exprs)}] sh={out[-1][1]} fit={out[-1][2]} {note} | {expr[:55]}", flush=True)
            except Exception as e:
                out.append((expr, None, None, None))
                print(f"    [{i}/{len(exprs)}] EXC {type(e).__name__}: {str(e)[:60]}", flush=True)
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=15)
    ap.add_argument("--region", default="USA")
    ap.add_argument("--universe", default="TOP3000")
    ap.add_argument("--min-sharpe", type=float, default=1.5)
    ap.add_argument("--simulate", action="store_true", help="hit BRAIN (default dry-run)")
    ap.add_argument("--part-a", action="store_true", help="also re-run winners as-is")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    env = _load_env()
    conn = _connect(env)
    cur = conn.cursor()

    print("=" * 72)
    print(f"Phase 0 reuse validation  {args.region}/{args.universe}  "
          f"mode={'SIMULATE' if args.simulate else 'DRY-RUN'}")
    print("=" * 72)

    baseline = free_form_baseline(cur, args.region, args.universe)
    print(f"\n[baseline] free-form recent (post-5/20): {baseline}")

    structures = pull_proven_structures(cur, args.min_sharpe, args.n)
    pool = pull_field_pool(cur, args.region, args.universe)
    print(f"[setup] proven structures={len(structures)}  field-pool={len(pool)}")
    if not structures or not pool:
        print("  ⚠️ insufficient data — abort.")
        return

    instantiated: List[str] = []
    print("\n[Part B] proven structure × current fields:")
    for struct, sh in structures:
        inst = instantiate(struct, pool, rng)
        if not inst:
            continue
        ok, msg = grammar_ok(inst)
        print(f"  proven(sh={sh:.2f}): {struct[:48]}")
        print(f"     → [{'ok' if ok else 'PARSE-FAIL:'+msg[:25]}] {inst[:66]}")
        if ok:
            instantiated.append(inst)
    print(f"\n[Part B] {len(instantiated)} parseable re-fielded structures.")

    if not args.simulate:
        print("\nDRY-RUN done. Review above. Re-run with --simulate to measure "
              f"PASS-grade vs free-form baseline ({baseline['pass_gate_pct']}%).")
        return

    print(f"\n[Part B] simulating {len(instantiated)} on BRAIN…")
    partB = summarize([(r[1], r[2]) for r in await simulate_batch(instantiated, args.region, args.universe)])

    sumA = None
    if args.part_a:
        print(f"\n[Part A] re-running {len(structures)} winners as-is…")
        sumA = summarize([(r[1], r[2]) for r in await simulate_batch([s for s, _ in structures], args.region, args.universe)])

    print("\n" + "=" * 72 + "\nVERDICT\n" + "=" * 72)
    fmt = lambda lbl, d: f"  {lbl:<32}{d['n']:<5}{str(d['avg_sharpe']):<9}{str(d['avg_fitness']):<9}{d['pass_gate']}({d['pass_gate_pct']}%)"
    print(f"  {'arm':<32}{'n':<5}{'avg_sh':<9}{'avg_fit':<9}pass_gate(sh>=1.25&fit>=1.0)")
    print(fmt("free-form (baseline)", baseline))
    print(fmt("reuse: structure+new fields (B)", partB))
    if sumA:
        print(fmt("reuse: winners as-is (A)", sumA))
    print("\n  GO  if B avg_fitness / pass_gate clearly > free-form baseline.")
    print("  NO-GO if B ≈ baseline → edge is field-specific, reuse won't help.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
