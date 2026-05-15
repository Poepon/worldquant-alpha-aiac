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
    """Compute per-region pillar shares over the trailing 7d and persist
    a JSON report. Returns a small summary dict for Celery result storage."""
    from sqlalchemy import select, func
    from backend.database import AsyncSessionLocal
    from backend.models import Alpha, Hypothesis
    from backend.pillar_classifier import PILLAR_VALUES, infer_pillar
    from backend.config import settings

    payload: Dict[str, Any] = {}
    try:
        now_utc = datetime.now(timezone.utc)
        sh_now = now_utc.astimezone(SH_TZ)
        # ``alphas.created_at`` is TIMESTAMP WITHOUT TIME ZONE — pass a naive
        # UTC datetime so asyncpg doesn't reject the WHERE clause with
        # "can't subtract offset-naive and offset-aware datetimes".
        cutoff = (now_utc - timedelta(days=7)).replace(tzinfo=None)
        target = getattr(settings, "PILLAR_TARGET_DISTRIBUTION", {}) or {}

        async with AsyncSessionLocal() as db:
            # M3: OUTER JOIN from Alpha so legacy rows with NULL hypothesis_id
            # are not silently dropped. The grouping is on Hypothesis.pillar;
            # NULL pillars (legacy + Hypothesis without pillar yet) are then
            # attributed via infer_pillar on the row's expression.
            #
            # Two-step approach:
            #   1. SUM by (region, pillar) for rows that already have a
            #      pillar (fast PG aggregate).
            #   2. Stream rows with NULL pillar through infer_pillar in
            #      Python and bin them into the same region buckets.
            grouped_stmt = (
                select(
                    Alpha.region,
                    Hypothesis.pillar,
                    func.count(Alpha.id),
                )
                .select_from(Alpha)
                .outerjoin(
                    Hypothesis, Alpha.hypothesis_id == Hypothesis.id,
                )
                .where(Alpha.created_at >= cutoff)
                .group_by(Alpha.region, Hypothesis.pillar)
            )
            grouped_rows = (await db.execute(grouped_stmt)).all()

            null_pillar_stmt = (
                select(Alpha.region, Alpha.expression)
                .select_from(Alpha)
                .outerjoin(
                    Hypothesis, Alpha.hypothesis_id == Hypothesis.id,
                )
                .where(
                    Alpha.created_at >= cutoff,
                    Hypothesis.pillar.is_(None),
                )
            )
            null_rows = (await db.execute(null_pillar_stmt)).all()

        # ---- aggregate ----
        # by_region[region][pillar] = count of alphas already stamped
        by_region: Dict[str, Dict[str, int]] = {}
        # inferred[region][pillar] = count of legacy NULL alphas attributed
        # via infer_pillar (kept separate so the report can show coverage).
        inferred: Dict[str, Dict[str, int]] = {}

        for region, pillar, count in grouped_rows:
            region = region or "UNKNOWN"
            bucket = by_region.setdefault(region, {})
            key = pillar if pillar else "unknown"
            bucket[key] = bucket.get(key, 0) + int(count or 0)

        for region, expression in null_rows:
            region = region or "UNKNOWN"
            inferred_pillar = infer_pillar(expression=expression or "")
            ibucket = inferred.setdefault(region, {})
            ibucket[inferred_pillar] = ibucket.get(inferred_pillar, 0) + 1

        # ---- build per-region report ----
        regions: Dict[str, Dict[str, Any]] = {}
        all_regions = set(by_region.keys()) | set(inferred.keys())
        for region in sorted(all_regions):
            stamped = by_region.get(region, {})
            legacy_inferred = inferred.get(region, {})
            # Stamped counts (denominator excludes ``unknown`` so legacy
            # backlog doesn't dilute fresh share computation).
            stamped_total = sum(
                c for p, c in stamped.items() if p in target
            )
            shares = {
                p: (stamped.get(p, 0) / stamped_total) if stamped_total else 0.0
                for p in target
            }
            deficits = {
                p: max(0.0, target.get(p, 0.0) - shares.get(p, 0.0))
                for p in target
            }
            skew = (
                max(shares.values()) - min(shares.values())
                if shares else 0.0
            )
            top_def = (
                max(deficits.items(), key=lambda kv: kv[1])
                if deficits else (None, 0.0)
            )
            regions[region] = {
                "stamped_counts": stamped,
                "stamped_total": stamped_total,
                "unknown_count": stamped.get("unknown", 0),
                "legacy_inferred_counts": legacy_inferred,
                "legacy_inferred_total": sum(legacy_inferred.values()),
                "shares": {k: round(v, 3) for k, v in shares.items()},
                "target": target,
                "deficits": {k: round(v, 3) for k, v in deficits.items()},
                "skew": round(skew, 3),
                "next_pillar": (
                    top_def[0] if (top_def[1] or 0) > 0 else None
                ),
            }

        payload = {
            "report_date": sh_now.strftime("%Y-%m-%d"),
            "generated_at_utc": now_utc.isoformat(),
            "lookback_days": 7,
            "pillar_values": sorted(PILLAR_VALUES),
            "regions": regions,
            "totals": {
                "regions_checked": len(regions),
                "stamped_alphas": sum(
                    r["stamped_total"] for r in regions.values()
                ),
                "legacy_inferred_alphas": sum(
                    r["legacy_inferred_total"] for r in regions.values()
                ),
            },
        }
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
