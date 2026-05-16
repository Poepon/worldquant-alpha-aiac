"""Daily Regime-Inference task (P2-C, 2026-05-16).

来源: docs/alphagbm_skills_research_2026-05-15.md skills `vix-status` +
`duan-analysis`.

Schedule: 10:30 Asia/Shanghai daily (registered in ``backend/celery_app.py``).
Sequence relative to siblings:
  - 08:00  run_alpha_health_check    (the data source for this task)
  - 08:30  run_hypothesis_health_check
  - 09:00  run_pillar_balance_check
  - 09:30  run_negative_knowledge_extract
  - 10:00  run_macro_narrative_extract
  - 10:30  THIS task — runs LAST so the morning alpha_health JSON is on disk

Reads the last 7 daily ``docs/alpha_health_check/<sh-date>.json`` blobs per
region, EWMA-smooths the GREEN+YELLOW pass-rate into a 5-bucket regime
label, writes the result to Redis (24h TTL), and emits a per-day archive
``docs/regime_state/<sh-date>.json``.

Read-mostly: only writes Redis keys ``aiac:current_regime:{region}`` and
``aiac:regime_snapshot:{region}`` (+ the on-disk JSON archive). Never
touches alphas / knowledge_entries / hypotheses.

The task is gated by ``ENABLE_REGIME_INFERENCE`` (default False, S1) —
flipping that flag to True is the first step of the P2-C onboarding.
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


_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "docs" / "regime_state"

# Asia/Shanghai is UTC+8 year-round (no DST). Mirrors the sibling extract
# tasks so the daily filename matches the Celery beat's local-time fire.
SH_TZ = timezone(timedelta(hours=8))

_REPORT_SCHEMA_VERSION = "p2c.v1"

# Default regions swept on every run. Override by editing this list (or
# providing a per-run argument in a future revision). Each is best-effort:
# a region with no alpha_health data falls back to a cold-start ``normal``
# snapshot rather than aborting the sweep.
_ACTIVE_REGIONS: List[str] = ["USA", "CHN", "EUR", "ASI", "GLB"]


@celery_app.task(name="backend.tasks.run_regime_infer")
def run_regime_infer():
    """Sync Celery entrypoint — delegates to the async runner."""
    return run_async(_run_async())


async def _run_async() -> Dict[str, Any]:
    """Inference sweep over active regions + archive emission."""
    from backend.config import settings
    from backend.database import AsyncSessionLocal
    from backend.services.regime_inference_service import (
        RegimeInferenceService,
    )

    if not bool(getattr(settings, "ENABLE_REGIME_INFERENCE", False)):
        return {"status": "skipped", "reason": "ENABLE_REGIME_INFERENCE=False"}

    now_utc = datetime.now(timezone.utc)
    sh_now = now_utc.astimezone(SH_TZ)

    per_region: Dict[str, Any] = {}
    errors: List[str] = []

    try:
        async with AsyncSessionLocal() as db:
            svc = RegimeInferenceService(db)
            for region in _ACTIVE_REGIONS:
                try:
                    snap = await svc.infer_current_regime(region=region)
                    write_status = await svc.write_regime_state(
                        region=region, snapshot=snap,
                    )
                    per_region[region] = {
                        **snap,
                        "_write": write_status,
                    }
                except Exception as ex:
                    logger.warning(
                        f"[regime_infer] region={region} failed (non-fatal): {ex}"
                    )
                    errors.append(f"{region}:{ex}")
                    per_region[region] = {
                        "regime": "normal",
                        "cold_start": True,
                        "error": str(ex),
                    }

        payload: Dict[str, Any] = {
            "report_date": sh_now.strftime("%Y-%m-%d"),
            "generated_at_utc": now_utc.isoformat(),
            "schema_version": _REPORT_SCHEMA_VERSION,
            "regions": per_region,
            "errors": errors,
        }
    except Exception as e:
        logger.error(f"[regime_infer] sweep failed: {e}")
        return {
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc(),
        }

    # ---- archive ----
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
            f"[regime_infer] swept {len(per_region)} regions → {out_path}"
        )
    except Exception as e:
        logger.warning(f"[regime_infer] write output failed: {e}")
        payload["write_error"] = str(e)

    return {
        "status": "ok",
        "report_date": payload.get("report_date"),
        "regions": {
            r: {
                "regime": payload["regions"][r].get("regime"),
                "cold_start": payload["regions"][r].get("cold_start"),
                "confidence": payload["regions"][r].get("confidence"),
            }
            for r in payload["regions"]
        },
        "errors": payload.get("errors", []),
        "json_path": payload.get("json_path"),
    }
