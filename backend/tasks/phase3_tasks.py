"""Phase 3 readiness — periodic check tasks.

Plan v5+ §Phase 3 prep T02: weekly auto-run of phase3_readiness_check
script, output written to docs/phase3_readiness/<date>.json so the
trajectory of GO/NO-GO over time is visible.

Schedule: Monday 04:00 Asia/Shanghai (after weekend, before any Phase 3
discussion meeting). See backend/celery_app.py beat_schedule.
"""
from __future__ import annotations

import asyncio
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

from loguru import logger

from backend.celery_app import celery_app


_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "docs" / "phase3_readiness"


@celery_app.task(name="backend.tasks.run_phase3_readiness_check")
def run_phase3_readiness_check():
    """Run phase3_readiness_check.py in --json mode and persist output.

    Returns: dict with overall=GO/NO-GO + auto_passed_count + json_path.
    """
    # Lazy import the script's main coroutine to avoid CLI-coupling.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    try:
        from scripts.phase3_readiness_check import (
            gate_1_phase2_task_count,
            gate_2_pass_rate_parity,
            gate_3_cross_round_data,
            gate_4_brain_quota_manual,
            gate_5_llm_pricing_manual,
        )
        from sqlalchemy.ext.asyncio import create_async_engine
    except Exception as e:
        logger.error(f"[phase3_readiness] import failed: {e}")
        return {"error": str(e), "traceback": traceback.format_exc()}

    async def _run():
        engine = create_async_engine(
            "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt",
            echo=False,
        )
        try:
            async with engine.begin() as conn:
                gates = [
                    await gate_1_phase2_task_count(conn),
                    await gate_2_pass_rate_parity(conn),
                    await gate_3_cross_round_data(conn),
                    await gate_4_brain_quota_manual(),
                    await gate_5_llm_pricing_manual(),
                ]
        finally:
            await engine.dispose()
        return gates

    try:
        gates = asyncio.run(_run())
    except Exception as e:
        logger.error(f"[phase3_readiness] gate execution failed: {e}")
        return {"error": str(e), "traceback": traceback.format_exc()}

    auto_passed = sum(1 for g in gates if g.get("passed") is True)
    auto_failed = sum(1 for g in gates if g.get("passed") is False)
    manual = sum(1 for g in gates if g.get("passed") is None)
    overall = "GO" if (auto_failed == 0 and auto_passed >= 3) else "NO-GO"

    payload = {
        "overall": overall,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "auto_passed_count": auto_passed,
        "auto_failed_count": auto_failed,
        "manual_pending_count": manual,
        "gates": gates,
    }

    # Persist to docs/phase3_readiness/<date>.json
    try:
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _OUTPUT_DIR / f"{datetime.utcnow().strftime('%Y-%m-%d')}.json"
        out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        logger.info(
            f"[phase3_readiness] {overall} | passed={auto_passed} failed={auto_failed} "
            f"manual={manual} | output={out_path}"
        )
        payload["json_path"] = str(out_path)
    except Exception as e:
        logger.warning(f"[phase3_readiness] write output failed: {e}")
        payload["write_error"] = str(e)

    return payload
