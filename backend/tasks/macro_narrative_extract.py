"""Daily Macro-Narrative extract task (P2-A, 2026-05-16).

来源: docs/alphagbm_skills_research_2026-05-15.md skill `macro-view`.

Schedule: 10:00 Asia/Shanghai daily (registered in ``backend/celery_app.py``).
Sequence relative to siblings:
  - 08:00  run_alpha_health_check
  - 08:30  run_hypothesis_health_check
  - 09:00  run_pillar_balance_check
  - 09:30  run_negative_knowledge_extract
  - 10:00  this task — runs LAST so KB writes don't compete with the
           earlier health-check / extract jobs

Two-phase work:
  1. Seed UPSERT (always, idempotent, no LLM cost): writes the inline
     seed bank into ``knowledge_entries`` (entry_type='MACRO_NARRATIVE').
  2. LLM batch fill-in (gated by ENABLE_MACRO_NARRATIVE_EXTRACT, default
     OFF — M9): list_fields_missing_narrative → split into batches of
     ``MACRO_NARRATIVE_LLM_BATCH_SIZE`` → per-batch token-budget guard
     via the daily Redis counter ``aiac:macro_extract_tokens:<utc-date>``
     (S5) → LLM call → S7 case-insensitive field_id match →
     upsert_llm_narratives.

Writes a JSON summary to ``docs/macro_narratives/<sh-date>.json``.

Read-mostly: only mutates ``knowledge_entries`` — never touches alphas /
datafields / datasets.
"""
from __future__ import annotations

import json
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from backend.celery_app import celery_app
from backend.tasks import run_async


_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "docs" / "macro_narratives"

# Asia/Shanghai is UTC+8 year-round (no DST). Mirrors the sibling extract
# tasks so the daily filename matches the Celery beat's local-time fire.
SH_TZ = timezone(timedelta(hours=8))

_REPORT_SCHEMA_VERSION = "p2a.v1"


@celery_app.task(name="backend.tasks.run_macro_narrative_extract")
def run_macro_narrative_extract():
    """Sync Celery entrypoint — delegates to the async runner."""
    return run_async(_run_async())


