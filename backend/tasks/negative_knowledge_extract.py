"""Daily Negative Knowledge extract task (P2-D, 2026-05-15).

来源: docs/alphagbm_skills_research_2026-05-15.md skills `take-profit`/
`health-check`.

Schedule: 09:30 Asia/Shanghai daily (registered in ``backend/celery_app.py``).
Sequence relative to siblings:
  - 08:00  run_alpha_health_check
  - 08:30  run_hypothesis_health_check
  - 09:00  run_pillar_balance_check
  - 09:30  this task — runs AFTER pillar balance so attribution writes
           from the earlier health checks have settled.

Aggregates 24h of failure signals from Alpha.metrics / AlphaFailure /
HypothesisRoundStats into ``FailureSignature`` rows, then UPSERTs them
into ``knowledge_entries`` (entry_type='FAILURE_PITFALL') and writes a
JSON summary to ``docs/negative_knowledge/<sh-date>.json``.

Read-mostly: only mutates ``knowledge_entries`` (UPSERT) — never touches
``alphas`` / ``alpha_failures`` / ``hypotheses``.
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


_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "docs" / "negative_knowledge"

# Asia/Shanghai is UTC+8 year-round (no DST). Mirrors pillar_balance_check
# SH_TZ so the daily filename matches the Celery beat's local-time fire.
SH_TZ = timezone(timedelta(hours=8))

_REPORT_SCHEMA_VERSION = "p2d.v1"
_TOP_PATTERNS_LIMIT = 20


@celery_app.task(name="backend.tasks.run_negative_knowledge_extract")
def run_negative_knowledge_extract():
    """Sync Celery entrypoint — delegates to the async runner."""
    return run_async(_run_async())


async def _run_async() -> Dict[str, Any]:
    """Collect 24h of failure signals → aggregate → UPSERT → write JSON."""
    from backend.database import AsyncSessionLocal
    from backend.services.negative_knowledge_service import (
        NegativeKnowledgeService,
    )
    from backend.config import settings
    from backend.negative_knowledge import aggregate_signatures

    payload: Dict[str, Any] = {}
    try:
        now_utc = datetime.now(timezone.utc)
        sh_now = now_utc.astimezone(SH_TZ)
        window_hours = int(getattr(
            settings,
            "NEGATIVE_KNOWLEDGE_RETROSPECTIVE_WINDOW_HOURS",
            24,
        ))

        async with AsyncSessionLocal() as db:
            svc = NegativeKnowledgeService(db)
            raw_sigs = await svc.collect_recent_failures(
                window_hours=window_hours,
            )
            agg = aggregate_signatures(raw_sigs)

            # By-category breakdown for the report
            by_category: Dict[str, int] = {}
            for sig in agg.values():
                by_category[sig.category] = by_category.get(
                    sig.category, 0,
                ) + sig.failure_count

            # Top-N patterns by fail_count for the report
            sorted_sigs = sorted(
                agg.values(),
                key=lambda s: (s.failure_count, s.last_seen_at or ""),
                reverse=True,
            )
            top_patterns: List[Dict[str, Any]] = [{
                "signature_key": s.signature_key,
                "rule_id": s.rule_id,
                "skeleton": s.skeleton,
                "region": s.region,
                "category": s.category,
                "severity": s.severity,
                "fail_count": s.failure_count,
                "first_seen_at": s.first_seen_at,
                "last_seen_at": s.last_seen_at,
                "remediation_hint": s.remediation_hint,
            } for s in sorted_sigs[:_TOP_PATTERNS_LIMIT]]

            # UPSERT — counters come back for the report. min_failure_count
            # _to_promote=1 so we keep everything; the LLM nudge side uses
            # NEGATIVE_KNOWLEDGE_MIN_FAIL_COUNT to filter at read time.
            counters = await svc.upsert_pitfalls(
                list(agg.values()),
                min_failure_count_to_promote=1,
            )

        payload = {
            "report_date": sh_now.strftime("%Y-%m-%d"),
            "generated_at_utc": now_utc.isoformat(),
            "window_hours": window_hours,
            "raw_signature_events": len(raw_sigs),
            "unique_signatures": len(agg),
            "by_category": by_category,
            "top_patterns": top_patterns,
            "upsert_counters": counters,
            "new_pitfalls": int(counters.get("new", 0)),
            "promoted_pitfalls": int(counters.get("new", 0))
                                + int(counters.get("updated", 0)),
            "schema_version": _REPORT_SCHEMA_VERSION,
        }
    except Exception as e:
        logger.error(f"[negative_knowledge] run failed: {e}")
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
            f"[negative_knowledge] events={payload['raw_signature_events']} "
            f"unique={payload['unique_signatures']} "
            f"new={payload['new_pitfalls']} "
            f"promoted={payload['promoted_pitfalls']} → {out_path}"
        )
    except Exception as e:
        logger.warning(f"[negative_knowledge] write output failed: {e}")
        payload["write_error"] = str(e)

    return {
        "report_date": payload.get("report_date"),
        "raw_signature_events": payload.get("raw_signature_events", 0),
        "unique_signatures": payload.get("unique_signatures", 0),
        "new_pitfalls": payload.get("new_pitfalls", 0),
        "promoted_pitfalls": payload.get("promoted_pitfalls", 0),
        "upsert_counters": payload.get("upsert_counters", {}),
        "json_path": payload.get("json_path"),
    }
