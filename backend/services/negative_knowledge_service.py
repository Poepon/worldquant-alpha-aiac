"""P2-D Negative Knowledge service (2026-05-15).

来源: docs/alphagbm_skills_research_2026-05-15.md skills `take-profit`/
`health-check`.

DB-bound service that turns recent failure signals into ``FailureSignature``
records and UPSERTs them into ``knowledge_entries`` (entry_type=
'FAILURE_PITFALL') so the LLM can be nudged away from repeating known
pitfalls.

Three public methods:
  - collect_recent_failures(window_hours) → List[FailureSignature]
  - upsert_pitfalls(signatures)            → Dict counters (new/updated/...)
  - fetch_top_pitfalls(region, ...)        → List[Dict] for PromptContext

PG-only by design — the JSON queries use JSONB operators (``?``, ``->>``,
``::int`` cast) that aiosqlite cannot evaluate. Tests must skipif when
Postgres is not reachable (see backend/tests/integration/
test_negative_knowledge_service.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import select, text, insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    Alpha,
    AlphaFailure,
    Hypothesis,
    HypothesisRoundStats,
    KnowledgeEntry,
    MiningTask,
    compute_pattern_hash,
)
from backend.negative_knowledge import (
    FailureSignature,
    _merge_examples,
    _pattern_text_for,
    aggregate_signatures,
    extract_failures_from_alpha,
    extract_failures_from_alpha_failure,
    extract_failures_from_hypothesis_round,
)
from backend.services.base import BaseService


# How big to keep meta_data["top_examples"]. Same constant in both upsert
# and aggregate paths to guarantee S6 reservoir invariant.
_TOP_EXAMPLES_KEEP = 5


class NegativeKnowledgeService(BaseService):
    """Sediment + retrieve negative-knowledge patterns."""

    # ------------------------------------------------------------------
    # collect_recent_failures
    # ------------------------------------------------------------------
    async def collect_recent_failures(
        self,
        window_hours: int = 24,
    ) -> List[FailureSignature]:
        """Walk the last ``window_hours`` of failure-bearing rows across 3
        sources and return the raw signature events (NOT yet aggregated)."""
        now_utc = datetime.now(timezone.utc)
        delta = timedelta(hours=int(window_hours))

        # S4: two cutoffs — naive for ``alphas.created_at`` (TIMESTAMP WITHOUT
        # TIME ZONE), aware for ``alpha_failures.created_at`` +
        # ``hypothesis_round_stats.created_at`` (both TIMESTAMP WITH TIME ZONE).
        cutoff_naive = (now_utc - delta).replace(tzinfo=None)
        cutoff_aware = now_utc - delta

        signatures: List[FailureSignature] = []

        # ---- 1) Alpha rows with metric-bearing failure signals ----
        try:
            # JSONB ? predicates — PG only. The OR chain lets one query catch
            # any of four metric flavors. Outerjoin Hypothesis to access
            # ``trigger_detail`` for hyp-trigger extraction.
            stmt = (
                select(Alpha, Hypothesis)
                .select_from(Alpha)
                .outerjoin(Hypothesis, Alpha.hypothesis_id == Hypothesis.id)
                .where(Alpha.created_at >= cutoff_naive)
                .where(
                    text(
                        "(alphas.metrics ? '_validation_findings' "
                        "OR alphas.metrics ? '_robustness_failed' "
                        "OR alphas.metrics ? 'failed_tests' "
                        "OR alphas.metrics ? '_failed_tests')"
                    )
                )
            )
            rows = (await self.db.execute(stmt)).all()
            for row in rows:
                try:
                    alpha = row[0]
                    hyp = row[1] if len(row) > 1 else None
                    sigs = extract_failures_from_alpha(
                        alpha, hypothesis=hyp, now_utc=now_utc,
                    )
                    signatures.extend(sigs)
                except Exception as ex:
                    logger.warning(
                        f"[negative_knowledge] alpha row extract failed: {ex}"
                    )
        except Exception as ex:
            logger.warning(
                f"[negative_knowledge] alpha query failed: {ex}"
            )

        # ---- 2) AlphaFailure rows ----
        try:
            # S5: AlphaFailure has no direct alpha_id link to Alpha — the FK
            # set in models/alpha.py is task_id/trace_step_id/run_id/
            # hypothesis_id. Resolve region by outerjoining MiningTask via
            # task_id (most reliable — task always has a region). Region
            # stamps onto a transient attr (_resolved_region) so the pure
            # extractor can read it without re-running the join.
            stmt2 = (
                select(AlphaFailure, MiningTask.region)
                .select_from(AlphaFailure)
                .outerjoin(
                    MiningTask, AlphaFailure.task_id == MiningTask.id,
                )
                .where(AlphaFailure.created_at >= cutoff_aware)
            )
            rows2 = (await self.db.execute(stmt2)).all()
            for row in rows2:
                try:
                    failure = row[0]
                    region = (row[1] if len(row) > 1 else "") or ""
                    setattr(failure, "_resolved_region", region)
                    sigs = extract_failures_from_alpha_failure(
                        failure, now_utc=now_utc,
                    )
                    signatures.extend(sigs)
                except Exception as ex:
                    logger.warning(
                        f"[negative_knowledge] failure row extract failed: {ex}"
                    )
        except Exception as ex:
            logger.warning(
                f"[negative_knowledge] alpha_failure query failed: {ex}"
            )

        # ---- 3) HypothesisRoundStats with attribution='hypothesis' ----
        try:
            stmt3 = (
                select(HypothesisRoundStats, Hypothesis)
                .select_from(HypothesisRoundStats)
                .join(
                    Hypothesis,
                    HypothesisRoundStats.hypothesis_id == Hypothesis.id,
                )
                .where(HypothesisRoundStats.created_at >= cutoff_aware)
                .where(HypothesisRoundStats.attribution == "hypothesis")
            )
            rows3 = (await self.db.execute(stmt3)).all()
            for row in rows3:
                try:
                    rs = row[0]
                    hyp = row[1] if len(row) > 1 else None
                    sigs = extract_failures_from_hypothesis_round(
                        rs, hyp, now_utc=now_utc,
                    )
                    signatures.extend(sigs)
                except Exception as ex:
                    logger.warning(
                        f"[negative_knowledge] hrs row extract failed: {ex}"
                    )
        except Exception as ex:
            logger.warning(
                f"[negative_knowledge] hrs query failed: {ex}"
            )

        return signatures

    # ------------------------------------------------------------------
    # upsert_pitfalls
    # ------------------------------------------------------------------
    async def upsert_pitfalls(
        self,
        signatures: List[FailureSignature],
        *,
        min_failure_count_to_promote: int = 2,
    ) -> Dict[str, int]:
        """Aggregate then UPSERT into knowledge_entries.

        Each signature gets its own SAVEPOINT so a single bad row (UNIQUE
        violation race, JSON shape mismatch, etc.) does not lose the whole
        batch. Counters returned: ``new`` / ``updated`` / ``skipped`` /
        ``errors``.

        ``min_failure_count_to_promote`` default is ``2`` (P2 review fix):
        the take-profit research principle is "**repeated** failures
        sediment" — single-fire signatures are mostly noise. Promoting
        them at count=1 caused KB to fill linearly with one-off events
        that the retrieve path (``fetch_top_pitfalls`` min_fail_count=3)
        never surfaces anyway. Callers wanting eager promotion (e.g.
        scripts re-running over a known-clean window) can override.
        """
        counters: Dict[str, int] = {
            "new": 0, "updated": 0, "skipped": 0, "errors": 0,
        }
        if not signatures:
            return counters

        agg = aggregate_signatures(signatures)
        is_pg = self._is_postgres()

        for key, sig in agg.items():
            if sig.failure_count < int(min_failure_count_to_promote):
                counters["skipped"] += 1
                continue
            try:
                async with self.db.begin_nested():
                    promoted = await self._upsert_one(sig, is_pg=is_pg)
                    if promoted == "new":
                        counters["new"] += 1
                    elif promoted == "updated":
                        counters["updated"] += 1
                    else:
                        counters["skipped"] += 1
            except Exception as ex:
                logger.warning(
                    f"[negative_knowledge] upsert sig={key} failed: {ex}"
                )
                counters["errors"] += 1
        try:
            await self.db.commit()
        except Exception as ex:
            logger.warning(
                f"[negative_knowledge] commit failed: {ex}"
            )
        return counters

    async def _upsert_one(
        self, sig: FailureSignature, *, is_pg: bool,
    ) -> str:
        """Inner UPSERT for one signature. Returns 'new' / 'updated'."""
        pattern_text = _pattern_text_for(sig)
        pattern_hash = compute_pattern_hash(pattern_text, sig.region, None)

        # FIND
        stmt = select(KnowledgeEntry).where(
            KnowledgeEntry.pattern_hash == pattern_hash,
        )
        existing = (await self.db.execute(stmt)).scalar_one_or_none()

        if existing is not None:
            # P2 review fix: do NOT clobber a curator's manual is_active=False.
            # Only auto-revive rows we authored ourselves ("P2D_NEGKB" created_by);
            # human-disabled / other-source rows keep their explicit state and
            # only get their fail_count / last_seen_at updated.
            if existing.created_by == "P2D_NEGKB" and not existing.is_active:
                existing.is_active = True
            existing.description = (sig.remediation_hint or "")[:240]
            existing.entry_type = "FAILURE_PITFALL"
            meta = dict(existing.meta_data or {})
            meta["fail_count"] = int(
                meta.get("fail_count", 0) or 0,
            ) + int(sig.failure_count)
            meta["skeleton"] = sig.skeleton
            meta["rule_id"] = sig.rule_id
            meta["category"] = sig.category
            meta["region"] = sig.region
            meta["severity"] = sig.severity
            meta["expected_signal"] = sig.expected_signal
            meta["signature_key"] = sig.signature_key
            meta["last_seen_at"] = sig.last_seen_at or meta.get("last_seen_at")
            # first_seen_at is keep-the-oldest (MIN)
            if not meta.get("first_seen_at"):
                meta["first_seen_at"] = sig.first_seen_at
            meta["top_examples"] = _merge_examples(
                meta.get("top_examples") or [],
                sig.top_examples or [],
                keep=_TOP_EXAMPLES_KEEP,
            )
            existing.meta_data = meta
            existing.usage_count = int(existing.usage_count or 0) + 1
            self.db.add(existing)
            return "updated"

        # INSERT
        meta = {
            "fail_count": int(sig.failure_count),
            "skeleton": sig.skeleton,
            "rule_id": sig.rule_id,
            "category": sig.category,
            "region": sig.region,
            "severity": sig.severity,
            "expected_signal": sig.expected_signal,
            "signature_key": sig.signature_key,
            "first_seen_at": sig.first_seen_at,
            "last_seen_at": sig.last_seen_at,
            "top_examples": list(sig.top_examples or [])[:_TOP_EXAMPLES_KEEP],
        }
        values = {
            "entry_type": "FAILURE_PITFALL",
            "pattern": pattern_text,
            "pattern_hash": pattern_hash,
            "description": (sig.remediation_hint or "")[:240],
            "meta_data": meta,
            "usage_count": 0,
            "is_active": True,
            "created_by": "P2D_NEGKB",
        }
        if is_pg:
            # ON CONFLICT DO NOTHING on the UNIQUE pattern_hash index — under
            # rare concurrent extract runs the second writer falls through to
            # treat the row as 'updated' on the next pass.
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            stmt2 = (
                pg_insert(KnowledgeEntry)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["pattern_hash"])
            )
            res = await self.db.execute(stmt2)
            if (res.rowcount or 0) == 0:
                # Conflict — race; fall back to UPDATE path next time
                return "updated"
            return "new"
        # SQLite fallback — no-op for production; only here so a test on
        # aiosqlite (if force-enabled) won't crash at import.
        stmt2 = insert(KnowledgeEntry).values(**values)
        await self.db.execute(stmt2)
        return "new"

    # ------------------------------------------------------------------
    # fetch_top_pitfalls
    # ------------------------------------------------------------------
    async def fetch_top_pitfalls(
        self,
        region: str,
        *,
        limit: int = 5,
        min_fail_count: int = 3,
        category_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return top pitfalls for the LLM nudge. The returned dicts are
        ``build_patterns_context``-friendly: ``pattern`` (skeleton),
        ``description`` (remediation hint), plus a few diagnostic fields
        the prompt template can ignore.

        WHERE clauses encode S1 (no UNKNOWN skeleton), S2 (14d recency),
        S5 (sim_error cross-region) — see commentary inline.
        """
        if not region:
            return []
        sql = """
            SELECT
              id,
              pattern,
              description,
              meta_data
            FROM knowledge_entries
            WHERE entry_type = 'FAILURE_PITFALL'
              AND is_active = TRUE
              AND COALESCE(meta_data->>'skeleton', 'UNKNOWN') != 'UNKNOWN'
              AND (meta_data->>'last_seen_at')::timestamptz
                  >= NOW() - INTERVAL '14 days'
              AND COALESCE((meta_data->>'fail_count')::int, 0) >= :min_fc
              AND (
                  meta_data->>'region' = :region
                  OR (meta_data->>'region' = '' AND
                      meta_data->>'category' = 'sim_error')
              )
        """
        params: Dict[str, Any] = {
            "region": region,
            "min_fc": int(min_fail_count),
        }
        if category_filter:
            sql += " AND meta_data->>'category' = :cat"
            params["cat"] = str(category_filter)
        sql += (
            " ORDER BY COALESCE((meta_data->>'fail_count')::int, 0) DESC,"
            " (meta_data->>'last_seen_at') DESC NULLS LAST"
            " LIMIT :lim"
        )
        params["lim"] = int(limit)

        try:
            rows = (await self.db.execute(text(sql), params)).all()
        except Exception as ex:
            logger.warning(
                f"[negative_knowledge] fetch_top_pitfalls failed: {ex}"
            )
            return []

        out: List[Dict[str, Any]] = []
        for r in rows:
            md = r[3] if len(r) > 3 else {}
            md = md if isinstance(md, dict) else {}
            out.append({
                "pattern": md.get("skeleton") or "",
                "description": (r[2] or "")[:200],
                "rule_id": md.get("rule_id") or "",
                "category": md.get("category") or "",
                "fail_count": int(md.get("fail_count", 0) or 0),
                "signature_key": md.get("signature_key") or "",
            })
        return out

    # ------------------------------------------------------------------
    # Ops dashboard read methods (P3, 2026-05-16)
    #
    # These complement fetch_top_pitfalls (mining-time, region-bound) with
    # ops-view aggregations that don't require a region. They run the
    # same JSONB query but with relaxed WHERE clauses.
    # ------------------------------------------------------------------

    async def fetch_top_pitfalls_admin(
        self,
        *,
        region: Optional[str] = None,
        limit: int = 20,
        min_fail_count: int = 1,
        category_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Admin-side variant of fetch_top_pitfalls — region optional.

        When region is None we return active pitfalls across all regions
        (the /ops/negative-knowledge page wants the global picture).
        Otherwise behaviour mirrors fetch_top_pitfalls (sim_error rows
        with empty region are always included).
        """
        sql = """
            SELECT
              id, pattern, description, meta_data, is_active,
              created_at, updated_at
            FROM knowledge_entries
            WHERE entry_type = 'FAILURE_PITFALL'
              AND is_active = TRUE
              AND COALESCE(meta_data->>'skeleton', 'UNKNOWN') != 'UNKNOWN'
              AND COALESCE((meta_data->>'fail_count')::int, 0) >= :min_fc
        """
        params: Dict[str, Any] = {"min_fc": int(min_fail_count)}
        if region:
            sql += (
                " AND (meta_data->>'region' = :region"
                "      OR (meta_data->>'region' = '' AND"
                "          meta_data->>'category' = 'sim_error'))"
            )
            params["region"] = region
        if category_filter:
            sql += " AND meta_data->>'category' = :cat"
            params["cat"] = str(category_filter)
        sql += (
            " ORDER BY COALESCE((meta_data->>'fail_count')::int, 0) DESC,"
            " (meta_data->>'last_seen_at') DESC NULLS LAST"
            " LIMIT :lim"
        )
        params["lim"] = int(limit)

        try:
            rows = (await self.db.execute(text(sql), params)).all()
        except Exception as ex:
            logger.warning(
                f"[negative_knowledge] fetch_top_pitfalls_admin failed: {ex}"
            )
            return []

        out: List[Dict[str, Any]] = []
        for r in rows:
            md = r[3] if r[3] else {}
            md = md if isinstance(md, dict) else {}
            out.append({
                "id": r[0],
                "pattern": md.get("skeleton") or "",
                "description": (r[2] or "")[:500],
                "rule_id": md.get("rule_id") or "",
                "category": md.get("category") or "",
                "region": md.get("region") or "",
                "fail_count": int(md.get("fail_count", 0) or 0),
                "signature_key": md.get("signature_key") or "",
                "first_seen_at": md.get("first_seen_at"),
                "last_seen_at": md.get("last_seen_at"),
                "is_active": bool(r[4]),
            })
        return out

    async def aggregate_by_category(
        self, *, region: Optional[str] = None,
    ) -> Dict[str, int]:
        """Count active pitfalls grouped by 6-class category.

        Returns ``{category: count}`` with 0 entries omitted. Used by
        /ops/negative-knowledge/category-breakdown to render a PieChart.
        """
        sql = """
            SELECT
              COALESCE(meta_data->>'category', 'unknown') AS cat,
              COUNT(*) AS n
            FROM knowledge_entries
            WHERE entry_type = 'FAILURE_PITFALL'
              AND is_active = TRUE
        """
        params: Dict[str, Any] = {}
        if region:
            sql += " AND meta_data->>'region' = :region"
            params["region"] = region
        sql += " GROUP BY cat"
        try:
            rows = (await self.db.execute(text(sql), params)).all()
        except Exception as ex:
            logger.warning(
                f"[negative_knowledge] aggregate_by_category failed: {ex}"
            )
            return {}
        return {r[0] or "unknown": int(r[1] or 0) for r in rows}

    async def get_pitfall_timeline(
        self, *, days: int = 30, region: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Per-day count of new pitfalls (created_at) over ``days``.

        Each entry: ``{"date": "2026-05-16", "new_count": N}``. Missing
        days are NOT zero-filled — frontend chart handles gaps.
        """
        sql = """
            SELECT
              DATE(created_at) AS d,
              COUNT(*) AS n
            FROM knowledge_entries
            WHERE entry_type = 'FAILURE_PITFALL'
              AND created_at >= NOW() - (:days || ' days')::interval
        """
        params: Dict[str, Any] = {"days": int(max(1, min(days, 365)))}
        if region:
            sql += " AND meta_data->>'region' = :region"
            params["region"] = region
        sql += " GROUP BY d ORDER BY d"
        try:
            rows = (await self.db.execute(text(sql), params)).all()
        except Exception as ex:
            logger.warning(
                f"[negative_knowledge] get_pitfall_timeline failed: {ex}"
            )
            return []
        return [
            {"date": r[0].isoformat() if r[0] else None, "new_count": int(r[1])}
            for r in rows if r[0]
        ]

    async def set_pitfall_active(self, entry_id: int, is_active: bool) -> bool:
        """Soft-enable / disable a single pitfall row. Returns True if
        anything was updated (one row, by ID), False otherwise.

        Backs the /ops/negative-knowledge "禁用 Switch" interaction.
        Keeps the audit trail in sync — knowledge_entries.updated_at
        bumps automatically via the ORM.
        """
        try:
            from sqlalchemy import update
            from backend.models import KnowledgeEntry
            stmt = (
                update(KnowledgeEntry)
                .where(
                    KnowledgeEntry.id == int(entry_id),
                    KnowledgeEntry.entry_type == "FAILURE_PITFALL",
                )
                .values(is_active=bool(is_active))
            )
            result = await self.db.execute(stmt)
            await self.db.commit()
            return result.rowcount > 0
        except Exception as ex:
            await self.db.rollback()
            logger.warning(
                f"[negative_knowledge] set_pitfall_active({entry_id}) failed: {ex}"
            )
            return False

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _is_postgres(self) -> bool:
        bind = getattr(self.db, "bind", None)
        if bind is None:
            try:
                bind = self.db.get_bind()
            except Exception:
                return False
        try:
            return bool(bind and bind.dialect.name == "postgresql")
        except Exception:
            return False


__all__ = ["NegativeKnowledgeService"]
