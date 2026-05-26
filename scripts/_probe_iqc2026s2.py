"""One-shot probe: does competitions/IQC2026S2 before-and-after-performance work,
and did BRAIN bring back the `score` field with the new season?

Bypasses get_before_and_after_performance (which returns {} when stats missing,
masking the real shape) and hits raw _safe_api_call directly.
"""
import asyncio
import json

from backend.adapters.brain_adapter import BrainAdapter

ALPHA_ID = "QP2xRAKX"
SCOPE = "competitions/IQC2026S2"
ENDPOINT = f"/{SCOPE}/alphas/{ALPHA_ID}/before-and-after-performance"


async def main():
    async with BrainAdapter() as ba:
        for i in range(30):
            resp = await ba._safe_api_call("GET", ENDPOINT)
            ra = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            print(f"poll={i} status={resp.status_code} retry-after={ra!r}")
            if resp.status_code != 200:
                print("BODY:", resp.text[:800])
                return
            if not ra or ra == "0":
                data = resp.json() or {}
                print("\n=== TOP-LEVEL KEYS ===", list(data.keys()))
                print("HAS 'score'? ->", "score" in data, "value:", data.get("score"))
                print("partitionName ->", data.get("partitionName"))
                stats = data.get("stats") or {}
                print("\nstats.before:", json.dumps(stats.get("before"), ensure_ascii=False))
                print("stats.after :", json.dumps(stats.get("after"), ensure_ascii=False))
                # slim: replace big arrays so we can eyeball the rest
                slim = {
                    k: ("<schema+records omitted>" if k in ("pnl", "yearlyStats", "partition") else v)
                    for k, v in data.items()
                }
                print("\nSLIM PAYLOAD:", json.dumps(slim, ensure_ascii=False)[:2000])
                return
            await asyncio.sleep(float(ra))
        print("polled 30x without completion")


if __name__ == "__main__":
    asyncio.run(main())
