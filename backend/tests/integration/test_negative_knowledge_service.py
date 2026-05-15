"""P2-D NegativeKnowledgeService integration tests (Postgres only).

Mirrors backend/tests/integration/test_pillar_balance_check.py: requires a
live Postgres on localhost:5433 because the service uses JSONB ``?`` / ``->>``
/ ``::int`` cast / pg_insert ON CONFLICT — none of which aiosqlite supports.

Tagged rows are seeded with a unique uuid prefix and cleaned up in the
fixture's finally block.
"""
from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import List

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("POSTGRES_PORT", "5433")


def _pg_reachable() -> bool:
    try:
        s = socket.create_connection(("localhost", 5433), timeout=1)
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason="P2-D negative-knowledge tests require Postgres on localhost:5433",
)


# Warm-up: importing backend.tasks first triggers the full agents/tasks
# graph loading in the right order, avoiding the
# backend.services -> mining_service -> agent_hub -> mining_agent -> tasks
# -> mining_tasks -> from backend.agents import MiningAgent CIRCULAR.
import backend.tasks  # noqa: E402,F401

from backend.models import (  # noqa: E402  — env tweak first
    KnowledgeEntry,
)
from backend.negative_knowledge import (  # noqa: E402
    FailureSignature,
    compute_signature_key,
)
from backend.services.negative_knowledge_service import (  # noqa: E402
    NegativeKnowledgeService,
)


_TAG = f"nkT{uuid.uuid4().hex[:3]}_"


def _now_iso(offset_hours: float = 0.0) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(hours=offset_hours)
    ).isoformat()


def _make_sig(
    *,
    rule_id: str = "RISK_TEST",
    skeleton: str = "ts_rank(...)",
    region: str = "USA",
    category: str = "static_finding",
    # P2 review fix: default ≥ min_failure_count_to_promote (=2) so existing
    # "upsert behavior" tests still go through the promotion path. Tests that
    # specifically exercise the singleton-skip behavior pass failure_count=1.
    failure_count: int = 2,
    last_seen_at: str = None,
) -> FailureSignature:
    """Generate a tagged signature. The signature_key embeds _TAG via the
    rule_id so cleanup can find the rows."""
    tagged_rule = f"{_TAG}{rule_id}"
    when = last_seen_at or _now_iso()
    return FailureSignature(
        signature_key=compute_signature_key(tagged_rule, skeleton, region),
        rule_id=tagged_rule,
        skeleton=skeleton,
        region=region,
        category=category,
        severity="orange",
        expected_signal="expected",
        remediation_hint=f"fix {tagged_rule}",
        failure_count=failure_count,
        top_examples=[{
            "alpha_id": f"{_TAG}{uuid.uuid4().hex[:6]}",
            "expression": "ts_rank(close, 20)",
            "at": when,
        }],
        first_seen_at=when,
        last_seen_at=when,
    )


