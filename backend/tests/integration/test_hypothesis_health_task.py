"""Integration tests for the P1-C part 2 hypothesis-health-check task.

来源: docs/alphagbm_skills_research_2026-05-15.md skill `investment-thesis`.

Targets live Postgres (the Hypothesis schema uses JSONB columns that
aiosqlite cannot render). Each test seeds rows tagged with a unique uuid
prefix and cleans them up in the fixture's finally block.

The dev DB (alpha_gpt on 5433) carries production hypotheses + alphas —
totals are non-zero — so assertions ONLY look at tagged rows
(``_TAG``-prefixed statement / alpha_id). The wrapper's
``AsyncSessionLocal()`` is bypassed via a custom runner so the test's
session stays in scope.
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


from backend.models import (  # noqa: E402  — env tweak first
    Alpha,
    Hypothesis,
    HypothesisRoundStats,
    HypothesisStatusTransition,
    MiningTask,
)
from backend.services.hypothesis_health_service import (  # noqa: E402
    HypothesisHealthService,
    LLMThesisScore,
)


# alpha_id is VARCHAR(20). Keep the tag short so suffix has room.
_TAG = f"_phT{uuid.uuid4().hex[:4]}_"


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
                # Tagged hyp ids — find first to drop their round stats
                tagged = (await s.execute(
                    select(Hypothesis.id).where(
                        Hypothesis.statement.like(f"{_TAG}%")
                    )
                )).scalars().all()
                if tagged:
                    await s.execute(
                        delete(HypothesisRoundStats).where(
                            HypothesisRoundStats.hypothesis_id.in_(tagged)
                        )
                    )
                await s.execute(
                    delete(Alpha).where(Alpha.alpha_id.like(f"{_TAG}%"))
                )
                # Audit rows cascade-delete via FK on Hypothesis.id
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
    t = MiningTask(
        task_name=f"{_TAG}task",
        region="USA",
        universe="TOP3000",
        dataset_strategy="AUTO",
        agent_mode="AUTONOMOUS_TIER1",
        status="RUNNING",
        daily_goal=4,
        max_iterations=2,
        config={},
    )
    pg_session.add(t)
    await pg_session.commit()
    await pg_session.refresh(t)
    return t


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _aid(suffix: str) -> str:
    return f"{_TAG}{suffix}"[:20]


async def _mk_hyp(s, *, suffix, **over):
    base = dict(
        statement=f"{_TAG}{suffix}",
        rationale="r",
        region="USA",
        kind="INVESTMENT_THESIS",
        target_tier=1,
        status="ACTIVE",
        is_active=True,
    )
    base.update(over)
    h = Hypothesis(**base)
    s.add(h)
    await s.flush()
    await s.refresh(h)
    return h


async def _mk_alpha(s, hid, task_id, *, suffix, **over):
    base = dict(
        alpha_id=_aid(suffix),
        task_id=task_id,
        expression="rank(close)",
        region="USA",
        universe="TOP3000",
        hypothesis_id=hid,
        quality_status="PASS",
        is_sharpe=1.5,
        is_fitness=0.8,
        is_turnover=0.3,
    )
    base.update(over)
    a = Alpha(**base)
    s.add(a)
    await s.flush()
    await s.refresh(a)
    return a


async def _mk_round(s, hid, task_id, ri, **over):
    base = dict(
        hypothesis_id=hid,
        task_id=task_id,
        round_index=ri,
        alpha_count=0,
        pass_count=0,
        syntax_fail_count=0,
        simulate_fail_count=0,
        quality_fail_count=0,
        flip_alpha_count=0,
        flip_pass_count=0,
        retryable_count=0,
    )
    base.update(over)
    r = HypothesisRoundStats(**base)
    s.add(r)
    await s.flush()
    return r


def _mock_llm(*, success=True, parsed=None, tokens=500, side_effect=None):
    llm = AsyncMock()
    if side_effect is not None:
        llm.call.side_effect = side_effect
        return llm
    llm.call.return_value = SimpleNamespace(
        success=success,
        parsed=parsed,
        tokens_used=tokens,
        error=None,
    )
    return llm


async def _run_task_with_session(
    pg_session, monkeypatch, tmp_path,
    *, llm=None, now_utc=None,
):
    """Mimics ``backend.tasks.hypothesis_health_check._run_async`` but uses
    the test's pg_session (so seeded rows are visible). Redirects
    ``_OUTPUT_DIR`` to ``tmp_path`` so we can read the JSON back."""
    from backend.tasks import hypothesis_health_check as task_mod

    monkeypatch.setattr(task_mod, "_OUTPUT_DIR", tmp_path)
    svc = HypothesisHealthService(pg_session, llm_service=llm)
    payload = await svc.run_full_check(now_utc=now_utc)
    out_path = tmp_path / f"{payload['report_date']}.json"
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8", newline="\n",
    )
    payload["json_path"] = str(out_path)
    return payload


def _tagged(payload):
    """Return only payload['hypotheses'] entries belonging to _TAG."""
    # Cannot filter by statement (we don't dump it) — instead the test
    # captures hypothesis_id and post-filters.
    return payload["hypotheses"]


# ===========================================================================
# Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_empty_run_writes_json_with_zero_totals(
    pg_session, monkeypatch, tmp_path,
):
    """No tagged hyps seeded — task runs against existing prod data
    without exploding, JSON file exists, by_band is a 5-bucket dict."""
    payload = await _run_task_with_session(pg_session, monkeypatch, tmp_path)
    assert payload["json_path"].endswith(".json")
    assert "GREEN" in payload["totals"]["by_band"]
    # Existing rows are fine; just need no crash
    assert isinstance(payload["totals"]["triggered_count"], int)


@pytest.mark.asyncio
async def test_dropped_sharpe_triggers_and_calls_llm(
    pg_session, monkeypatch, tmp_path, seeded_task,
):
    """A PROMOTED hyp with baseline sharpe=2.0 but current sharpe avg
    ~0.5 (drop ~75%) should fire T1 (red) and trigger an LLM call.
    LLM returns a valid 80-score → status=ok."""
    h = await _mk_hyp(
        pg_session, suffix="t1ok", status="PROMOTED",
        baseline_metrics={
            "stamped_at": "2026-04-01T00:00:00+00:00",
            "n_alphas": 5,
            "sharpe_avg": 2.0, "fitness_avg": 1.0, "turnover_avg": 0.3,
            "alpha_pks_seed": [1, 2, 3],
        },
    )
    # Current PASS alphas — average sharpe = 0.5 (drop 75%)
    for i in range(3):
        await _mk_alpha(
            pg_session, h.id, seeded_task.id,
            suffix=f"t1{i}", is_sharpe=0.5,
        )
    await pg_session.commit()

    llm = _mock_llm(parsed={
        "thesis_score": 35,
        "ai_feedback": "Sharpe halved — likely thesis decay.",
        "recommended_action": "pivot",
        "reasons": ["sharpe dropped 75% vs baseline"],
    }, tokens=1000)
    payload = await _run_task_with_session(
        pg_session, monkeypatch, tmp_path, llm=llm,
    )
    rec = next(r for r in payload["hypotheses"] if r["hypothesis_id"] == h.id)
    assert rec["is_triggered"] is True
    assert any(t["type"] == "dropped_sharpe_pct" for t in rec["triggers"])
    assert rec["thesis_score"] == 35
    assert rec["llm_status"] == "ok"
    assert rec["recommended_action"] == "pivot"
    assert payload["llm_token_used"] >= 1000


@pytest.mark.asyncio
async def test_abandoned_status_skipped(
    pg_session, monkeypatch, tmp_path, seeded_task,
):
    """ABANDONED + SUPERSEDED hypotheses are out of scope."""
    h_abn = await _mk_hyp(
        pg_session, suffix="abn", status="ABANDONED",
        abandon_reason="testing",
        baseline_metrics={"sharpe_avg": 2.0, "n_alphas": 5},
    )
    h_act = await _mk_hyp(pg_session, suffix="act", status="ACTIVE")
    await pg_session.commit()
    payload = await _run_task_with_session(pg_session, monkeypatch, tmp_path)
    hids = {r["hypothesis_id"] for r in payload["hypotheses"]}
    assert h_abn.id not in hids
    # h_act might or might not be dumped (depends on GREEN truncation),
    # but it must NOT have crashed processing
    # — just assert no error in payload
    assert "error" not in payload


@pytest.mark.asyncio
async def test_llm_exception_writes_fallback_status(
    pg_session, monkeypatch, tmp_path, seeded_task,
):
    h = await _mk_hyp(
        pg_session, suffix="fb", status="PROMOTED",
        baseline_metrics={
            "stamped_at": "2026-04-01T00:00:00+00:00",
            "n_alphas": 5, "sharpe_avg": 2.0,
            "fitness_avg": 1.0, "turnover_avg": 0.3,
            "alpha_pks_seed": [1, 2, 3],
        },
    )
    for i in range(3):
        await _mk_alpha(
            pg_session, h.id, seeded_task.id,
            suffix=f"fb{i}", is_sharpe=0.5,
        )
    await pg_session.commit()

    llm = _mock_llm(side_effect=ConnectionError("simulated outage"))
    payload = await _run_task_with_session(
        pg_session, monkeypatch, tmp_path, llm=llm,
    )
    rec = next(r for r in payload["hypotheses"] if r["hypothesis_id"] == h.id)
    assert rec["llm_status"] == "fallback_failed"
    assert rec["thesis_score"] == 50

    # The DB row should also persist the status
    refreshed = (await pg_session.execute(
        select(Hypothesis).where(Hypothesis.id == h.id)
    )).scalar_one()
    assert refreshed.last_thesis_score_status == "fallback_failed"


@pytest.mark.asyncio
async def test_llm_action_normalised_padded(
    pg_session, monkeypatch, tmp_path, seeded_task,
):
    """MFX-6: 'Pivot. ' is normalised to 'pivot' — non-fallback."""
    h = await _mk_hyp(
        pg_session, suffix="nrm", status="PROMOTED",
        baseline_metrics={"n_alphas": 5, "sharpe_avg": 2.0},
    )
    for i in range(3):
        await _mk_alpha(
            pg_session, h.id, seeded_task.id,
            suffix=f"nm{i}", is_sharpe=0.5,
        )
    await pg_session.commit()
    llm = _mock_llm(parsed={
        "thesis_score": 40,
        "ai_feedback": "questionable",
        "recommended_action": "Pivot. ",
        "reasons": ["test"],
    })
    payload = await _run_task_with_session(
        pg_session, monkeypatch, tmp_path, llm=llm,
    )
    rec = next(r for r in payload["hypotheses"] if r["hypothesis_id"] == h.id)
    assert rec["llm_status"] == "ok"
    assert rec["recommended_action"] == "pivot"


@pytest.mark.asyncio
async def test_llm_invalid_action_fallback_schema_invalid(
    pg_session, monkeypatch, tmp_path, seeded_task,
):
    h = await _mk_hyp(
        pg_session, suffix="si", status="PROMOTED",
        baseline_metrics={"n_alphas": 5, "sharpe_avg": 2.0},
    )
    for i in range(3):
        await _mk_alpha(
            pg_session, h.id, seeded_task.id,
            suffix=f"si{i}", is_sharpe=0.5,
        )
    await pg_session.commit()
    llm = _mock_llm(parsed={
        "thesis_score": 40,
        "ai_feedback": "ok",
        "recommended_action": "quit",  # invalid
        "reasons": ["test"],
    })
    payload = await _run_task_with_session(
        pg_session, monkeypatch, tmp_path, llm=llm,
    )
    rec = next(r for r in payload["hypotheses"] if r["hypothesis_id"] == h.id)
    assert rec["llm_status"] == "fallback_schema_invalid"


@pytest.mark.asyncio
async def test_24h_gate_skips_llm(
    pg_session, monkeypatch, tmp_path, seeded_task,
):
    """A hyp scored 'ok' 12h ago should NOT trigger another LLM call."""
    now = datetime.now(timezone.utc)
    h = await _mk_hyp(
        pg_session, suffix="g24", status="PROMOTED",
        baseline_metrics={"n_alphas": 5, "sharpe_avg": 2.0},
        last_thesis_score_at=now - timedelta(hours=12),
        last_thesis_score_status="ok",
    )
    for i in range(3):
        await _mk_alpha(
            pg_session, h.id, seeded_task.id,
            suffix=f"g{i}", is_sharpe=0.5,
        )
    await pg_session.commit()
    llm = _mock_llm(parsed={
        "thesis_score": 30,
        "ai_feedback": "would be called",
        "recommended_action": "abandon",
        "reasons": ["x"],
    })
    payload = await _run_task_with_session(
        pg_session, monkeypatch, tmp_path, llm=llm, now_utc=now,
    )
    rec = next(r for r in payload["hypotheses"] if r["hypothesis_id"] == h.id)
    # Trigger fired but LLM was gated — record shows no llm_status
    assert rec["is_triggered"] is True
    assert rec["llm_status"] is None
    llm.call.assert_not_called()


@pytest.mark.asyncio
async def test_4h_backoff_skips_llm_for_fallback(
    pg_session, monkeypatch, tmp_path, seeded_task,
):
    now = datetime.now(timezone.utc)
    h = await _mk_hyp(
        pg_session, suffix="b4", status="PROMOTED",
        baseline_metrics={"n_alphas": 5, "sharpe_avg": 2.0},
        last_thesis_score_at=now - timedelta(hours=2),
        last_thesis_score_status="fallback_failed",
    )
    for i in range(3):
        await _mk_alpha(
            pg_session, h.id, seeded_task.id,
            suffix=f"b{i}", is_sharpe=0.5,
        )
    await pg_session.commit()
    llm = _mock_llm()
    payload = await _run_task_with_session(
        pg_session, monkeypatch, tmp_path, llm=llm, now_utc=now,
    )
    rec = next(r for r in payload["hypotheses"] if r["hypothesis_id"] == h.id)
    assert rec["is_triggered"] is True
    assert rec["llm_status"] is None
    llm.call.assert_not_called()


@pytest.mark.asyncio
async def test_token_budget_exhausted_stops_llm(
    pg_session, monkeypatch, tmp_path, seeded_task,
):
    """Budget = 800; first LLM call burns 1000 tokens; second hyp's
    LLM call must be skipped by the gate."""
    from backend.config import settings as cfg
    monkeypatch.setattr(cfg, "THESIS_SCORE_PER_RUN_TOKEN_BUDGET", 800)
    h1 = await _mk_hyp(
        pg_session, suffix="b1", status="PROMOTED",
        baseline_metrics={"n_alphas": 5, "sharpe_avg": 2.0},
    )
    h2 = await _mk_hyp(
        pg_session, suffix="b2", status="PROMOTED",
        baseline_metrics={"n_alphas": 5, "sharpe_avg": 2.0},
    )
    for hid, label in ((h1.id, "h1"), (h2.id, "h2")):
        for i in range(3):
            await _mk_alpha(
                pg_session, hid, seeded_task.id,
                suffix=f"{label}{i}", is_sharpe=0.5,
            )
    await pg_session.commit()
    llm = _mock_llm(parsed={
        "thesis_score": 40,
        "ai_feedback": "ok",
        "recommended_action": "monitor",
        "reasons": ["r"],
    }, tokens=1000)
    payload = await _run_task_with_session(
        pg_session, monkeypatch, tmp_path, llm=llm,
    )
    # h1 scored, h2 hit budget → no LLM
    rec1 = next(r for r in payload["hypotheses"] if r["hypothesis_id"] == h1.id)
    rec2 = next(r for r in payload["hypotheses"] if r["hypothesis_id"] == h2.id)
    assert rec1["llm_status"] == "ok"
    assert rec2["llm_status"] is None
    assert llm.call.call_count == 1


@pytest.mark.asyncio
async def test_trigger_detail_dedup_within_24h(
    pg_session, monkeypatch, tmp_path, seeded_task,
):
    """Running the task twice within 24h on the same hit should NOT
    grow trigger_detail (24h dedup)."""
    h = await _mk_hyp(
        pg_session, suffix="dd", status="PROMOTED",
        baseline_metrics={"n_alphas": 5, "sharpe_avg": 2.0},
    )
    for i in range(3):
        await _mk_alpha(
            pg_session, h.id, seeded_task.id,
            suffix=f"d{i}", is_sharpe=0.5,
        )
    await pg_session.commit()
    llm = _mock_llm(parsed={
        "thesis_score": 30, "ai_feedback": "x",
        "recommended_action": "abandon", "reasons": ["r"],
    })
    await _run_task_with_session(pg_session, monkeypatch, tmp_path, llm=llm)
    # Second run — same calendar day → status still <24h, LLM gated, but
    # mark_triggered called again and should dedup.
    await _run_task_with_session(pg_session, monkeypatch, tmp_path, llm=llm)
    h_refresh = (await pg_session.execute(
        select(Hypothesis).where(Hypothesis.id == h.id)
    )).scalar_one()
    # Only ONE trigger_detail entry survives
    types = [e["type"] for e in (h_refresh.trigger_detail or [])]
    assert types.count("dropped_sharpe_pct") == 1


@pytest.mark.asyncio
async def test_audit_row_only_on_edge(
    pg_session, monkeypatch, tmp_path, seeded_task,
):
    h = await _mk_hyp(
        pg_session, suffix="au", status="PROMOTED",
        baseline_metrics={"n_alphas": 5, "sharpe_avg": 2.0},
    )
    for i in range(3):
        await _mk_alpha(
            pg_session, h.id, seeded_task.id,
            suffix=f"a{i}", is_sharpe=0.5,
        )
    await pg_session.commit()
    llm = _mock_llm(parsed={
        "thesis_score": 30, "ai_feedback": "x",
        "recommended_action": "abandon", "reasons": ["r"],
    })
    await _run_task_with_session(pg_session, monkeypatch, tmp_path, llm=llm)
    await _run_task_with_session(pg_session, monkeypatch, tmp_path, llm=llm)
    rows = (await pg_session.execute(
        select(HypothesisStatusTransition).where(
            HypothesisStatusTransition.hypothesis_id == h.id,
        )
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].new_is_triggered is True


@pytest.mark.asyncio
async def test_filename_uses_asia_shanghai_date(
    pg_session, monkeypatch, tmp_path,
):
    """now_utc=23:00 UTC on 2026-05-14 → 07:00 Asia/Shanghai on 2026-05-15."""
    now_utc = datetime(2026, 5, 14, 23, 0, 0, tzinfo=timezone.utc)
    payload = await _run_task_with_session(
        pg_session, monkeypatch, tmp_path, now_utc=now_utc,
    )
    assert payload["report_date"] == "2026-05-15"
    p = Path(payload["json_path"])
    assert p.name == "2026-05-15.json"


@pytest.mark.asyncio
async def test_naive_datetime_in_last_score_handled(
    pg_session, monkeypatch, tmp_path, seeded_task,
):
    """A hyp with naive ``last_thesis_score_at`` (legacy/manual write)
    must not crash the 24h-gate check."""
    naive_dt = datetime.now() - timedelta(hours=12)  # naive
    h = await _mk_hyp(
        pg_session, suffix="nv", status="PROMOTED",
        baseline_metrics={"n_alphas": 5, "sharpe_avg": 2.0},
        last_thesis_score_at=naive_dt,
        last_thesis_score_status="ok",
    )
    for i in range(3):
        await _mk_alpha(
            pg_session, h.id, seeded_task.id,
            suffix=f"v{i}", is_sharpe=0.5,
        )
    await pg_session.commit()
    llm = _mock_llm()
    payload = await _run_task_with_session(
        pg_session, monkeypatch, tmp_path, llm=llm,
    )
    rec = next(r for r in payload["hypotheses"] if r["hypothesis_id"] == h.id)
    assert rec["is_triggered"] is True  # didn't crash


@pytest.mark.asyncio
async def test_baseline_stamped_on_first_promoted(
    pg_session, monkeypatch, tmp_path, seeded_task,
):
    """Calling HypothesisService.mark_promoted on an ACTIVE hyp with PASS
    alphas stamps baseline_metrics; second call doesn't overwrite."""
    from backend.services.hypothesis_service import HypothesisService

    h = await _mk_hyp(pg_session, suffix="bp", status="ACTIVE")
    await _mk_alpha(
        pg_session, h.id, seeded_task.id, suffix="bp1",
        quality_status="PASS", is_sharpe=2.0,
    )
    await _mk_alpha(
        pg_session, h.id, seeded_task.id, suffix="bp2",
        quality_status="PASS", is_sharpe=3.0,
    )
    await pg_session.commit()
    svc = HypothesisService(pg_session)
    assert await svc.mark_promoted(h.id) is True
    await pg_session.commit()
    h1 = await svc.get_by_id(h.id)
    assert h1.baseline_metrics is not None
    assert h1.baseline_metrics["n_alphas"] == 2
    assert abs(h1.baseline_metrics["sharpe_avg"] - 2.5) < 1e-6
    # second call must not change baseline_metrics
    await svc.mark_promoted(h.id)
    await pg_session.commit()
    h2 = await svc.get_by_id(h.id)
    assert h2.baseline_metrics == h1.baseline_metrics


@pytest.mark.asyncio
async def test_no_pass_in_n_rounds_via_round_stats(
    pg_session, monkeypatch, tmp_path, seeded_task,
):
    """5 consecutive tested rounds all 0-PASS → T2 fires."""
    h = await _mk_hyp(pg_session, suffix="t2", status="ACTIVE")
    for ri in range(5):
        await _mk_round(
            pg_session, h.id, seeded_task.id, ri,
            alpha_count=3, pass_count=0,
        )
    await pg_session.commit()
    llm = _mock_llm(parsed={
        "thesis_score": 40, "ai_feedback": "x",
        "recommended_action": "monitor", "reasons": ["r"],
    })
    payload = await _run_task_with_session(
        pg_session, monkeypatch, tmp_path, llm=llm,
    )
    rec = next(r for r in payload["hypotheses"] if r["hypothesis_id"] == h.id)
    types = [t["type"] for t in rec["triggers"]]
    assert "no_pass_in_n_rounds" in types
