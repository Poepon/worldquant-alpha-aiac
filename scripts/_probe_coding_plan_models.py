"""Probe which Coding Plan (coding.dashscope) models are reachable + incumbent
reachability check. Thin wrapper over benchmark_llm_per_node's shared helpers —
no quota-heavy generation, just models.list() / per-model 1-token probes.

    venv/Scripts/python.exe scripts/_probe_coding_plan_models.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for sibling import

import benchmark_llm_per_node as B  # noqa: E402


async def main() -> int:
    from backend.agents.services.llm_service import LLMService
    svc = LLMService(provider="openai")
    base_url = await B.point_at_coding_plan(svc)
    print(f"endpoint = {base_url}\n")

    cat = await B.probe_catalog(svc, B.CATALOG)
    live_map = await B.load_live_map()

    incumbents = {nk: B.incumbent_for(nk, live_map) for nk in
                  ["code_gen", "hypothesis", "self_correct", "r1b_retry", "r5_alignment_c1",
                   "llm_mutate_alpha", "llm_crossover_alpha", "r1b_mutate", "r5_alignment_c2",
                   "attribution", "distill_context", "__default__"]}
    broken = {}
    for nk, m in incumbents.items():
        if m and m not in cat["reachable"] and not await B._probe_one(svc, m):
            broken[nk] = m

    report = {"endpoint": base_url, **cat, "live_incumbents": incumbents,
              "BROKEN_incumbents_not_on_catalog": broken}
    print(json.dumps(report, indent=2, default=str))
    if broken:
        print(f"\n⚠ BROKEN INCUMBENTS — these nodes route to a model NOT on the Coding Plan: {broken}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
