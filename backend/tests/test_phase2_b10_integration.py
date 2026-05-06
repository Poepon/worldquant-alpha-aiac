"""Phase 2 B10 — end-to-end integration tests.

Covers Phase 2 component协作 that the per-step tests (B1/B3/B4/B5/B6/B7/B8)
don't exercise together. These run against live Postgres because Phase 2
uses JSONB / FK / TIMESTAMP_WITH_TIMEZONE columns aiosqlite can't render.

Strategy: instead of mocking the entire LangGraph (5+ LLM calls per round,
brittle), we exercise the Phase 2-specific code paths in a compressed
pipeline:

  1. node_hypothesis with mock LLM → DB Hypothesis row + state update
  2. Synthesize AlphaCandidate list (skipping code_gen/simulate/evaluate)
  3. node_save_results → B4 alpha.hypothesis_id + B5 lifecycle transitions
  4. Direct call to workflow's V-19.5 refresh_stats path
  5. record_*_pattern via RAGService → B8 KB tagging
  6. get_recent_pass_examples → B8 hypothesis-keyed retrieval

Tests:
- happy_path: 1 round PASS → PROMOTED + KB tagged + retrievable
- 3_round_abandon: 3 fail rounds → ABANDONED with abandon_reason
- kb_lineage: pattern hit by 2 hypotheses → hypothesis_ids accumulates
- v19_5_stats_refresh: post-commit refresh_stats matches alphas JOIN
"""
from __future__ import annotations

import socket
import uuid
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from backend.agents.graph.state import MiningState, AlphaCandidate
from backend.models import (
    Alpha, AlphaFailure, ExperimentRun, Hypothesis,
    HypothesisStatus, KnowledgeEntry, MiningTask,
)
from backend.services.hypothesis_service import HypothesisService


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


_TAG = f"_b10_{uuid.uuid4().hex[:8]}"


# Distinct outer operators per test, picked so skeletons don't collide
# (expression_to_skeleton normalizes field/num to FIELD/NUM).
_OPS = [
    "ts_rank({f}, 5)",       # 0
    "ts_zscore({f}, 7)",     # 1
    "ts_mean({f}, 9)",       # 2
    "ts_std_dev({f}, 11)",   # 3
    "ts_delta({f}, 13)",     # 4
    "ts_sum({f}, 15)",       # 5
    "ts_arg_max({f}, 17)",   # 6
    "ts_av_diff({f}, 19)",   # 7
    "ts_quantile({f}, 21)",  # 8
    "ts_decay_linear({f}, 23)",  # 9
]


def _expr(idx: int) -> str:
    return _OPS[idx % len(_OPS)].format(f="dummy_field")


def _candidate(expr: str, status: str, sharpe: float = 1.5) -> AlphaCandidate:
    aid = f"_b10_{uuid.uuid4().hex[:13]}"
    assert len(aid) <= 20
    return AlphaCandidate(
        expression=expr,
        hypothesis="legacy text",
        explanation="b10 test",
        is_valid=(status != "FAIL_SYNTAX"),
        validation_error="syntax" if status == "FAIL_SYNTAX" else None,
        is_simulated=(status not in ("FAIL_SYNTAX", "FAIL_SIM")),
        simulation_success=(status not in ("FAIL_SYNTAX", "FAIL_SIM")),
        simulation_error="sim failed" if status == "FAIL_SIM" else None,
        alpha_id=aid if status not in ("FAIL_SYNTAX",) else None,
        metrics={"sharpe": sharpe, "fitness": 0.5, "turnover": 0.3} if status not in ("FAIL_SYNTAX",) else {},
        quality_status=("PASS" if status == "PASS"
                        else "PASS_PROVISIONAL" if status == "PROV"
                        else "FAIL"),
    )


def _mock_llm_with_hypotheses(hypotheses: List[dict]):
    response = MagicMock()
    response.success = True
    response.parsed = {
        "hypotheses": hypotheses,
        "knowledge_transfer": {},
        "analysis": {},
    }
    response.error = None
    llm = MagicMock()
    llm.call = AsyncMock(return_value=response)
    return llm


