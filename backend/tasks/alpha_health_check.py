"""Daily alpha-library health check (P1-C, first half).

来源: docs/alphagbm_skills_research_2026-05-15.md skill `health-check`.

Schedule: 08:00 Asia/Shanghai daily (registered in
``backend/celery_app.py``). The cron is ``crontab(hour=8, minute=0)`` —
Celery 4.x+ interprets cron strings under ``celery_app.conf.timezone``
("Asia/Shanghai", set in celery_app.py L25), so this fires at 08:00 SH
local time.

Why 08:00 not 07:00:
  - 06:00  sync_datasets (BRAIN, ~10 min)
  - 06:15  refresh_kb_referenced_alphas
  - 06:30  refresh_os_correlation_cache + monitor_llm_op_hallucinations
  - 08:00  this task — 90 min buffer for the pre-tasks because the
           Windows Celery worker is ``--pool=solo`` (serial).

This task is **pure read-only**: it never writes to ``alphas``,
``hypotheses``, or ``knowledge_entries``. Output is one JSON file
``docs/alpha_health_check/<asia-shanghai-date>.json`` per run; same-day
re-runs (manual ``send_task``) overwrite. See plan
``C:\\Users\\Administrator\\.claude\\plans\\enumerated-enchanting-knuth.md``.
"""
from __future__ import annotations

import json
import traceback
from pathlib import Path

from loguru import logger

from backend.celery_app import celery_app
from backend.tasks import run_async


_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "docs" / "alpha_health_check"


@celery_app.task(name="backend.tasks.run_alpha_health_check")
def run_alpha_health_check():
    """Sync Celery entrypoint — delegates to the async runner."""
    return run_async(_run_async())


async def _run_async():
    """Run health check + persist JSON output. Returns a dict summary.

    BaselineProvider construction order is load-bearing here: we build the
    ``category_resolver`` first, then pass it via the constructor
    ``BaselineProvider(category_resolver=resolver)`` (mirrors the
    ``evaluation.py:1788`` pattern). Patching the provider's private
    ``_category_resolver`` attribute *after* construction does **not**
    work — ``BaselineProvider.__init__`` already defaulted it to a no-op
    lambda, so any later ``getattr(..., None) is None`` check fires False
    and the resolver is silently ignored.
    """
    from backend.database import AsyncSessionLocal
    from backend.services.alpha_health_service import AlphaHealthService
    from backend.agents.services.baseline_provider import BaselineProvider

    payload = None
    try:
        async with AsyncSessionLocal() as db:
            resolver = await AlphaHealthService.build_category_resolver(db)
            bp = BaselineProvider(category_resolver=resolver)
            svc = AlphaHealthService(db, baseline_provider=bp)
            payload = await svc.run_full_check()
    except Exception as e:
        logger.error(f"[alpha_health] run failed: {e}")
        return {"error": str(e), "traceback": traceback.format_exc()}

    # Persist to docs/alpha_health_check/<sh-date>.json. Filename uses the
    # service-generated ``payload['report_date']`` (already Asia/Shanghai
    # local-date string) so tests only need to inject ``now_utc`` — no
    # freezegun dependency required.
    try:
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _OUTPUT_DIR / f"{payload['report_date']}.json"
        # newline="\n": force LF on Windows so git-diff and downstream
        # tools see byte-identical files cross-platform.
        out_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
            newline="\n",
        )
        payload["json_path"] = str(out_path)
        logger.info(
            f"[alpha_health] checked={payload['totals']['checked']} "
            f"by_band={payload['totals']['by_band']} → {out_path}"
        )
    except Exception as e:
        logger.warning(f"[alpha_health] write output failed: {e}")
        payload["write_error"] = str(e)

    return {
        "checked": payload["totals"]["checked"],
        "by_band": payload["totals"]["by_band"],
        "kb_orphans_outside_scope": len(
            payload.get("kb_orphans_outside_scope", [])
        ),
        "json_path": payload.get("json_path"),
    }
