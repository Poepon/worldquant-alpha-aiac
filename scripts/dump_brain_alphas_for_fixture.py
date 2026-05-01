"""Dump BRAIN user alphas to a JSON fixture for offline classifier tests.

Run this once (or monthly) to refresh the test fixture file. The fixture
captures ~4135 historical alpha expressions and is used by
backend/tests/test_factor_tier_classifier.py to verify classify_tier
matches real BRAIN data, not just hand-crafted samples.

Usage:
    python scripts/dump_brain_alphas_for_fixture.py
    python scripts/dump_brain_alphas_for_fixture.py --output backend/tests/fixtures/brain_alphas_4135.json
    python scripts/dump_brain_alphas_for_fixture.py --limit 500  # smaller dump for quick iteration

Requires BRAIN_EMAIL / BRAIN_PASSWORD env vars (or .env). The dump is
network-bound and rate-limited by BRAIN. Expect 5-15 minutes for 4000+ alphas.

Output schema (each alpha):
    {
      "alpha_id": "abc123",
      "expression": "ts_rank(close, 20)",
      "region": "USA",
      "universe": "TOP3000",
      "stage": "OS" | "IS",
      "is_sharpe": 1.4,
      "is_fitness": 0.95,
      "is_turnover": 0.32,
      "date_created": "2025-12-01T...",
    }
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Dict, List

from loguru import logger

from backend.adapters.brain_adapter import BrainAdapter


PAGE_SIZE = 100
DEFAULT_OUTPUT = Path("backend/tests/fixtures/brain_alphas_4135.json")


async def dump_alphas(adapter: BrainAdapter, limit: int) -> List[Dict]:
    out: List[Dict] = []
    offset = 0
    while len(out) < limit:
        page_size = min(PAGE_SIZE, limit - len(out))
        try:
            response = await adapter.get_user_alphas(limit=page_size, offset=offset)
        except Exception as e:
            logger.error(f"[fixture-dump] page offset={offset} failed: {e}")
            break
        items = response.get("results") if isinstance(response, dict) else response
        if not items:
            logger.info(f"[fixture-dump] no more results at offset={offset}, stopping")
            break
        for item in items:
            settings = item.get("settings") or {}
            is_block = item.get("is") or {}
            out.append(
                {
                    "alpha_id": item.get("id"),
                    "expression": (item.get("regular") or {}).get("code")
                    or item.get("expression"),
                    "region": settings.get("region"),
                    "universe": settings.get("universe"),
                    "stage": item.get("stage"),
                    "is_sharpe": is_block.get("sharpe"),
                    "is_fitness": is_block.get("fitness"),
                    "is_turnover": is_block.get("turnover"),
                    "date_created": item.get("dateCreated"),
                }
            )
        offset += page_size
        if len(items) < page_size:
            logger.info(f"[fixture-dump] partial page returned ({len(items)} < {page_size}), stopping")
            break
        logger.info(f"[fixture-dump] {len(out)} alphas fetched so far")
    return out


async def main(output: Path, limit: int) -> None:
    adapter = BrainAdapter()
    await adapter.login()
    alphas = await dump_alphas(adapter, limit=limit)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        json.dump(alphas, f, indent=2, default=str)
    print(f"Wrote {len(alphas)} alphas to {output.resolve()}")


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=4135)
    args = parser.parse_args()
    try:
        asyncio.run(main(args.output, args.limit))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    cli()