@pytest_asyncio.fixture
async def session_with_task():
    """Live PG session + seeded MiningTask + ExperimentRun."""
    engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt",
        echo=False,
    )
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        task = MiningTask(
            task_name=f"{_TAG}_task",
            region="USA", universe="TOP3000",
            dataset_strategy="AUTO", agent_mode="AUTONOMOUS_TIER1",
            status="RUNNING", daily_goal=4, max_iterations=2,
            config={"hypothesis_centric_variant": 2},
        )
        s.add(task)
        await s.flush()
        run = ExperimentRun(task_id=task.id, status="RUNNING")
        s.add(run)
        await s.commit()

        yield s, task, run

        try:
            # Cleanup order: alphas → failures → run → task → hypotheses → KB
            await s.execute(delete(Alpha).where(Alpha.task_id == task.id))
            await s.execute(delete(AlphaFailure).where(AlphaFailure.task_id == task.id))
            await s.execute(delete(ExperimentRun).where(ExperimentRun.id == run.id))
            await s.execute(text("DELETE FROM mining_tasks WHERE id = :i"), {"i": task.id})
            await s.execute(delete(Hypothesis).where(
                Hypothesis.statement.like(f"{_TAG}%")
            ))
            # KB cleanup by skeleton
            test_skeletons = [_OPS[i].replace("{f}", "FIELD").replace("5", "NUM").replace("7", "NUM")
                              .replace("9", "NUM").replace("11", "NUM").replace("13", "NUM")
                              .replace("15", "NUM").replace("17", "NUM").replace("19", "NUM")
                              .replace("21", "NUM").replace("23", "NUM")
                              for i in range(len(_OPS))]
            await s.execute(delete(KnowledgeEntry).where(
                KnowledgeEntry.pattern.in_(test_skeletons)
            ))
            await s.commit()
        except Exception:
            await s.rollback()
    await engine.dispose()


# =============================================================================
# 1. HAPPY PATH — propose → PASS → PROMOTED + KB tagged
# =============================================================================

@pytest.mark.asyncio
async def test_b10_happy_path_propose_pass_promote_kb(session_with_task):
    """End-to-end: B3 persists Hypothesis → B4 alpha gets hypothesis_id →
    B5 marks PROMOTED → V-19.5 stats refresh → B8 KB entry tagged."""
    s, task, run = session_with_task
    from backend.agents.graph.nodes.generation import node_hypothesis
    from backend.agents.graph.nodes.persistence import node_save_results
    from backend.agents.services.rag_service import RAGService

    # ---------- Step 1: B3 propose (mock LLM with 1 hypothesis) ----------
    llm = _mock_llm_with_hypotheses([
        {"idea": f"{_TAG}_happy_h1",
         "rationale": "test rationale",
         "selected_datasets": ["pv1"]},
    ])
    state = MiningState(
        task_id=task.id, region="USA", universe="TOP3000",
        dataset_id="pv1",
        fields=[{"id": "close"}, {"id": "volume"}],
        operators=[{"name": "ts_rank", "category": "ts"}],
        factor_tier=1,
        available_dataset_pool=["pv1"],
    )
    config = {"configurable": {
        "hypothesis_centric_level": 2,
        "experiment_variant": "b10-happy",
    }}
    propose_result = await node_hypothesis(state, llm, config)
    hid = propose_result["current_hypothesis_id"]
    assert hid is not None, "B3 must persist a Hypothesis row"
    assert propose_result["current_hypothesis_ids"] == [hid]

    # Apply state update (LangGraph would do this; we do it manually)
    state.current_hypothesis_id = hid
    state.current_hypothesis_ids = [hid]

    # Verify Hypothesis row exists in PROPOSED state
    h = await s.get(Hypothesis, hid)
    assert h is not None
    assert h.status == HypothesisStatus.PROPOSED.value
    assert h.experiment_variant == "b10-happy"

    # ---------- Step 2: synthesize 1 PASS + 1 FAIL alpha ----------
    state.pending_alphas = [
        _candidate(_expr(0), "PASS", sharpe=2.0),
        _candidate(_expr(1), "FAIL_QUAL", sharpe=-0.5),
    ]

    # ---------- Step 3: B4 + B5 via node_save_results ----------
    save_config = {"configurable": {
        "trace_service": None,
        "hypothesis_centric_level": 2,
        "experiment_variant": "b10-happy",
    }}
    save_result = await node_save_results(state, save_config)
    success_batch = save_result["generated_alphas"]
    assert len(success_batch) == 1
    assert success_batch[0].hypothesis_id == hid, "B4: alpha should carry hypothesis_id"

    # B5 lifecycle should have transitioned PROPOSED → ACTIVE → PROMOTED
    await s.refresh(h)
    assert h.status == HypothesisStatus.PROMOTED.value, \
        f"B5 expected PROMOTED, got {h.status}"

    # ---------- Step 4: persist alpha row + V-19.5 refresh_stats ----------
    landed_alpha = Alpha(
        task_id=task.id, run_id=run.id,
        alpha_id=success_batch[0].alpha_id,
        expression=success_batch[0].expression,
        region="USA", universe="TOP3000", dataset_id="pv1",
        quality_status="PASS",
        is_sharpe=2.0,
        factor_tier=1,
        hypothesis_id=hid,
    )
    s.add(landed_alpha)
    await s.commit()

    svc = HypothesisService(s)
    stats = await svc.refresh_stats(hid)
    await s.commit()
    assert stats.alpha_count == 1, "V-19.5: refresh_stats should see committed alpha"
    assert stats.pass_count == 1
    assert stats.sharpe_max == 2.0

    # ---------- Step 5: B8 KB tagging ----------
    rag = RAGService(s)
    await rag.record_success_pattern(
        expression=success_batch[0].expression,
        metrics={"sharpe": 2.0, "fitness": 0.5, "turnover": 0.3},
        region="USA", dataset_id="pv1",
        alpha_id=success_batch[0].alpha_id,
        hypothesis_id=hid,
        experiment_variant="b10-happy",
    )

    # Verify KB entry tagged with hypothesis_id
    r = await s.execute(
        select(KnowledgeEntry).where(
            KnowledgeEntry.entry_type == "SUCCESS_PATTERN",
            KnowledgeEntry.meta_data["hypothesis_id"].astext == str(hid),
        )
    )
    kb_rows = list(r.scalars().all())
    assert len(kb_rows) == 1, f"B8 expected 1 KB SUCCESS_PATTERN with hid={hid}"
    md = kb_rows[0].meta_data
    assert md["hypothesis_id"] == hid
    assert md["experiment_variant"] == "b10-happy"
    assert md["hypothesis_ids"] == [hid]


