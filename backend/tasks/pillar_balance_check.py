"""Daily Pillar Balance check (P2-B, 2026-05-15).

来源: docs/alphagbm_skills_research_2026-05-15.md skill `compare`.

Schedule: 09:00 Asia/Shanghai daily (registered in
``backend/celery_app.py``). Sequence relative to siblings:
  - 08:00  run_alpha_health_check
  - 08:30  run_hypothesis_health_check
  - 09:00  this task — runs AFTER both health checks so any sync-induced
           metric refreshes have settled before the alpha+hypothesis JOIN.

Pure read-only — never mutates ``alphas`` / ``hypotheses`` / KB. Output is
one JSON file ``docs/pillar_balance/<asia-shanghai-date>.json`` per run.
Same-day re-runs (manual ``send_task``) overwrite.

The JOIN uses ``outerjoin`` so legacy alphas with ``hypothesis_id IS NULL``
land in the ``unknown`` bucket. For those rows we run
``infer_pillar(expression=...)`` to attribute them in the report — but we
do NOT UPDATE the hypothesis table (avoids hypothesis_status_transitions
audit noise on what is essentially a read-only inference).
"""
from __future__ import annotations

import json
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

from loguru import logger

from backend.celery_app import celery_app
from backend.tasks import run_async


_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "docs" / "pillar_balance"

# Asia/Shanghai is UTC+8 year-round (no DST). Mirrors alpha_health_service
# SH_TZ so the daily filename matches the Celery beat's local-time fire.
SH_TZ = timezone(timedelta(hours=8))


@celery_app.task(name="backend.tasks.run_pillar_balance_check")
def run_pillar_balance_check():
    """Sync Celery entrypoint — delegates to the async runner."""
    return run_async(_run_async())


async def _run_async() -> Dict[str, Any]:
    """Thin wrapper around PillarService.compute_balance_report.

    The aggregation logic was moved to backend/services/pillar_service.py
    in P3 (ops dashboard) so /ops/pillar-balance can recompute the same
    report on demand without spawning a Celery task. This task is now
    only responsible for the side effects:
      - constructing the AsyncSession
      - persisting the JSON to docs/pillar_balance/<sh-date>.json
      - shaping the small dict that ends up in the Celery result backend

    Byte-for-byte report equivalence with the pre-refactor task output is
    enforced by backend/tests/unit/test_pillar_service_byte_for_byte.py.
    """
    from backend.database import AsyncSessionLocal
    from backend.services.pillar_service import PillarService

    payload: Dict[str, Any] = {}
    try:
        async with AsyncSessionLocal() as db:
            payload = await PillarService(db).compute_balance_report()
    except Exception as e:
        logger.error(f"[pillar_balance] run failed: {e}")
        return {"error": str(e), "traceback": traceback.format_exc()}

    # ---- persist ----
    try:
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _OUTPUT_DIR / f"{payload['report_date']}.json"
        out_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
            newline="\n",
        )
        payload["json_path"] = str(out_path)
        logger.info(
            f"[pillar_balance] regions={payload['totals']['regions_checked']} "
            f"stamped={payload['totals']['stamped_alphas']} "
            f"legacy_inferred={payload['totals']['legacy_inferred_alphas']} "
            f"→ {out_path}"
        )
    except Exception as e:
        logger.warning(f"[pillar_balance] write output failed: {e}")
        payload["write_error"] = str(e)

    return {
        "report_date": payload.get("report_date"),
        "regions_checked": payload.get("totals", {}).get(
            "regions_checked", 0,
        ),
        "stamped_alphas": payload.get("totals", {}).get(
            "stamped_alphas", 0,
        ),
        "legacy_inferred_alphas": payload.get("totals", {}).get(
            "legacy_inferred_alphas", 0,
        ),
        "json_path": payload.get("json_path"),
    }
