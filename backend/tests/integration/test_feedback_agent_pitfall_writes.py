"""Integration test — FeedbackAgent.learn_from_round pitfall writes.

Major #3 follow-up from the negative-knowledge KB pollution fix: helper
unit tests cover the classifier in isolation, but a fresh review flagged
that a future refactor could silently re-introduce a NULL-category write
path while still passing helper tests. This test verifies the CALLER —
the pitfall write loop inside ``learn_from_round`` — actually skips when
the helper returns None, and that classifier_call_log captures every
decision (kept and dropped) for /ops/classifier/stats.
"""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.agents.feedback_agent import FeedbackAgent
from backend.agents.services.llm_service import LLMResponse
from backend.models import ClassifierCallLog, KnowledgeEntry


_FAKE_ANALYSIS = {
    "new_patterns": [],
    "new_pitfalls": [
        # noise: dedup race
        {
            "pattern": "noise_dup_pattern",
            "description": "DB dup",
            "error_type": "DB duplicate",
            "severity": "medium",
            "recommendation": "n/a",
        },
        # noise: BRAIN infra
        {
            "pattern": "noise_infra_pattern",
            "description": "auth fail",
            "error_type": "API auth error",
            "severity": "high",
            "recommendation": "n/a",
        },
        # real signal: threshold
        {
            "pattern": "ts_zscore(close, 60)",
            "description": "low sharpe — neutralize",
            "error_type": "Metrics below threshold",
            "severity": "medium",
            "recommendation": "neutralize sector",
        },
        # real signal: robustness
        {
            "pattern": "winsorize(rank(close), 0.99)",
            "description": "concentrated weights",
            "error_type": "CONCENTRATED_WEIGHT",
            "severity": "high",
            "recommendation": "decay or truncate",
        },
    ],
    "field_insights": {},
    "hypothesis_evolution": {},
}


@pytest.mark.asyncio
async def test_learn_from_round_drops_noise_and_stamps_category(
    db_session, sample_task,
):
    """Mocks the LLM call to return a fixed analysis mixing noise + signal
    pitfalls; verifies (1) only signal pitfalls are persisted to
    knowledge_entries with the right category, (2) every classification
    decision is logged to classifier_call_log."""
    agent = FeedbackAgent(db_session)

    mock_response = LLMResponse(
        content="ignored",
        parsed=_FAKE_ANALYSIS,
        model="mock",
        success=True,
    )

    with patch.object(
        agent.llm_service, "call", AsyncMock(return_value=mock_response)
    ):
        result = await agent.learn_from_round(
            successes=[],
            failures=[{"expression": "x", "error_message": "y"}],
            iteration=3,
            dataset_id="model16",
            region="USA",
            task_id=sample_task.id,
        )

    assert "error" not in result, result

    # 1) knowledge_entries: only 2 signal pitfalls written, noise dropped.
    rows = (await db_session.execute(
        select(KnowledgeEntry).where(
            KnowledgeEntry.entry_type == "FAILURE_PITFALL"
        )
    )).scalars().all()
    patterns = {r.pattern for r in rows}
    assert "noise_dup_pattern" not in patterns
    assert "noise_infra_pattern" not in patterns
    assert "ts_zscore(close, 60)" in patterns
    assert "winsorize(rank(close), 0.99)" in patterns
    # Category stamped on meta_data for the kept rows.
    by_pattern = {r.pattern: r for r in rows}
    assert by_pattern["ts_zscore(close, 60)"].meta_data["category"] == "threshold"
    assert by_pattern["winsorize(rank(close), 0.99)"].meta_data["category"] == "robustness"

    # 2) classifier_call_log: all 4 pitfalls logged with correct resolution.
    log_rows = (await db_session.execute(
        select(ClassifierCallLog).order_by(ClassifierCallLog.id)
    )).scalars().all()
    assert len(log_rows) == 4
    by_error_type = {r.error_type: r for r in log_rows}
    assert by_error_type["DB duplicate"].resolved_category is None
    assert by_error_type["API auth error"].resolved_category is None
    assert by_error_type["Metrics below threshold"].resolved_category == "threshold"
    assert by_error_type["CONCENTRATED_WEIGHT"].resolved_category == "robustness"
    # task_id / iteration / region / dataset_id stamped on every row.
    for r in log_rows:
        assert r.task_id == sample_task.id
        assert r.iteration == 3
        assert r.region == "USA"
        assert r.dataset_id == "model16"


@pytest.mark.asyncio
async def test_learn_from_round_no_pitfalls_no_classifier_rows(
    db_session, sample_task,
):
    """When the LLM returns no pitfalls, classifier_call_log gets no rows
    — proves the log writer doesn't fire on empty input."""
    agent = FeedbackAgent(db_session)
    analysis = {"new_patterns": [], "new_pitfalls": [], "field_insights": {},
                "hypothesis_evolution": {}}
    mock_response = LLMResponse(
        content="ignored", parsed=analysis, model="mock", success=True,
    )
    with patch.object(
        agent.llm_service, "call", AsyncMock(return_value=mock_response)
    ):
        await agent.learn_from_round(
            successes=[],
            failures=[{"expression": "x", "error_message": "y"}],
            iteration=1,
            dataset_id="model16",
            region="USA",
            task_id=sample_task.id,
        )

    log_rows = (await db_session.execute(
        select(ClassifierCallLog)
    )).scalars().all()
    assert len(log_rows) == 0