@pytest_asyncio.fixture
async def pg_session():
    """Live PG session; cleans up tagged rows in a finally block."""
    from backend.config import settings

    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as s:
            yield s
            try:
                # Clean up by rule_id ILIKE _TAG% match in meta_data
                # (ILIKE because some rule_ids are lowercased in the
                # extractor while _TAG keeps mixed case "nkT...").
                await s.execute(
                    text(
                        "DELETE FROM knowledge_entries "
                        "WHERE meta_data->>'rule_id' ILIKE :p"
                    ),
                    {"p": f"{_TAG}%"},
                )
                # Also clean by created_by tag we may have used
                await s.execute(
                    delete(KnowledgeEntry).where(
                        KnowledgeEntry.pattern.like(f"%{_TAG}%"),
                    )
                )
                await s.commit()
            except Exception:
                await s.rollback()
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestUpsertPitfalls:

    @pytest.mark.asyncio
    async def test_upsert_new_pitfall(self, pg_session):
        """S1: One fresh signature → counters['new']=1, row inserted with
        entry_type=FAILURE_PITFALL, fail_count=1."""
        svc = NegativeKnowledgeService(pg_session)
        sig = _make_sig(rule_id="RISK_NEW_1")
        counters = await svc.upsert_pitfalls([sig])
        assert counters["new"] == 1
        assert counters["errors"] == 0

        row = (await pg_session.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.pattern == f"PITFALL::{sig.signature_key}",
            )
        )).scalar_one()
        assert row.entry_type == "FAILURE_PITFALL"
        assert row.is_active is True
        assert (row.meta_data or {}).get("fail_count") == 1

    @pytest.mark.asyncio
    async def test_upsert_existing_increments(self, pg_session):
        """S2: Pre-existing row with fail_count=5 + new sig with
        failure_count=3 → fail_count=8 after upsert."""
        svc = NegativeKnowledgeService(pg_session)
        sig_a = _make_sig(rule_id="RISK_INC", failure_count=5)
        counters_a = await svc.upsert_pitfalls([sig_a])
        assert counters_a["new"] == 1

        sig_b = _make_sig(rule_id="RISK_INC", failure_count=3)
        counters_b = await svc.upsert_pitfalls([sig_b])
        assert counters_b["updated"] == 1
        assert counters_b["new"] == 0

        row = (await pg_session.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.pattern == f"PITFALL::{sig_b.signature_key}",
            )
        )).scalar_one()
        assert (row.meta_data or {}).get("fail_count") == 8

    @pytest.mark.asyncio
    async def test_savepoint_isolates_failures(self, pg_session, monkeypatch):
        """S3: If the upsert of sig #2 raises, sigs #1 and #3 still land.
        counters['errors']=1 — SAVEPOINT isolation works."""
        svc = NegativeKnowledgeService(pg_session)
        sigs = [
            _make_sig(rule_id="RISK_SP_1"),
            _make_sig(rule_id="RISK_SP_2"),
            _make_sig(rule_id="RISK_SP_3"),
        ]
        # Monkeypatch _upsert_one to raise on the middle signature
        original = svc._upsert_one
        call_count = {"n": 0}

        async def _boom(sig, *, is_pg):
            call_count["n"] += 1
            if sig.rule_id.endswith("RISK_SP_2"):
                raise RuntimeError("simulated mid-batch failure")
            return await original(sig, is_pg=is_pg)

        monkeypatch.setattr(svc, "_upsert_one", _boom)
        counters = await svc.upsert_pitfalls(sigs)
        assert counters["errors"] == 1
        assert (counters["new"] + counters["updated"]) >= 2

        # Sigs 1 and 3 must exist
        for sig in (sigs[0], sigs[2]):
            row = (await pg_session.execute(
                select(KnowledgeEntry).where(
                    KnowledgeEntry.pattern == f"PITFALL::{sig.signature_key}",
                )
            )).scalar_one_or_none()
            assert row is not None, (
                f"SAVEPOINT did not isolate — sig {sig.rule_id} lost"
            )

    @pytest.mark.asyncio
    async def test_singleton_signature_not_promoted_by_default(self, pg_session):
        """P2 review fix: default min_failure_count_to_promote=2 keeps
        single-fire signatures OUT of the KB. The take-profit research
        principle is 'repeated failures sediment', not every one-off.
        Counter goes to 'skipped', no row written."""
        svc = NegativeKnowledgeService(pg_session)
        # Explicit failure_count=1 (override _make_sig's bumped default).
        sig = _make_sig(rule_id="RISK_SINGLETON", failure_count=1)
        counters = await svc.upsert_pitfalls([sig])
        assert counters["skipped"] == 1
        assert counters["new"] == 0
        assert counters["updated"] == 0

        row = (await pg_session.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.pattern == f"PITFALL::{sig.signature_key}",
            )
        )).scalar_one_or_none()
        assert row is None, "singleton signature should NOT have been written"

    @pytest.mark.asyncio
    async def test_singleton_promoted_when_explicit_threshold_lower(
        self, pg_session,
    ):
        """Callers can opt back into eager promotion via the kwarg."""
        svc = NegativeKnowledgeService(pg_session)
        sig = _make_sig(rule_id="RISK_SINGLETON_EAGER", failure_count=1)
        counters = await svc.upsert_pitfalls(
            [sig], min_failure_count_to_promote=1,
        )
        assert counters["new"] == 1
        assert counters["skipped"] == 0

    @pytest.mark.asyncio
    async def test_curator_deactivate_preserved_on_update(self, pg_session):
        """P2 review fix: existing.is_active=False set by a curator must
        survive a subsequent upsert. Pre-fix the UPDATE branch did
        ``existing.is_active = True`` unconditionally, silently undoing
        manual ops decisions. Now we only auto-revive rows we authored
        (created_by='P2D_NEGKB')."""
        svc = NegativeKnowledgeService(pg_session)
        sig = _make_sig(rule_id="RISK_CURATOR", failure_count=5)
        # 1) First write — our row, is_active=True.
        await svc.upsert_pitfalls([sig])
        row = (await pg_session.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.pattern == f"PITFALL::{sig.signature_key}",
            )
        )).scalar_one()
        # 2) Simulate curator: flip is_active=False AND change created_by
        # to a non-P2D origin (mimics "ops took ownership of this entry").
        row.is_active = False
        row.created_by = "CURATOR_MANUAL"
        pg_session.add(row)
        await pg_session.commit()
        # 3) Another failure fires the same signature — should NOT revive.
        sig_again = _make_sig(rule_id="RISK_CURATOR", failure_count=3)
        await svc.upsert_pitfalls([sig_again])
        row_after = (await pg_session.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.pattern == f"PITFALL::{sig_again.signature_key}",
            )
        )).scalar_one()
        assert row_after.is_active is False, (
            "curator's deactivation was silently reverted by upsert"
        )
        assert row_after.created_by == "CURATOR_MANUAL"
        # fail_count still updates — counters/last_seen are diagnostic-only.
        assert (row_after.meta_data or {}).get("fail_count") == 8

    @pytest.mark.asyncio
    async def test_our_own_deactivated_row_is_revived(self, pg_session):
        """Symmetric: a row WE wrote and that later went is_active=False
        (e.g. by a cleanup task that prunes stale rows) SHOULD be revived
        when the pattern fires again. created_by='P2D_NEGKB' guard
        permits the auto-revive on our rows only."""
        svc = NegativeKnowledgeService(pg_session)
        sig = _make_sig(rule_id="RISK_OURS_REVIVE", failure_count=5)
        await svc.upsert_pitfalls([sig])
        row = (await pg_session.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.pattern == f"PITFALL::{sig.signature_key}",
            )
        )).scalar_one()
        assert row.created_by == "P2D_NEGKB"
        row.is_active = False
        pg_session.add(row)
        await pg_session.commit()
        sig_again = _make_sig(rule_id="RISK_OURS_REVIVE", failure_count=2)
        await svc.upsert_pitfalls([sig_again])
        row_after = (await pg_session.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.pattern == f"PITFALL::{sig_again.signature_key}",
            )
        )).scalar_one()
        assert row_after.is_active is True, (
            "our own deactivated row must auto-revive on next failure"
        )