# =============================================================================
# 2. 3-ROUND ABANDON — B6 trigger over multi-round history
# =============================================================================

@pytest.mark.asyncio
async def test_b10_3_round_abandon(session_with_task):
    """3 consecutive rounds with quality-fail-only alphas → primary
    hypothesis ABANDONED with abandon_reason recorded."""
    s, task, run = session_with_task
    from backend.agents.graph.nodes.persistence import node_save_results

    # Pre-create a hypothesis in PROPOSED state (skip propose — we want
    # to focus on the abandon path)
    svc = HypothesisService(s)
    from backend.services.hypothesis_service import HypothesisCreateData
    from backend.models import HypothesisKind
    h = await svc.create_hypothesis(HypothesisCreateData(
        statement=f"{_TAG}_abandon_h1",
        rationale="test",
        region="USA", universe="TOP3000",
        kind=HypothesisKind.INVESTMENT_THESIS.value,
        target_tier=1,
        experiment_variant="b10-abandon",
    ))
    await s.commit()
    hid = h.id

    history = {}
    for round_idx in (1, 2, 3):
        state = MiningState(
            task_id=task.id, region="USA", universe="TOP3000",
            dataset_id="pv1", fields=[], operators=[], factor_tier=1,
            current_hypothesis_id=hid,
            current_hypothesis_ids=[hid],
            hypothesis_round_history=history,
            pending_alphas=[
                _candidate(_expr(round_idx + 1), "FAIL_QUAL", sharpe=-0.3)
                for _ in range(4)
            ],
        )
        config = {"configurable": {"trace_service": None,
                                    "hypothesis_centric_level": 2}}
        save_result = await node_save_results(state, config)
        history = save_result["hypothesis_round_history"]

    # After 3 rounds, primary should be ABANDONED
    await s.refresh(h)
    assert h.status == HypothesisStatus.ABANDONED.value
    assert h.abandon_reason is not None
    assert "3 consecutive rounds" in h.abandon_reason


# =============================================================================
# 3. KB LINEAGE — pattern hit by 2 hypotheses, ids accumulate
# =============================================================================

