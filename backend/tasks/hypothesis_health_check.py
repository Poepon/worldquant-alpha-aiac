"""Daily hypothesis-health-check task (P1-C, second half).

来源: docs/alphagbm_skills_research_2026-05-15.md skill `investment-thesis`.

Schedule: 08:30 Asia/Shanghai daily (registered in
``backend/celery_app.py``). The cron is ``crontab(hour=8, minute=30)`` —
Celery 4.x+ interprets cron strings under ``celery_app.conf.timezone``
("Asia/Shanghai", set in celery_app.py L25), so this fires at 08:30 SH.

Why 08:30 not 08:00:
  - 08:00  run_alpha_health_check (alpha-library audit)
  - 08:30  this task — runs AFTER the alpha-side check so any
           sync-induced metric refreshes have settled. Windows Celery
           worker is ``--pool=solo`` (serial) so a 30-min gap covers
           even slow alpha-audit runs.

This task writes to the hypothesis row (is_triggered, trigger_detail,
thesis_score, ai_feedback, history) and the new audit table
``hypothesis_status_transitions`` — those are the ONLY mutations.
Output is one JSON file
``docs/hypothesis_health_check/<asia-shanghai-date>.json`` per run;
same-day re-runs overwrite. See plan
``C:\\Users\\Administrator\\.claude\\plans\\enumerated-enchanting-knuth.md``.
"""
from __future__ import annotations

import json
import traceback
from pathlib import Path

from loguru import logger

from backend.celery_app import celery_app
from backend.tasks import run_async


_OUTPUT_DIR = (
    Path(__file__).resolve().parents[2] / "docs" / "hypothesis_health_check"
)


@celery_app.task(name="backend.tasks.run_hypothesis_health_check")
def run_hypothesis_health_check():
    """Sync Celery entrypoint — delegates to the async runner."""
    return run_async(_run_async())


async def _run_async():
    """Run hypothesis health audit + persist JSON output.

    BaselineProvider construction order is load-bearing (mirrors
    ``backend/tasks/alpha_health_check.py``): build the
    ``category_resolver`` first, then pass it via the constructor
    ``BaselineProvider(category_resolver=resolver)``. Patching the
    provider's private ``_category_resolver`` attribute *after*
    construction does NOT work — the constructor already defaulted it
    to a no-op lambda, so a later ``getattr(..., None) is None`` check
    fires False and the resolver is silently ignored.

    P1-C part 1 carried this lesson as MF1/MF2 — we repeat the
    constructor-injected pattern here verbatim.
    """
    from backend.agents.services.baseline_provider import BaselineProvider
    from backend.agents.services.llm_service import LLMService
    from backend.database import AsyncSessionLocal
    from backend.services.alpha_health_service import AlphaHealthService
    from backend.services.hypothesis_health_service import (
        HypothesisHealthService,
    )

    payload = None
    try:
        async with AsyncSessionLocal() as db:
            resolver = await AlphaHealthService.build_category_resolver(db)
            bp = BaselineProvider(category_resolver=resolver)
            # LLM is optional — if init fails (no key etc.) we still run the
            # trigger evaluation, just without scoring. The service handles
            # ``llm_service=None`` gracefully via ``_can_call_llm``.
            try:
                llm = LLMService()
            except Exception as e:
                logger.warning(
                    f"[hyp_health] LLMService init failed (no LLM scoring this "
                    f"run): {type(e).__name__}: {e}"
                )
                llm = None
            svc = HypothesisHealthService(
                db, baseline_provider=bp, llm_service=llm,
            )
            payload = await svc.run_full_check()
    except Exception as e:
        logger.error(f"[hyp_health] run failed: {e}")
        return {"error": str(e), "traceback": traceback.format_exc()}

    # Persist to docs/hypothesis_health_check/<sh-date>.json. Filename uses
    # the service-generated ``payload['report_date']`` (already Asia/Shanghai
    # local-date string) so tests only need to inject ``now_utc`` — no
    # freezegun dependency required.
    try:
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _OUTPUT_DIR / f"{payload['report_date']}.json"
        # newline="\n": force LF on Windows so git-diff is cross-platform.
        out_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
            newline="\n",
        )
        payload["json_path"] = str(out_path)
        logger.info(
            f"[hyp_health] checked={payload['totals']['checked']} "
            f"triggered={payload['totals']['triggered_count']} "
            f"tokens={payload.get('llm_token_used', 0)} → {out_path}"
        )
    except Exception as e:
        logger.warning(f"[hyp_health] write output failed: {e}")
        payload["write_error"] = str(e)

    return {
        "checked": payload["totals"]["checked"],
        "triggered_count": payload["totals"]["triggered_count"],
        "llm_token_used": payload.get("llm_token_used", 0),
        "json_path": payload.get("json_path"),
    }
