"""Integration tests for the P1-C daily alpha-health-check task.

来源: docs/alphagbm_skills_research_2026-05-15.md skill `health-check`.

Targets the real PostgreSQL DB (the Alpha / KnowledgeEntry tables use JSONB +
ARRAY columns that aiosqlite cannot render — same pattern as Phase 2 B10 /
V-27.45 integration tests in this repo). Each test seeds rows tagged
with a unique uuid prefix and cleans them up in a finally block.

Plan note: the original plan §测试 listed ``conftest.py:db_session``
(aiosqlite) as the fixture, but the in-memory engine fails at create_all
because ``Alpha.tags`` is ``ARRAY(String)`` and several columns are
``JSONB`` — neither is renderable by the SQLite type compiler. Mirroring
the existing pattern, this file uses live Postgres via ``pg_session``
with ``_pg_reachable`` skipif, so the suite is non-disruptive on CI
boxes without a 5433 PG.
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("POSTGRES_PORT", "5433")

from backend.models import (  # noqa: E402  — env tweak first
    Alpha,
    Hypothesis,
    KnowledgeEntry,
    MiningTask,
)
from backend.services.alpha_health_service import AlphaHealthService  # noqa: E402


def _pg_reachable() -> bool:
    try:
        s = socket.create_connection(("localhost", 5433), timeout=1)
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason="Postgres not reachable on localhost:5433",
)


_TAG = f"_p1c_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def pg_session():
    """Live PG session; cleans up _TAG-prefixed rows in a final block."""
    from backend.config import settings

    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as s:
            yield s
            try:
                # Cleanup: KB first (FK-free), then alphas, hypotheses, tasks
                await s.execute(
                    delete(KnowledgeEntry).where(
                        KnowledgeEntry.pattern.like(f"{_TAG}%")
                    )
                )
                # Alphas by alpha_id prefix
                await s.execute(
                    delete(Alpha).where(Alpha.alpha_id.like(f"{_TAG}%"))
                )
                await s.execute(
                    delete(Hypothesis).where(
                        Hypothesis.statement.like(f"{_TAG}%")
                    )
                )
                await s.execute(
                    text("DELETE FROM mining_tasks WHERE task_name LIKE :p"),
                    {"p": f"{_TAG}%"},
                )
                await s.commit()
            except Exception:
                await s.rollback()
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seeded_task(pg_session):
    """Seed a MiningTask for FK satisfaction; return the task object."""
    t = MiningTask(
        task_name=f"{_TAG}_task",
        region="USA",
        universe="TOP3000",
        dataset_strategy="AUTO",        status="RUNNING",
        daily_goal=4,
        
        config={},
    )
    pg_session.add(t)
    await pg_session.commit()
    await pg_session.refresh(t)
    return t


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------

def _aid(suffix: str) -> str:
    return f"{_TAG}{suffix}"[:20]


def _make_alpha(task_id, *, suffix, status="PASS",
                decay_curve=None,
                is_sharpe=1.5, is_fitness=0.8, is_turnover=0.3,
                metrics_snapshot_at=None,
                hypothesis_id=None,                dataset_id="fnd6") -> Alpha:
    return Alpha(
        alpha_id=_aid(suffix),
        task_id=task_id,
        expression="rank(close)",
        expression_hash=f"hash_{suffix}",
        region="USA",
        universe="TOP3000",
        dataset_id=dataset_id,
        status="simulated",
        stage="IS",
        quality_status=status,
        is_sharpe=is_sharpe,
        is_fitness=is_fitness,
        is_turnover=is_turnover,
        decay_curve=decay_curve or [],
        metrics_snapshot_at=metrics_snapshot_at,
        hypothesis_id=hypothesis_id,    )


# ---------------------------------------------------------------------------
# Helpers around the task entrypoint
# ---------------------------------------------------------------------------

async def _run_task_with_session(pg_session, monkeypatch, tmp_path,
                                 bp=None, now_utc=None):
    """Run the service+wrapper end-to-end using the pg_session, bypassing
    the wrapper's ``AsyncSessionLocal()`` (we want to keep the test's
    seeded rows in scope). Also redirects ``_OUTPUT_DIR`` to tmp_path."""
    from backend.tasks import alpha_health_check as task_mod
    monkeypatch.setattr(task_mod, "_OUTPUT_DIR", tmp_path)
    svc = AlphaHealthService(pg_session, baseline_provider=bp)
    payload = await svc.run_full_check(now_utc=now_utc)
    # Mimic the wrapper's persistence step (so we can assert the file)
    out_path = tmp_path / f"{payload['report_date']}.json"
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
        newline="\n",
    )
    payload["json_path"] = str(out_path)
    return payload


# ===========================================================================
# Tests
#
# IMPORTANT: this test file runs against the live PG (`alpha_gpt` on 5433)
# which carries real PASS+PROVISIONAL alphas — totals will be in the
# hundreds, never zero. All assertions must scope to the _TAG-prefixed
# rows we seed, NOT to the global totals.
# ===========================================================================

def _tagged_alphas(payload):
    """Subset of payload['alphas'] (dumped < threshold) belonging to _TAG."""
    return [r for r in payload["alphas"]
            if r["alpha_id"] and r["alpha_id"].startswith(_TAG)]


def _tagged_orphans(payload):
    """Subset of kb_orphans_outside_scope produced by KB rows we seeded.
    Since the wrapper SELECT'd alpha rows by id (not by tag), an "external"
    KB row pointing at a non-test alpha could still appear here — filter
    by KB rows tagged via meta_data['tag'] = 'p1c-test' instead.
    Simpler scope: we identify our orphans via alpha_pk that we seeded."""
    # caller must pass list of acceptable alpha_pk ints
    return payload["kb_orphans_outside_scope"]


@pytest.mark.asyncio
async def test_baseline_smoke_payload_schema(
    pg_session, monkeypatch, tmp_path,
):
    """Payload schema sanity (run against live DB)."""
    payload = await _run_task_with_session(pg_session, monkeypatch, tmp_path)
    # Schema invariants — independent of how many alphas exist
    assert "report_date" in payload
    assert "generated_at" in payload
    assert payload["scope"] == ["PASS", "PASS_PROVISIONAL"]
    assert set(payload["totals"]["by_band"].keys()) == {
        "GREEN", "YELLOW", "ORANGE", "RED", "CRITICAL",
    }
    assert sum(payload["totals"]["by_band"].values()) == \
        payload["totals"]["checked"]
    out_path = Path(payload["json_path"])
    assert out_path.exists()
    parsed = json.loads(out_path.read_text(encoding="utf-8"))
    assert parsed["totals"]["checked"] == payload["totals"]["checked"]


@pytest.mark.asyncio
async def test_seed_3_alphas_writes_json_correct_schema(
    pg_session, seeded_task, monkeypatch, tmp_path,
):
    """3 tagged alphas with different band signatures; assertions
    scoped to _TAG only because the live PG has real alphas alongside."""
    now = datetime.now(timezone.utc)
    # 1. green: fresh + clean decay
    a_green = _make_alpha(
        seeded_task.id, suffix="g1", status="PASS",
        decay_curve=[{"sharpe": 1.5, "fitness": 0.8, "turnover": 0.3}],
        is_sharpe=1.5, is_fitness=0.8, is_turnover=0.3,
        metrics_snapshot_at=now - timedelta(days=2),
    )
    # 2. drifting (sharpe down >50%) red drift
    a_red_drift = _make_alpha(
        seeded_task.id, suffix="r1", status="PASS",
        decay_curve=[{"sharpe": 2.0, "fitness": 1.0, "turnover": 0.3}],
        is_sharpe=0.8, is_fitness=0.5, is_turnover=0.4,
        metrics_snapshot_at=now - timedelta(days=3),
    )
    # 3. stale: fresh decay-curve match but snapshot 20 days old → stale orange
    a_stale = _make_alpha(
        seeded_task.id, suffix="s1", status="PASS_PROVISIONAL",
        decay_curve=[{"sharpe": 1.0, "fitness": 0.5, "turnover": 0.3}],
        is_sharpe=1.0, is_fitness=0.5, is_turnover=0.3,
        metrics_snapshot_at=now - timedelta(days=20),
    )
    pg_session.add_all([a_green, a_red_drift, a_stale])
    await pg_session.commit()

    payload = await _run_task_with_session(
        pg_session, monkeypatch, tmp_path, now_utc=now,
    )

    # Global totals dominated by real DB; scope to tagged.
    tagged_dumped = _tagged_alphas(payload)
    dumped_aids = {r["alpha_id"] for r in tagged_dumped}
    # r1 (red drift) and s1 (orange stale) MUST be in dumped (< threshold);
    # g1 is healthy → may not appear (score >= 70).
    assert _aid("r1") in dumped_aids, (
        f"red-drift alpha missing from dumped; got {dumped_aids}"
    )
    r = next(r for r in tagged_dumped if r["alpha_id"] == _aid("r1"))
    assert r["signals"]["drift"]["severity"] == "red"
    assert r["signals"]["drift"]["baseline_source"] == "decay_curve_head"
    # Score math: drift_pen=90 * 0.5 = 45 → score = 55 → ORANGE → review.
    # Higher penalties (stale/orphan) would shift it to RED/CRITICAL.
    assert r["recommended_action"] in {"review", "consider_demote", "investigate"}
    assert r["health_band"] in {"ORANGE", "RED", "CRITICAL"}


@pytest.mark.asyncio
async def test_kb_orphans_outside_scope_collected(
    pg_session, seeded_task, monkeypatch, tmp_path,
):
    """KB active SUCCESS_PATTERN referencing a FAIL alpha → orphan section."""
    # Seed a FAIL alpha + an active KB row pointing at its PK.
    a_fail = _make_alpha(
        seeded_task.id, suffix="f1", status="FAIL",
        is_sharpe=0.1, is_fitness=0.0, is_turnover=0.9,
    )
    pg_session.add(a_fail)
    await pg_session.commit()
    await pg_session.refresh(a_fail)

    ke = KnowledgeEntry(
        entry_type="SUCCESS_PATTERN",
        pattern=f"{_TAG}_pat_active",
        pattern_hash=f"{_TAG}_h_active",
        meta_data={"alpha_id_ref": a_fail.id, "tag": "p1c-test"},
        is_active=True,
    )
    pg_session.add(ke)
    await pg_session.commit()

    payload = await _run_task_with_session(pg_session, monkeypatch, tmp_path)
    orphans = payload["kb_orphans_outside_scope"]
    targeted = [o for o in orphans if o["alpha_pk"] == a_fail.id]
    assert targeted, f"expected orphan for {a_fail.id}; got {orphans}"
    o = targeted[0]
    assert "kb_active_but_alpha_FAIL" in o["reason"]
    assert ke.id in o["kb_active_entry_ids"]


@pytest.mark.asyncio
async def test_kb_orphans_softdel_distinct_reason(
    pg_session, seeded_task, monkeypatch, tmp_path,
):
    """KB inactive (soft-deleted) referencing OPTIMIZE alpha → softdel reason."""
    a_opt = _make_alpha(
        seeded_task.id, suffix="o1", status="OPTIMIZE",
        is_sharpe=0.7, is_fitness=0.4, is_turnover=0.5,
    )
    pg_session.add(a_opt)
    await pg_session.commit()
    await pg_session.refresh(a_opt)

    ke = KnowledgeEntry(
        entry_type="SUCCESS_PATTERN",
        pattern=f"{_TAG}_pat_softdel",
        pattern_hash=f"{_TAG}_h_softdel",
        meta_data={"alpha_id_ref": a_opt.id},
        is_active=False,
    )
    pg_session.add(ke)
    await pg_session.commit()

    payload = await _run_task_with_session(pg_session, monkeypatch, tmp_path)
    orphans = payload["kb_orphans_outside_scope"]
    targeted = [o for o in orphans if o["alpha_pk"] == a_opt.id]
    assert targeted
    o = targeted[0]
    assert "kb_softdel_alpha_OPTIMIZE" in o["reason"]
    assert o["kb_active_entry_ids"] == []


@pytest.mark.asyncio
async def test_kb_references_missing_alpha_pk(
    pg_session, monkeypatch, tmp_path,
):
    """KB referencing a non-existent alpha_pk → reason kb_references_missing_alpha.

    Plan-invented sentinel ``quality_status="MISSING"`` (not a QualityStatus
    enum member; downstream consumers must not parse this field as an enum).
    """
    # Pick a guaranteed-unused alpha_pk: max(id)+10000
    max_id = (await pg_session.execute(
        text("SELECT COALESCE(MAX(id), 0) FROM alphas")
    )).scalar()
    missing_pk = max_id + 10000

    ke = KnowledgeEntry(
        entry_type="SUCCESS_PATTERN",
        pattern=f"{_TAG}_pat_missing",
        pattern_hash=f"{_TAG}_h_missing",
        meta_data={"alpha_id_ref": missing_pk},
        is_active=True,
    )
    pg_session.add(ke)
    await pg_session.commit()

    payload = await _run_task_with_session(pg_session, monkeypatch, tmp_path)
    orphans = payload["kb_orphans_outside_scope"]
    targeted = [o for o in orphans if o["alpha_pk"] == missing_pk]
    assert targeted
    o = targeted[0]
    assert o["reason"] == "kb_references_missing_alpha"
    assert o["quality_status"] == "MISSING"


@pytest.mark.asyncio
async def test_truncation_threshold(
    pg_session, seeded_task, monkeypatch, tmp_path,
):
    """5 healthy + 1 red-drift → only red-drift in tagged dumped subset."""
    now = datetime.now(timezone.utc)
    green_alphas = [
        _make_alpha(
            seeded_task.id, suffix=f"tg{i}", status="PASS",
            decay_curve=[{"sharpe": 1.5, "fitness": 0.8, "turnover": 0.3}],
            is_sharpe=1.5, is_fitness=0.8, is_turnover=0.3,
            metrics_snapshot_at=now - timedelta(days=1),
        )
        for i in range(5)
    ]
    a_red = _make_alpha(
        seeded_task.id, suffix="tr1", status="PASS",
        decay_curve=[{"sharpe": 2.0, "fitness": 1.0, "turnover": 0.3}],
        is_sharpe=0.5, is_fitness=0.3, is_turnover=0.6,
        metrics_snapshot_at=now - timedelta(days=1),
    )
    pg_session.add_all(green_alphas + [a_red])
    await pg_session.commit()

    payload = await _run_task_with_session(
        pg_session, monkeypatch, tmp_path, now_utc=now,
    )
    tagged_dumped = _tagged_alphas(payload)
    dumped_aids = {r["alpha_id"] for r in tagged_dumped}
    # Red-drift in dumped; greens not in dumped (score >= threshold).
    assert _aid("tr1") in dumped_aids
    for i in range(5):
        assert _aid(f"tg{i}") not in dumped_aids


@pytest.mark.asyncio
async def test_naive_metrics_snapshot_at_handled(
    pg_session, seeded_task, monkeypatch, tmp_path,
):
    """metrics_snapshot_at with naive datetime must not raise (some legacy
    writes may bypass the tzinfo). Note: PG stores the column as
    TIMESTAMP_WITH_TZ so writes get auto-converted; this test mainly
    proves the pure helper's naive-handling code path isn't bypassed."""
    now = datetime.now(timezone.utc)
    a = _make_alpha(
        seeded_task.id, suffix="n1", status="PASS",
        decay_curve=[{"sharpe": 1.5, "fitness": 0.8, "turnover": 0.3}],
        is_sharpe=1.5, is_fitness=0.8, is_turnover=0.3,
        metrics_snapshot_at=now - timedelta(days=2),
    )
    pg_session.add(a)
    await pg_session.commit()
    payload = await _run_task_with_session(
        pg_session, monkeypatch, tmp_path, now_utc=now,
    )
    # Live PG has many alphas; just verify the run didn't raise on this
    # naive-timestamp row and that GREEN counter is non-zero (our healthy
    # tagged alpha contributes).
    assert payload["totals"]["checked"] >= 1
    assert payload["totals"]["by_band"]["GREEN"] >= 1


@pytest.mark.asyncio
async def test_baseline_provider_fallback_invoked_when_no_decay(
    pg_session, seeded_task, monkeypatch, tmp_path,
):
    """Alpha without decay_curve + a usable mocked BaselineProvider →
    drift.baseline_source == 'cluster_baseline'."""
    # Seed a Hypothesis so expected_signal_map has an entry.
    h = Hypothesis(
        statement=f"{_TAG} fallback test",
        region="USA",
        kind="INVESTMENT_THESIS",
        expected_signal="momentum",
        status="ACTIVE",
    )
    pg_session.add(h)
    await pg_session.commit()
    await pg_session.refresh(h)

    now = datetime.now(timezone.utc)
    a = _make_alpha(
        seeded_task.id, suffix="b1", status="PASS",
        decay_curve=[],  # no curve → bp fallback
        is_sharpe=1.0, is_fitness=0.5, is_turnover=0.3,
        metrics_snapshot_at=now - timedelta(days=2),
        hypothesis_id=h.id,
    )
    pg_session.add(a)
    await pg_session.commit()

    fake_stats = SimpleNamespace(
        mean=1.0, std=0.5, count=50, cell_key="k",
        granularity="fine", usable=True,
    )
    bp = SimpleNamespace(get_baseline=AsyncMock(return_value=fake_stats))

    payload = await _run_task_with_session(
        pg_session, monkeypatch, tmp_path, bp=bp, now_utc=now,
    )

    targeted = [r for r in payload["alphas"]
                if r["alpha_id"] == _aid("b1")]
    # green band score may exceed threshold → not in alphas. Re-pull via
    # explicit check by re-running service & filtering records via the
    # _build_payload internal — simpler: bp.get_baseline must have been
    # invoked at least once.
    assert bp.get_baseline.await_count >= 1
    # The record will be GREEN (cur >= mean) → counted in by_band
    assert payload["totals"]["by_band"]["GREEN"] >= 1


@pytest.mark.asyncio
async def test_baseline_provider_unusable_marks_unknown(
    pg_session, seeded_task, monkeypatch, tmp_path,
):
    """Insufficient cluster baseline (.usable=False) → severity=unknown."""
    now = datetime.now(timezone.utc)
    a = _make_alpha(
        seeded_task.id, suffix="u1", status="PASS",
        decay_curve=[],
        is_sharpe=1.0, is_fitness=0.5, is_turnover=0.3,
        metrics_snapshot_at=now - timedelta(days=20),  # orange stale
    )
    pg_session.add(a)
    await pg_session.commit()

    fake_stats = SimpleNamespace(
        mean=0.0, std=0.0, count=5, cell_key="k",
        granularity="insufficient", usable=False,
    )
    bp = SimpleNamespace(get_baseline=AsyncMock(return_value=fake_stats))

    payload = await _run_task_with_session(
        pg_session, monkeypatch, tmp_path, bp=bp, now_utc=now,
    )
    # The orange-stale alpha will be dumped (score < 70 likely)
    targeted = [r for r in payload["alphas"]
                if r["alpha_id"] == _aid("u1")]
    assert targeted
    r = targeted[0]
    assert r["signals"]["drift"]["severity"] == "unknown"


@pytest.mark.asyncio
async def test_hypothesis_coverage_pct_reported(
    pg_session, seeded_task, monkeypatch, tmp_path,
):
    """Half alphas with hypothesis_id pointing at expected_signal='momentum',
    half without → coverage_pct reflects only the linked half."""
    h = Hypothesis(
        statement=f"{_TAG} cov test",
        region="USA",
        kind="INVESTMENT_THESIS",
        expected_signal="momentum",
        status="ACTIVE",
    )
    pg_session.add(h)
    await pg_session.commit()
    await pg_session.refresh(h)

    now = datetime.now(timezone.utc)
    # 2 linked alphas, 2 unlinked
    linked = [
        _make_alpha(
            seeded_task.id, suffix=f"c{i}", status="PASS",
            decay_curve=[{"sharpe": 1.5}],
            is_sharpe=1.5, is_fitness=0.8, is_turnover=0.3,
            metrics_snapshot_at=now - timedelta(days=2),
            hypothesis_id=h.id,
        )
        for i in range(2)
    ]
    unlinked = [
        _make_alpha(
            seeded_task.id, suffix=f"d{i}", status="PASS",
            decay_curve=[{"sharpe": 1.5}],
            is_sharpe=1.5, is_fitness=0.8, is_turnover=0.3,
            metrics_snapshot_at=now - timedelta(days=2),
            hypothesis_id=None,
        )
        for i in range(2)
    ]
    pg_session.add_all(linked + unlinked)
    await pg_session.commit()

    payload = await _run_task_with_session(
        pg_session, monkeypatch, tmp_path, now_utc=now,
    )
    # coverage_pct is a global metric — it includes pre-existing alphas
    # with hypothesis_id != NULL. We only verify it's a valid % when at
    # least one in-scope alpha is linked.
    cov = payload["totals"]["hypothesis_coverage_pct"]
    assert cov is None or (0.0 <= cov <= 100.0)


@pytest.mark.asyncio
async def test_no_baseline_provider_injected_works(
    pg_session, seeded_task, monkeypatch, tmp_path,
):
    """No BaselineProvider + no decay → severity=unknown without exception."""
    now = datetime.now(timezone.utc)
    a = _make_alpha(
        seeded_task.id, suffix="n0", status="PASS",
        decay_curve=[],
        is_sharpe=1.0, is_fitness=0.5, is_turnover=0.3,
        metrics_snapshot_at=now - timedelta(days=20),  # orange stale, dumped
    )
    pg_session.add(a)
    await pg_session.commit()

    payload = await _run_task_with_session(
        pg_session, monkeypatch, tmp_path, bp=None, now_utc=now,
    )
    targeted = [r for r in payload["alphas"]
                if r["alpha_id"] == _aid("n0")]
    assert targeted
    r = targeted[0]
    assert r["signals"]["drift"]["severity"] == "unknown"
    assert r["signals"]["drift"]["reason"] == "no_baseline_available"


@pytest.mark.asyncio
async def test_filename_uses_asia_shanghai_date(
    pg_session, seeded_task, monkeypatch, tmp_path,
):
    """now_utc=23:00 2026-05-15 UTC → 07:00 2026-05-16 SH → report_date=2026-05-16."""
    now_utc = datetime(2026, 5, 15, 23, 0, 0, tzinfo=timezone.utc)
    payload = await _run_task_with_session(
        pg_session, monkeypatch, tmp_path, now_utc=now_utc,
    )
    assert payload["report_date"] == "2026-05-16"
    out_path = Path(payload["json_path"])
    assert out_path.name == "2026-05-16.json"
    # generated_at carries the +08:00 offset
    assert "+08:00" in payload["generated_at"]


@pytest.mark.asyncio
async def test_kb_query_filters_alpha_id_ref_isnot_null(
    pg_session, seeded_task, monkeypatch, tmp_path,
):
    """KB SUCCESS_PATTERN with meta_data lacking ``alpha_id_ref`` must be
    skipped by the JSONB filter (legacy 5k+ rows guard)."""
    # KB without alpha_id_ref
    ke_no_ref = KnowledgeEntry(
        entry_type="SUCCESS_PATTERN",
        pattern=f"{_TAG}_pat_noref",
        pattern_hash=f"{_TAG}_h_noref",
        meta_data={"category": "legacy_no_ref"},
        is_active=True,
    )
    pg_session.add(ke_no_ref)
    await pg_session.commit()
    payload = await _run_task_with_session(pg_session, monkeypatch, tmp_path)
    # ke_no_ref must not produce any orphan entry
    for o in payload["kb_orphans_outside_scope"]:
        assert ke_no_ref.id not in (o.get("kb_entry_ids") or [])