class TestFetchTopPitfalls:

    @pytest.mark.asyncio
    async def test_fetch_filters_region_and_min_count(self, pg_session):
        """S4: USA fc=10 + USA fc=1 + GLB fc=20 → fetch(USA, min_fc=3) returns
        only USA fc=10."""
        svc = NegativeKnowledgeService(pg_session)

        # Use very-high fail_counts so our tagged rows come back even with
        # a shared DB that has hundreds of other USA pitfalls.
        sig_usa_10 = _make_sig(
            rule_id="RISK_FILT_USA", region="USA", failure_count=99999,
            skeleton="ts_rank(usa_10)",
        )
        sig_usa_1 = _make_sig(
            rule_id="RISK_FILT_USA_LOW", region="USA", failure_count=1,
            skeleton="ts_rank(usa_1)",
        )
        sig_glb = _make_sig(
            rule_id="RISK_FILT_GLB", region="GLB", failure_count=99998,
            skeleton="ts_rank(glb)",
        )
        await svc.upsert_pitfalls(
            [sig_usa_10, sig_usa_1, sig_glb],
            min_failure_count_to_promote=1,
        )

        results = await svc.fetch_top_pitfalls(
            "USA", limit=50, min_fail_count=3,
        )
        # Filter to our tagged ones (shared DB may have other USA rows)
        ours = [r for r in results if r["rule_id"].startswith(_TAG)]
        rule_ids = {r["rule_id"] for r in ours}
        # USA_10 must be present (fc=99999 will be at the top of any DB)
        assert f"{_TAG}RISK_FILT_USA" in rule_ids, (
            f"USA fc=99999 row missing from top-50 USA results "
            f"(got rule_ids: {rule_ids})"
        )
        # USA_LOW (fc=1) filtered out by min_fc
        assert f"{_TAG}RISK_FILT_USA_LOW" not in rule_ids
        # GLB filtered out by region
        assert f"{_TAG}RISK_FILT_GLB" not in rule_ids

    @pytest.mark.asyncio
    async def test_fetch_excludes_unknown_skeleton_and_old_rows(
        self, pg_session,
    ):
        """S1 (skeleton UNKNOWN exclusion) + S2 (14d recency) + S5
        (sim_error cross-region inclusion via region="")."""
        svc = NegativeKnowledgeService(pg_session)

        # Use very-high fail_counts so our tagged rows reliably appear in
        # top-50 even on a shared DB with many production pitfalls.
        # 1. UNKNOWN skeleton — must NOT come back
        sig_unknown = _make_sig(
            rule_id="RISK_UNKNOWN_SK", region="USA", failure_count=99999,
            skeleton="UNKNOWN",
        )
        # 2. Stale row (last_seen_at 30 days ago) — must NOT come back
        stale_when = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).isoformat()
        sig_stale = _make_sig(
            rule_id="RISK_STALE", region="USA", failure_count=99998,
            skeleton="ts_rank(stale)",
            last_seen_at=stale_when,
        )
        # 3. sim_error with region="" — must come back when querying USA
        sig_sim = _make_sig(
            rule_id="RISK_SIM_GLOBAL", region="", failure_count=99997,
            category="sim_error", skeleton="ts_rank(sim)",
        )
        # 4. Normal fresh USA row — must come back
        sig_fresh = _make_sig(
            rule_id="RISK_FRESH", region="USA", failure_count=99996,
            skeleton="ts_rank(fresh)",
        )
        await svc.upsert_pitfalls(
            [sig_unknown, sig_stale, sig_sim, sig_fresh],
            min_failure_count_to_promote=1,
        )

        results = await svc.fetch_top_pitfalls(
            "USA", limit=50, min_fail_count=3,
        )
        ours = [r for r in results if r["rule_id"].startswith(_TAG)]
        rule_ids = {r["rule_id"] for r in ours}

        # S1: UNKNOWN skeleton excluded
        assert f"{_TAG}RISK_UNKNOWN_SK" not in rule_ids, (
            "UNKNOWN skeleton row must be filtered out"
        )
        # S2: stale (>14d) row excluded
        assert f"{_TAG}RISK_STALE" not in rule_ids, (
            "Stale (>14d last_seen_at) row must be filtered out"
        )
        # S5: sim_error cross-region (region="") included
        assert f"{_TAG}RISK_SIM_GLOBAL" in rule_ids, (
            "sim_error region='' must be returned for any region query"
        )
        # Fresh USA included
        assert f"{_TAG}RISK_FRESH" in rule_ids

        # Ordering: results should be DESC by fail_count
        if len(ours) >= 2:
            fc_seq = [r["fail_count"] for r in ours]
            assert fc_seq == sorted(fc_seq, reverse=True), (
                f"results not sorted by fail_count DESC: {fc_seq}"
            )