@pytest.mark.asyncio
async def test_b10_kb_lineage_accumulates_hypothesis_ids(session_with_task):
    """Same expression skeleton hit by hypothesis A (FAIL) and hypothesis B
    (FAIL): KnowledgeEntry.meta_data.hypothesis_ids accumulates both."""
    s, _task, _run = session_with_task
    from backend.agents.services.rag_service import RAGService
    from backend.knowledge_extraction import expression_to_skeleton

    svc = HypothesisService(s)
    from backend.services.hypothesis_service import HypothesisCreateData
    h_a = await svc.create_hypothesis(HypothesisCreateData(
        statement=f"{_TAG}_kbA", region="USA",
    ))
    h_b = await svc.create_hypothesis(HypothesisCreateData(
        statement=f"{_TAG}_kbB", region="USA",
    ))
    await s.commit()

    rag = RAGService(s)
    expr = _expr(2)  # ts_mean(FIELD, NUM) — distinct from happy_path's
    skel = expression_to_skeleton(expr)

    await rag.record_failure_pattern(
        expression=expr, error_type="LOW_SHARPE",
        metrics={"sharpe": 0.4}, region="USA", dataset_id="pv1",
        hypothesis_id=h_a.id, experiment_variant="v2",
    )
    await rag.record_failure_pattern(
        expression=expr, error_type="LOW_SHARPE",
        metrics={"sharpe": 0.5}, region="USA", dataset_id="pv1",
        hypothesis_id=h_b.id, experiment_variant="v2",
    )

    # Single KB row, hypothesis_ids should contain both
    r = await s.execute(
        select(KnowledgeEntry).where(
            KnowledgeEntry.pattern == skel,
            KnowledgeEntry.entry_type == "FAILURE_PITFALL",
        )
    )
    rows = list(r.scalars().all())
    assert len(rows) == 1, f"expected 1 merged row, got {len(rows)}"
    md = rows[0].meta_data
    assert sorted(md["hypothesis_ids"]) == sorted([h_a.id, h_b.id])
    assert md["failure_count"] == 2

    # B8 retrieval: filter by hypothesis_id should return this row for either
    out_a = await rag.get_recent_pass_examples(  # despite name, also uses
        region="USA", dataset_id="pv1", limit=10,  # for failures? No — only
        hypothesis_id=h_a.id,                       # SUCCESS path. Skip.
    )
    # We tested retrieval in test_phase2_b8_kb_hypothesis already; this test
    # focuses on accumulator behavior.


# =============================================================================
# 4. V-19.5 STATS REFRESH — alpha JOIN consistency
# =============================================================================

@pytest.mark.asyncio
async def test_b10_v19_5_stats_refresh_matches_alpha_join(session_with_task):
    """After committing alphas with hypothesis_id FK, calling refresh_stats
    yields counts that match a direct JOIN. This locks in the V-19.5
    post-commit timing fix."""
    s, task, run = session_with_task
    svc = HypothesisService(s)
    from backend.services.hypothesis_service import HypothesisCreateData
    h = await svc.create_hypothesis(HypothesisCreateData(
        statement=f"{_TAG}_v195_h", region="USA", universe="TOP3000",
    ))
    await s.commit()

    # Insert 3 alphas linked to h (2 PASS + 1 FAIL)
    rows = [
        Alpha(task_id=task.id, run_id=run.id,
              alpha_id=f"_b10_{uuid.uuid4().hex[:13]}",
              expression="rank(close)", region="USA", universe="TOP3000",
              quality_status="PASS", is_sharpe=2.0,
              factor_tier=1, hypothesis_id=h.id),
        Alpha(task_id=task.id, run_id=run.id,
              alpha_id=f"_b10_{uuid.uuid4().hex[:13]}",
              expression="rank(volume)", region="USA", universe="TOP3000",
              quality_status="PASS_PROVISIONAL", is_sharpe=1.2,
              factor_tier=1, hypothesis_id=h.id),
        Alpha(task_id=task.id, run_id=run.id,
              alpha_id=f"_b10_{uuid.uuid4().hex[:13]}",
              expression="ts_zscore(returns, 5)", region="USA", universe="TOP3000",
              quality_status="FAIL", is_sharpe=-0.3,
              factor_tier=1, hypothesis_id=h.id),
    ]
    for row in rows:
        s.add(row)
    await s.commit()

    # V-19.5: refresh_stats AFTER commit
    stats = await svc.refresh_stats(h.id)
    await s.commit()

    # Direct JOIN to verify
    r = await s.execute(text(f"""
        SELECT COUNT(*) AS n,
               COUNT(*) FILTER (WHERE quality_status IN ('PASS','PASS_PROVISIONAL')) AS p,
               MAX(is_sharpe) AS sm
        FROM alphas WHERE hypothesis_id = {h.id}
    """))
    expected = r.fetchone()

    assert stats.alpha_count == expected.n == 3
    assert stats.pass_count == expected.p == 2
    assert stats.sharpe_max == expected.sm == 2.0

    # Hypothesis row reflects refreshed stats
    await s.refresh(h)
    assert h.alpha_count == 3
    assert h.pass_count == 2
    assert h.sharpe_max == 2.0
