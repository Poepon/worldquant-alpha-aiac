"""P2-A MacroNarrativeService (2026-05-16).

来源: docs/alphagbm_skills_research_2026-05-15.md skill `macro-view`.

DB-bound service that:
  * upserts the inline seed bank into ``knowledge_entries`` (entry_type=
    'MACRO_NARRATIVE') — idempotent via pattern_hash UNIQUE
  * upserts LLM-batch generated narratives (source='llm')
  * lists DataFields lacking a narrative (for the daily extract task)
  * fetches up to 5 (field|dataset|category)-scope narratives for prompt
    injection in node_hypothesis

Each upsert path uses ``async with self.db.begin_nested()`` SAVEPOINT-per-
row (S6) so a single bad row doesn't lose the batch. The fetch path is PG-
only because the JSONB queries (``meta_data->>'field_id'``, ANY operator)
can't be evaluated by aiosqlite.

Circular-import note (M10): ``infer_dataset_category`` lives in
``backend.agents.services.rag_service`` and is lazy-imported inside the
single method that needs it; importing it at module top would create a
``services → agents → services`` cycle.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from backend.macro_narratives import (
    MacroNarrative,
    get_all_seeds,
    narrative_to_kb_payload,
)
from backend.models import DataField, DatasetMetadata, KnowledgeEntry
from backend.services.base import BaseService


class MacroNarrativeService(BaseService):
    """Seed + retrieve macro narratives for the P2-A RAG nudge."""

    # ------------------------------------------------------------------
    # upsert_seed_narratives — idempotent inline seed bank UPSERT
    # ------------------------------------------------------------------
    async def upsert_seed_narratives(self) -> Dict[str, int]:
        """Walk the inline seed bank and UPSERT each row.

        Uses SAVEPOINT-per-row (S6) so a single IntegrityError on the
        UNIQUE pattern_hash index doesn't lose the rest of the batch.

        Returns counters dict with keys: ``new``, ``updated``, ``skipped``,
        ``errors``.
        """
        counters: Dict[str, int] = {
            "new": 0, "updated": 0, "skipped": 0, "errors": 0,
        }
        for seed in get_all_seeds():
            try:
                async with self.db.begin_nested():
                    op = await self._upsert_one(seed)
                    if op == "new":
                        counters["new"] += 1
                    elif op == "updated":
                        counters["updated"] += 1
                    else:
                        counters["skipped"] += 1
            except IntegrityError as iex:
                # UNIQUE race or constraint surprise — log + swallow.
                logger.warning(
                    f"[macro_narrative] seed upsert IntegrityError "
                    f"field={seed.field_id} cat={seed.dataset_category}: {iex}"
                )
                counters["errors"] += 1
            except Exception as ex:
                logger.warning(
                    f"[macro_narrative] seed upsert failed "
                    f"field={seed.field_id} cat={seed.dataset_category}: {ex}"
                )
                counters["errors"] += 1
        try:
            await self.db.commit()
        except Exception as ex:
            logger.warning(f"[macro_narrative] seed commit failed: {ex}")
        return counters

    # ------------------------------------------------------------------
    # upsert_llm_narratives — LLM-batch path (source='llm', allow updates)
    # ------------------------------------------------------------------
    async def upsert_llm_narratives(
        self, narratives: List[MacroNarrative],
    ) -> Dict[str, int]:
        """LLM-generated narrative UPSERT. Same SAVEPOINT-per-row pattern
        as ``upsert_seed_narratives``. LLM rows can update in-place (the
        ``_upsert_one`` UPDATE branch refreshes mechanism / confidence)."""
        counters: Dict[str, int] = {
            "new": 0, "updated": 0, "skipped": 0, "errors": 0,
        }
        for n in narratives:
            try:
                async with self.db.begin_nested():
                    op = await self._upsert_one(n)
                    if op == "new":
                        counters["new"] += 1
                    elif op == "updated":
                        counters["updated"] += 1
                    else:
                        counters["skipped"] += 1
            except IntegrityError as iex:
                logger.warning(
                    f"[macro_narrative] llm upsert IntegrityError "
                    f"field={n.field_id} cat={n.dataset_category}: {iex}"
                )
                counters["errors"] += 1
            except Exception as ex:
                logger.warning(
                    f"[macro_narrative] llm upsert failed "
                    f"field={n.field_id} cat={n.dataset_category}: {ex}"
                )
                counters["errors"] += 1
        try:
            await self.db.commit()
        except Exception as ex:
            logger.warning(f"[macro_narrative] llm commit failed: {ex}")
        return counters

    async def _upsert_one(self, n: MacroNarrative) -> str:
        """Inner UPSERT for one MacroNarrative. Returns 'new' / 'updated'.

        FIND by pattern_hash (UNIQUE index). If existing → UPDATE
        description / meta_data; if absent → INSERT new row.
        """
        payload = narrative_to_kb_payload(n)
        ph = payload["pattern_hash"]

        existing = (await self.db.execute(
            select(KnowledgeEntry).where(KnowledgeEntry.pattern_hash == ph)
        )).scalar_one_or_none()

        if existing is not None:
            existing.entry_type = payload["entry_type"]
            existing.description = payload["description"]
            existing.is_active = True
            # meta_data refresh — keep S6 prior fields if newer source has
            # nothing better, but the seed/llm story is "newer narrative
            # wins" so we overwrite. Note: pattern_hash includes source
            # so seed and llm rows are separate keys; an LLM upsert never
            # clobbers a seed row (different hash).
            # Reassignment (not in-place mutation) — SQLAlchemy attribute
            # setter detects the change automatically; flag_modified() not
            # needed (it's only required when mutating the existing dict).
            existing.meta_data = payload["meta_data"]
            self.db.add(existing)
            return "updated"

        new_entry = KnowledgeEntry(
            entry_type=payload["entry_type"],
            pattern=payload["pattern"],
            pattern_hash=payload["pattern_hash"],
            description=payload["description"],
            meta_data=payload["meta_data"],
            is_active=True,
            created_by=payload["created_by"],
            usage_count=0,
        )
        self.db.add(new_entry)
        return "new"

    # ------------------------------------------------------------------
    # list_fields_missing_narrative (M1/M2 — JOIN DataField → DatasetMetadata)
    # ------------------------------------------------------------------
    async def list_fields_missing_narrative(
        self, *, region: Optional[str] = None, limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Return DataField rows that have NO field-scope MACRO_NARRATIVE.

        M1/M2 critical correction: ``DataField.dataset_id`` is an Integer
        FK to ``datasets.id``. The BRAIN string dataset_id we use for the
        category inference lives in ``DatasetMetadata.dataset_id``
        (String(100)). The query JOINs through so we return the BRAIN
        string id, which is what ``_infer_category`` expects.

        Output row dict: ``field_id``, ``field_name``, ``description``,
        ``dataset_id`` (BRAIN string), ``dataset_category_inferred``,
        ``region``, ``category_name``.
        """
        # LEFT JOIN against KnowledgeEntry on JSONB meta_data->>'field_id'
        # = DataField.field_id, only the field-scope MACRO_NARRATIVE rows
        # (so missing here means we have NO field-level narrative).
        sql = """
            SELECT
              df.field_id,
              df.field_name,
              df.description,
              df.category_name,
              df.region,
              dm.dataset_id AS dataset_id_brain
            FROM datafields df
            JOIN datasets dm ON df.dataset_id = dm.id
            LEFT JOIN knowledge_entries ke
              ON ke.entry_type = 'MACRO_NARRATIVE'
              AND ke.is_active = TRUE
              AND COALESCE(ke.meta_data->>'scope', 'unknown') = 'field'
              AND COALESCE(ke.meta_data->>'field_id', '') = df.field_id
            WHERE df.is_active = TRUE
              AND ke.id IS NULL
        """
        params: Dict[str, Any] = {}
        if region:
            sql += " AND df.region = :region"
            params["region"] = region
        # P2 review fix: ORDER BY region FIRST so the task can groupby
        # and produce region-homogeneous batches. Without this the LLM
        # gets one region's market context but writes narratives for
        # cross-region fields, all stamped with batch[0]'s region.
        sql += " ORDER BY df.region, df.field_id LIMIT :lim"
        params["lim"] = int(limit)

        try:
            rows = (await self.db.execute(text(sql), params)).all()
        except Exception as ex:
            logger.warning(
                f"[macro_narrative] list_fields_missing_narrative failed: {ex}"
            )
            return []

        out: List[Dict[str, Any]] = []
        for r in rows:
            dataset_id_brain = (r[5] or "") if len(r) > 5 else ""
            category = self._infer_category(dataset_id_brain)
            out.append({
                "field_id": r[0] or "",
                "field_name": r[1] or "",
                "description": (r[2] or ""),
                "category_name": r[3] or "",
                "region": r[4] or "",
                "dataset_id": dataset_id_brain,
                "dataset_category_inferred": category,
            })
        return out

    # ------------------------------------------------------------------
    # fetch_macro_narratives (S4 — Python-side sort, field +0.1 bonus)
    # ------------------------------------------------------------------
    async def fetch_macro_narratives(
        self,
        *,
        dataset_id: Optional[str],
        region: Optional[str],
        key_fields: Optional[List[str]] = None,
        limit_field: int = 3,
        limit_dataset: int = 1,
        limit_category: int = 1,
    ) -> List[Dict[str, Any]]:
        """Return up to 5 narratives ordered by global confidence DESC,
        with field-scope rows receiving a +0.1 bonus (S4).

        Three independent queries (field / dataset / category) are run +
        union'd in Python so a low-confidence field row doesn't push a
        high-confidence dataset/category narrative out of contention.

        Returns dicts ready for ``build_macro_context_block`` consumption.
        """
        rows_all: List[Dict[str, Any]] = []

        # ---- field-scope ----
        if key_fields:
            try:
                sql_field = """
                    SELECT id, pattern, description, meta_data
                    FROM knowledge_entries
                    WHERE entry_type = 'MACRO_NARRATIVE'
                      AND is_active = TRUE
                      AND COALESCE(meta_data->>'scope', '') = 'field'
                      AND COALESCE(meta_data->>'field_id', '') = ANY(:keys)
                      AND COALESCE(meta_data->>'region', '*') IN (:reg, '*')
                    ORDER BY COALESCE((meta_data->>'confidence')::float, 0.0) DESC NULLS LAST
                    LIMIT :lim
                """
                params_field = {
                    "keys": [str(k) for k in (key_fields or [])],
                    "reg": region or "*",
                    "lim": int(max(limit_field * 2, 5)),
                }
                rows = (await self.db.execute(
                    text(sql_field), params_field,
                )).all()
                for r in rows:
                    md = r[3] if len(r) > 3 else {}
                    md = md if isinstance(md, dict) else {}
                    rec = dict(md)
                    base_conf = float(rec.get("confidence", 0.5) or 0.5)
                    rec["_priority_conf"] = base_conf + 0.1  # S4 field bonus
                    rec.setdefault("scope", "field")
                    rows_all.append(rec)
            except Exception as ex:
                logger.warning(
                    f"[macro_narrative] field-scope fetch failed: {ex}"
                )

        # ---- dataset-scope ----
        if dataset_id:
            try:
                sql_ds = """
                    SELECT id, pattern, description, meta_data
                    FROM knowledge_entries
                    WHERE entry_type = 'MACRO_NARRATIVE'
                      AND is_active = TRUE
                      AND COALESCE(meta_data->>'scope', '') = 'dataset'
                      AND COALESCE(meta_data->>'dataset_id', '') = :ds
                      AND COALESCE(meta_data->>'region', '*') IN (:reg, '*')
                    ORDER BY COALESCE((meta_data->>'confidence')::float, 0.0) DESC NULLS LAST
                    LIMIT :lim
                """
                params_ds = {
                    "ds": str(dataset_id),
                    "reg": region or "*",
                    "lim": int(max(limit_dataset * 2, 3)),
                }
                rows = (await self.db.execute(
                    text(sql_ds), params_ds,
                )).all()
                for r in rows:
                    md = r[3] if len(r) > 3 else {}
                    md = md if isinstance(md, dict) else {}
                    rec = dict(md)
                    base_conf = float(rec.get("confidence", 0.5) or 0.5)
                    rec["_priority_conf"] = base_conf
                    rec.setdefault("scope", "dataset")
                    rows_all.append(rec)
            except Exception as ex:
                logger.warning(
                    f"[macro_narrative] dataset-scope fetch failed: {ex}"
                )

        # ---- category-scope ----
        category = self._infer_category(dataset_id) if dataset_id else None
        if category:
            try:
                sql_cat = """
                    SELECT id, pattern, description, meta_data
                    FROM knowledge_entries
                    WHERE entry_type = 'MACRO_NARRATIVE'
                      AND is_active = TRUE
                      AND COALESCE(meta_data->>'scope', '') = 'category'
                      AND COALESCE(meta_data->>'dataset_category', '') = :cat
                      AND COALESCE(meta_data->>'region', '*') IN (:reg, '*')
                    ORDER BY COALESCE((meta_data->>'confidence')::float, 0.0) DESC NULLS LAST
                    LIMIT :lim
                """
                params_cat = {
                    "cat": str(category),
                    "reg": region or "*",
                    "lim": int(max(limit_category * 2, 3)),
                }
                rows = (await self.db.execute(
                    text(sql_cat), params_cat,
                )).all()
                for r in rows:
                    md = r[3] if len(r) > 3 else {}
                    md = md if isinstance(md, dict) else {}
                    rec = dict(md)
                    base_conf = float(rec.get("confidence", 0.5) or 0.5)
                    rec["_priority_conf"] = base_conf
                    rec.setdefault("scope", "category")
                    rows_all.append(rec)
            except Exception as ex:
                logger.warning(
                    f"[macro_narrative] category-scope fetch failed: {ex}"
                )

        if not rows_all:
            return []

        # Sort: global confidence DESC (with the +0.1 field bonus baked in)
        rows_all.sort(
            key=lambda r: -float(r.get("_priority_conf", r.get("confidence", 0.5)))
        )
        return rows_all[:5]

    # ------------------------------------------------------------------
    # Ops dashboard read methods (P3, 2026-05-16)
    # ------------------------------------------------------------------

    async def coverage_stats(self) -> Dict[str, Any]:
        """Return narrative counts grouped by scope + coverage ratio.

        ``coverage`` = field-scope narratives / total active datafields,
        so ops can see how much of the catalog has economic context.
        Output shape::

            {
              "by_scope": {"field": N, "dataset": N, "category": N},
              "total": N,
              "fields_total": M,
              "fields_with_narrative": K,
              "fields_coverage_pct": 100*K/M,
            }
        """
        scope_sql = """
            SELECT
              COALESCE(meta_data->>'scope', 'unknown') AS scope,
              COUNT(*) AS n
            FROM knowledge_entries
            WHERE entry_type = 'MACRO_NARRATIVE'
              AND is_active = TRUE
            GROUP BY scope
        """
        try:
            scope_rows = (await self.db.execute(text(scope_sql))).all()
        except Exception as ex:
            logger.warning(f"[macro_narrative] coverage_stats — scope query failed: {ex}")
            scope_rows = []
        by_scope = {r[0] or "unknown": int(r[1] or 0) for r in scope_rows}
        total = sum(by_scope.values())

        # field coverage — datafields with at least one field-scope narrative
        coverage_sql = """
            SELECT
              COUNT(DISTINCT df.field_id) AS fields_total,
              COUNT(DISTINCT
                CASE
                  WHEN ke.id IS NOT NULL THEN df.field_id
                END
              ) AS fields_with_narrative
            FROM datafields df
            LEFT JOIN knowledge_entries ke
              ON ke.entry_type = 'MACRO_NARRATIVE'
              AND ke.is_active = TRUE
              AND COALESCE(ke.meta_data->>'scope', 'unknown') = 'field'
              AND COALESCE(ke.meta_data->>'field_id', '') = df.field_id
            WHERE df.is_active = TRUE
        """
        try:
            cov_row = (await self.db.execute(text(coverage_sql))).first()
        except Exception as ex:
            logger.warning(
                f"[macro_narrative] coverage_stats — coverage query failed: {ex}"
            )
            cov_row = None

        fields_total = int(cov_row[0] or 0) if cov_row else 0
        fields_with = int(cov_row[1] or 0) if cov_row else 0
        coverage_pct = (
            round(100.0 * fields_with / fields_total, 1) if fields_total else 0.0
        )
        return {
            "by_scope": by_scope,
            "total": total,
            "fields_total": fields_total,
            "fields_with_narrative": fields_with,
            "fields_coverage_pct": coverage_pct,
        }

    async def list_narratives_by_scope(
        self,
        scope: str,
        *,
        dataset_category: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """List active narrative entries filtered by scope (field/dataset/category).

        Returns dicts ready for the /ops/macro-narratives Tab Table.
        """
        if scope not in ("field", "dataset", "category"):
            return []
        sql = """
            SELECT
              id, pattern, description, meta_data, created_at, updated_at
            FROM knowledge_entries
            WHERE entry_type = 'MACRO_NARRATIVE'
              AND is_active = TRUE
              AND COALESCE(meta_data->>'scope', '') = :scope
        """
        params: Dict[str, Any] = {"scope": scope}
        if dataset_category:
            sql += " AND COALESCE(meta_data->>'dataset_category', '') = :cat"
            params["cat"] = dataset_category
        sql += (
            " ORDER BY COALESCE((meta_data->>'confidence')::float, 0) DESC,"
            " updated_at DESC LIMIT :lim"
        )
        params["lim"] = int(max(1, min(limit, 1000)))
        try:
            rows = (await self.db.execute(text(sql), params)).all()
        except Exception as ex:
            logger.warning(
                f"[macro_narrative] list_narratives_by_scope failed: {ex}"
            )
            return []
        out: List[Dict[str, Any]] = []
        for r in rows:
            md = r[3] if r[3] else {}
            md = md if isinstance(md, dict) else {}
            out.append({
                "id": r[0],
                "pattern": r[1] or "",
                "description": r[2] or "",
                "scope": md.get("scope") or "",
                "field_id": md.get("field_id") or "",
                "dataset_id": md.get("dataset_id") or "",
                "dataset_category": md.get("dataset_category") or "",
                "region": md.get("region") or "",
                "mechanism": md.get("mechanism") or "",
                "confidence": float(md.get("confidence", 0.0) or 0.0),
                "source": md.get("source") or "",
                "created_at": r[4].isoformat() if r[4] else None,
                "updated_at": r[5].isoformat() if r[5] else None,
            })
        return out

    @staticmethod
    def get_token_usage(utc_date: Optional[str] = None) -> Dict[str, Any]:
        """Read Redis macro-extract daily token counter.

        Used by /ops/macro/token-budget to show how much of today's LLM
        budget the macro-narrative extractor has consumed. Fail-open
        with zeros on Redis outage (consistent with other ops endpoints).
        """
        from datetime import datetime as _dt, timezone as _tz
        if utc_date is None:
            utc_date = _dt.now(_tz.utc).date().isoformat()
        out = {"utc_date": utc_date, "tokens_used": 0, "redis_ok": False}
        try:
            from backend.tasks.redis_pool import get_redis_client  # lazy
            cli = get_redis_client()
            raw = cli.get(f"aiac:macro_extract_tokens:{utc_date}")
            out["tokens_used"] = int(raw) if raw else 0
            out["redis_ok"] = True
        except Exception as ex:
            logger.debug(f"[macro_narrative] token usage — redis blip: {ex}")
        return out

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    @staticmethod
    def _infer_category(dataset_id: Optional[str]) -> str:
        """Lazy-import wrapper around
        ``backend.agents.services.rag_service.infer_dataset_category``.

        M10: importing it at module top would form a
        ``services → agents → services`` cycle through rag_service's
        local imports.
        """
        if not dataset_id:
            return "other"
        from backend.agents.services.rag_service import (  # lazy (M10)
            infer_dataset_category,
        )
        return infer_dataset_category(dataset_id)


__all__ = ["MacroNarrativeService"]