async def _run_async() -> Dict[str, Any]:
    """Phase 1: seed UPSERT; Phase 2: optional LLM batch fill-in."""
    from backend.database import AsyncSessionLocal
    from backend.config import settings
    from backend.macro_narratives import MacroNarrative
    from backend.services.macro_narrative_service import MacroNarrativeService
    from backend.agents.prompts.macro_narrative import (
        MACRO_NARRATIVE_BATCH_SYSTEM,
        build_macro_narrative_batch_user_prompt,
        parse_macro_narrative_batch_response,
    )

    payload: Dict[str, Any] = {}
    try:
        now_utc = datetime.now(timezone.utc)
        sh_now = now_utc.astimezone(SH_TZ)
        utc_date = now_utc.strftime("%Y-%m-%d")

        # ----- Phase 1: seed UPSERT (always runs) -----
        seed_counters: Dict[str, int] = {}
        async with AsyncSessionLocal() as db:
            svc = MacroNarrativeService(db)
            seed_counters = await svc.upsert_seed_narratives()

        # ----- Phase 2: LLM batch (gated by ENABLE_MACRO_NARRATIVE_EXTRACT) -----
        llm_counters: Dict[str, int] = {
            "new": 0, "updated": 0, "skipped": 0, "errors": 0,
            "batches_run": 0, "batches_skipped_budget": 0,
            "llm_field_unmatched": 0, "llm_json_parse_failures": 0,
        }
        fields_processed = 0

        extract_enabled = bool(getattr(
            settings, "ENABLE_MACRO_NARRATIVE_EXTRACT", False,
        ))

        if extract_enabled:
            max_per_day = int(getattr(
                settings, "MACRO_NARRATIVE_LLM_MAX_PER_DAY", 500,
            ))
            batch_size = int(getattr(
                settings, "MACRO_NARRATIVE_LLM_BATCH_SIZE", 20,
            ))
            max_token_budget = int(getattr(
                settings, "MAX_TOKENS_PER_DAY", 500_000,
            ))

            # S5 Redis day-counter
            redis_cli = None
            try:
                from backend.tasks.redis_pool import get_redis_client
                redis_cli = get_redis_client()
            except Exception as ex:
                logger.warning(
                    f"[macro_narrative] redis unavailable for token "
                    f"counter: {ex} — proceeding without budget guard"
                )
                redis_cli = None

            token_key = f"aiac:macro_extract_tokens:{utc_date}"

            async with AsyncSessionLocal() as db:
                svc = MacroNarrativeService(db)
                missing = await svc.list_fields_missing_narrative(
                    region=None, limit=max_per_day,
                )
                fields_processed = len(missing)

                # Initialize LLM service (deferred to avoid top-level import
                # cycle through agents/services/__init__).
                from backend.agents.services.llm_service import LLMService
                llm_service = LLMService()

                # P2 review fix: group `missing` by region BEFORE batching so
                # every batch is region-homogeneous. The list_fields query now
                # ORDER BYs region, so itertools.groupby is sufficient. Without
                # this, a single batch can mix USA / EUR / CHN fields and the
                # LLM gets one region's market context applied to all of them
                # (then ALL get persisted with batch[0]'s region — wrong).
                from itertools import groupby

                def _region_key(item):
                    return (item.get("region") or "").strip() or "USA"

                # Stop the outer loop early if budget exhausted on inner batch.
                _budget_stop = False

                # Per-batch loop, partitioned by region
                for region_key, region_iter in groupby(missing, key=_region_key):
                    if _budget_stop:
                        break
                    region_fields = list(region_iter)
                    for batch_start in range(0, len(region_fields), batch_size):
                        batch = region_fields[batch_start: batch_start + batch_size]
                        if not batch:
                            break

                        # Budget guard (S5)
                        if redis_cli is not None:
                            try:
                                used_raw = redis_cli.get(token_key)
                                used = int(used_raw) if used_raw else 0
                            except Exception:
                                used = 0
                            # Conservative cost estimate: 300 tokens / field
                            est = 300 * len(batch)
                            if used + est > max_token_budget:
                                llm_counters["batches_skipped_budget"] += 1
                                logger.warning(
                                    f"[macro_narrative] token budget exceeded "
                                    f"used={used} + est={est} > "
                                    f"max={max_token_budget} — stop batches"
                                )
                                # Stop both inner AND outer loop — outer
                                # checks _budget_stop at the top.
                                _budget_stop = True
                                break

                        # Build prompt + call LLM. region_key is the groupby
                        # key — guaranteed homogeneous within this batch.
                        region_for_batch = region_key
                        user_prompt = build_macro_narrative_batch_user_prompt(
                            batch, region=region_for_batch,
                        )
                        try:
                            response = await llm_service.call(
                                system_prompt=MACRO_NARRATIVE_BATCH_SYSTEM,
                                user_prompt=user_prompt,
                                temperature=0.4,
                                json_mode=True,
                            )
                        except Exception as ex:
                            logger.warning(
                                f"[macro_narrative] LLM call failed for batch "
                                f"{batch_start} (region={region_for_batch}): {ex}"
                            )
                            llm_counters["errors"] += 1
                            continue

                        parsed = None
                        if getattr(response, "success", False):
                            parsed = getattr(response, "parsed", None)

                        items = []
                        if parsed:
                            if isinstance(parsed, dict):
                                items = parsed.get("items") or []
                            elif isinstance(parsed, str):
                                items = parse_macro_narrative_batch_response(parsed)

                        if not items:
                            llm_counters["llm_json_parse_failures"] += 1
                            continue

                        # S7: case-insensitive exact match field_id
                        by_fid = {
                            str(f.get("field_id") or "").strip().lower(): f
                            for f in batch
                        }
                        new_narratives: List[MacroNarrative] = []
                        for it in items:
                            rid = str(it.get("field_id") or "").strip().lower()
                            if not rid:
                                llm_counters["llm_field_unmatched"] += 1
                                continue
                            match = by_fid.get(rid)
                            if match is None:
                                llm_counters["llm_field_unmatched"] += 1
                                continue
                            new_narratives.append(MacroNarrative(
                                field_id=match.get("field_id"),
                                dataset_id=match.get("dataset_id"),
                                dataset_category=match.get(
                                    "dataset_category_inferred",
                                ),
                                region=region_for_batch,
                                mechanism=(it.get("mechanism") or "")[:500],
                                transmission_channel=(
                                    it.get("transmission_channel") or ""
                                )[:500],
                                expected_signal_hint=(
                                    it.get("expected_signal_hint") or ""
                                ),
                                confidence=float(it.get("confidence", 0.5) or 0.5),
                                source="llm",
                            ))

                        # UPSERT into KB
                        if new_narratives:
                            sub = await svc.upsert_llm_narratives(new_narratives)
                            for k in ("new", "updated", "skipped", "errors"):
                                llm_counters[k] += int(sub.get(k, 0))

                        llm_counters["batches_run"] += 1

                        # Token counter increment (best-effort)
                        if redis_cli is not None:
                            try:
                                # Conservative: 300 tokens per field actually
                                # sent (regardless of LLM-reported usage).
                                redis_cli.incrby(token_key, 300 * len(batch))
                                # 36h TTL — survives midnight rollover so two
                                # back-to-back runs in the same UTC day are
                                # bounded by the same counter.
                                redis_cli.expire(token_key, 36 * 3600)
                            except Exception:
                                pass

        payload = {
            "report_date": sh_now.strftime("%Y-%m-%d"),
            "generated_at_utc": now_utc.isoformat(),
            "schema_version": _REPORT_SCHEMA_VERSION,
            "seed_counters": seed_counters,
            "extract_enabled": extract_enabled,
            "llm_counters": llm_counters,
            "fields_processed": fields_processed,
        }
    except Exception as e:
        logger.error(f"[macro_narrative] run failed: {e}")
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
            f"[macro_narrative] seed_new={seed_counters.get('new', 0)} "
            f"seed_updated={seed_counters.get('updated', 0)} "
            f"extract_enabled={payload.get('extract_enabled')} "
            f"llm_new={llm_counters.get('new', 0)} "
            f"→ {out_path}"
        )
    except Exception as e:
        logger.warning(f"[macro_narrative] write output failed: {e}")
        payload["write_error"] = str(e)

    return {
        "report_date": payload.get("report_date"),
        "seed_counters": payload.get("seed_counters", {}),
        "extract_enabled": payload.get("extract_enabled", False),
        "llm_counters": payload.get("llm_counters", {}),
        "fields_processed": payload.get("fields_processed", 0),
        "json_path": payload.get("json_path"),
    }
