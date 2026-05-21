"""G8 Phase A unit tests — Hypothesis forest cross-task reference (2026-05-19).

Coverage split:

A. build_cross_task_hypotheses_block (pure render)
  - empty list → "" (flag-off invariant)
  - one entry renders header + bullet
  - 5-cap: 6 entries → only top 5 rendered
  - missing fields render defensively (no crash)
  - sharpe_avg non-numeric → "?" placeholder

B. PromptContext.cross_task_hypotheses field default
  - new field defaults to []
  - splice into build_hypothesis_prompt is byte-for-byte legacy when empty

C. HypothesisService.fetch_cross_task_promoted
  - filters by region
  - filters by status (PROPOSED excluded)
  - filters by min_pass_count + min_sharpe_avg
  - filters by is_active=False (regime-frozen excluded)
  - optional pillar filter
  - optional experiment_variant filter
  - sorted by sharpe_avg DESC
  - limit honored
"""
from __future__ import annotations

import pytest

from backend.agents.prompts.base import (
    PromptContext,
    build_cross_task_hypotheses_block,
)
from backend.agents.prompts.hypothesis import build_hypothesis_prompt


# ---------------------------------------------------------------------------
# A. build_cross_task_hypotheses_block
# ---------------------------------------------------------------------------


def test_block_empty_returns_empty_string():
    """Flag-off invariant: empty list → splice produces empty string."""
    assert build_cross_task_hypotheses_block([]) == ""


def test_block_one_entry_renders_header_and_bullet():
    hyps = [
        {
            "hypothesis_id": 42,
            "statement": "When momentum surges, mean reversion follows in 5d",
            "pillar": "momentum",
            "sharpe_avg": 1.85,
            "pass_count": 3,
            "alpha_count": 5,
        }
    ]
    out = build_cross_task_hypotheses_block(hyps)
    assert "Cross-task Hypothesis Forest" in out
    assert "**H42**" in out
    assert "pillar=momentum" in out
    assert "sharpe_avg=1.85" in out
    assert "pass=3/5" in out
    assert "When momentum surges" in out


def test_block_caps_at_5_entries():
    hyps = [
        {
            "hypothesis_id": i,
            "statement": f"h{i}",
            "pillar": "value",
            "sharpe_avg": 1.0 + i * 0.1,
            "pass_count": 2,
            "alpha_count": 4,
        }
        for i in range(1, 7)  # 6 entries
    ]
    out = build_cross_task_hypotheses_block(hyps)
    # Only H1..H5 rendered (H6 dropped by cap)
    for i in range(1, 6):
        assert f"**H{i}**" in out
    assert "**H6**" not in out


def test_block_handles_missing_fields_defensively():
    """No statement / null sharpe / null pillar / null counts shouldn't crash."""
    hyps = [
        {"hypothesis_id": 1},  # all else missing
        {"hypothesis_id": 2, "statement": None, "sharpe_avg": None,
         "pillar": None, "pass_count": None, "alpha_count": None},
    ]
    out = build_cross_task_hypotheses_block(hyps)
    assert "**H1**" in out
    assert "**H2**" in out
    # Defensive defaults: ? for missing pillar/sharpe, 0/0 for counts
    assert "pillar=?" in out
    assert "sharpe_avg=?" in out
    assert "pass=0/0" in out


def test_block_sharpe_non_numeric_renders_question_mark():
    hyps = [
        {"hypothesis_id": 3, "statement": "x", "pillar": "value",
         "sharpe_avg": "n/a", "pass_count": 2, "alpha_count": 3},
    ]
    out = build_cross_task_hypotheses_block(hyps)
    assert "sharpe_avg=?" in out


# ---------------------------------------------------------------------------
# B. PromptContext + build_hypothesis_prompt integration
# ---------------------------------------------------------------------------


def test_prompt_context_cross_task_default_empty():
    ctx = PromptContext(dataset_id="pv1", region="USA")
    assert ctx.cross_task_hypotheses == []


def test_build_hypothesis_prompt_no_forest_renders_no_block():
    """With cross_task_hypotheses=[] the rendered prompt must NOT contain
    the G8 header (byte-for-byte legacy at the splice site)."""
    ctx = PromptContext(
        dataset_id="pv1", dataset_description="d", dataset_category="pv",
        region="USA", universe="TOP3000", fields=[], operators=[],
        success_patterns=[], failure_pitfalls=[],
        cross_task_hypotheses=[],
    )
    out = build_hypothesis_prompt(ctx, experiment_trace=None)
    assert "Cross-task Hypothesis Forest" not in out


def test_build_hypothesis_prompt_with_forest_renders_block():
    """With cross_task_hypotheses non-empty the block appears in the prompt."""
    ctx = PromptContext(
        dataset_id="pv1", dataset_description="d", dataset_category="pv",
        region="USA", universe="TOP3000", fields=[], operators=[],
        success_patterns=[], failure_pitfalls=[],
        cross_task_hypotheses=[{
            "hypothesis_id": 7, "statement": "trade vol pop",
            "pillar": "volatility", "sharpe_avg": 1.5,
            "pass_count": 2, "alpha_count": 3,
        }],
    )
    out = build_hypothesis_prompt(ctx, experiment_trace=None)
    assert "Cross-task Hypothesis Forest" in out
    assert "**H7**" in out


# ---------------------------------------------------------------------------
# C. HypothesisService.fetch_cross_task_promoted — integration (real DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_cross_task_promoted_filters_region(db_session):
    from backend.models import Hypothesis
    from backend.services.hypothesis_service import HypothesisService

    # Insert one USA + one CHN hypothesis, both PROMOTED, both above thresholds
    db_session.add(Hypothesis(
        statement="usa h", region="USA", status="PROMOTED",
        pass_count=3, alpha_count=5, sharpe_avg=1.5, is_active=True,
    ))
    db_session.add(Hypothesis(
        statement="chn h", region="CHN", status="PROMOTED",
        pass_count=3, alpha_count=5, sharpe_avg=1.5, is_active=True,
    ))
    await db_session.commit()

    svc = HypothesisService(db_session)
    rows = await svc.fetch_cross_task_promoted(region="USA")
    assert len(rows) == 1
    assert rows[0].region == "USA"


@pytest.mark.asyncio
async def test_fetch_cross_task_promoted_excludes_proposed(db_session):
    from backend.models import Hypothesis
    from backend.services.hypothesis_service import HypothesisService

    db_session.add(Hypothesis(
        statement="proposed h", region="USA", status="PROPOSED",
        pass_count=3, alpha_count=5, sharpe_avg=1.5, is_active=True,
    ))
    db_session.add(Hypothesis(
        statement="active h", region="USA", status="ACTIVE",
        pass_count=3, alpha_count=5, sharpe_avg=1.5, is_active=True,
    ))
    db_session.add(Hypothesis(
        statement="promoted h", region="USA", status="PROMOTED",
        pass_count=3, alpha_count=5, sharpe_avg=1.5, is_active=True,
    ))
    await db_session.commit()

    svc = HypothesisService(db_session)
    rows = await svc.fetch_cross_task_promoted(region="USA")
    statements = {r.statement for r in rows}
    # PROPOSED excluded; ACTIVE + PROMOTED included
    assert statements == {"active h", "promoted h"}


@pytest.mark.asyncio
async def test_fetch_cross_task_promoted_filters_min_pass_count(db_session):
    from backend.models import Hypothesis
    from backend.services.hypothesis_service import HypothesisService

    db_session.add(Hypothesis(
        statement="too few passes", region="USA", status="PROMOTED",
        pass_count=1, alpha_count=2, sharpe_avg=2.0, is_active=True,
    ))
    db_session.add(Hypothesis(
        statement="enough passes", region="USA", status="PROMOTED",
        pass_count=3, alpha_count=5, sharpe_avg=1.2, is_active=True,
    ))
    await db_session.commit()

    svc = HypothesisService(db_session)
    rows = await svc.fetch_cross_task_promoted(region="USA", min_pass_count=2)
    assert len(rows) == 1
    assert rows[0].statement == "enough passes"


@pytest.mark.asyncio
async def test_fetch_cross_task_promoted_filters_min_sharpe(db_session):
    from backend.models import Hypothesis
    from backend.services.hypothesis_service import HypothesisService

    db_session.add(Hypothesis(
        statement="low sharpe", region="USA", status="PROMOTED",
        pass_count=3, alpha_count=5, sharpe_avg=0.5, is_active=True,
    ))
    db_session.add(Hypothesis(
        statement="ok sharpe", region="USA", status="PROMOTED",
        pass_count=3, alpha_count=5, sharpe_avg=1.5, is_active=True,
    ))
    db_session.add(Hypothesis(
        statement="null sharpe", region="USA", status="PROMOTED",
        pass_count=3, alpha_count=5, sharpe_avg=None, is_active=True,
    ))
    await db_session.commit()

    svc = HypothesisService(db_session)
    rows = await svc.fetch_cross_task_promoted(region="USA", min_sharpe_avg=1.0)
    assert len(rows) == 1
    assert rows[0].statement == "ok sharpe"


@pytest.mark.asyncio
async def test_fetch_cross_task_promoted_excludes_regime_frozen(db_session):
    from backend.models import Hypothesis
    from backend.services.hypothesis_service import HypothesisService

    db_session.add(Hypothesis(
        statement="frozen h", region="USA", status="PROMOTED",
        pass_count=3, alpha_count=5, sharpe_avg=2.0, is_active=False,  # frozen
    ))
    db_session.add(Hypothesis(
        statement="active h", region="USA", status="PROMOTED",
        pass_count=3, alpha_count=5, sharpe_avg=1.5, is_active=True,
    ))
    await db_session.commit()

    svc = HypothesisService(db_session)
    rows = await svc.fetch_cross_task_promoted(region="USA")
    assert len(rows) == 1
    assert rows[0].statement == "active h"


@pytest.mark.asyncio
async def test_fetch_cross_task_promoted_pillar_filter(db_session):
    from backend.models import Hypothesis
    from backend.services.hypothesis_service import HypothesisService

    db_session.add(Hypothesis(
        statement="momentum h", region="USA", status="PROMOTED",
        pass_count=3, alpha_count=5, sharpe_avg=1.5, is_active=True,
        pillar="momentum",
    ))
    db_session.add(Hypothesis(
        statement="value h", region="USA", status="PROMOTED",
        pass_count=3, alpha_count=5, sharpe_avg=1.5, is_active=True,
        pillar="value",
    ))
    await db_session.commit()

    svc = HypothesisService(db_session)
    rows = await svc.fetch_cross_task_promoted(region="USA", pillar="value")
    assert len(rows) == 1
    assert rows[0].statement == "value h"


@pytest.mark.asyncio
async def test_fetch_cross_task_promoted_experiment_variant_filter(db_session):
    from backend.models import Hypothesis
    from backend.services.hypothesis_service import HypothesisService

    db_session.add(Hypothesis(
        statement="v1 h", region="USA", status="PROMOTED",
        pass_count=3, alpha_count=5, sharpe_avg=1.5, is_active=True,
        experiment_variant="1",
    ))
    db_session.add(Hypothesis(
        statement="v2 h", region="USA", status="PROMOTED",
        pass_count=3, alpha_count=5, sharpe_avg=1.5, is_active=True,
        experiment_variant="2",
    ))
    await db_session.commit()

    svc = HypothesisService(db_session)
    rows = await svc.fetch_cross_task_promoted(region="USA", experiment_variant="2")
    assert len(rows) == 1
    assert rows[0].statement == "v2 h"


@pytest.mark.asyncio
async def test_fetch_cross_task_promoted_sorted_by_sharpe_desc(db_session):
    from backend.models import Hypothesis
    from backend.services.hypothesis_service import HypothesisService

    for stmt, sh in [("h_low", 1.1), ("h_high", 2.5), ("h_mid", 1.7)]:
        db_session.add(Hypothesis(
            statement=stmt, region="USA", status="PROMOTED",
            pass_count=3, alpha_count=5, sharpe_avg=sh, is_active=True,
        ))
    await db_session.commit()

    svc = HypothesisService(db_session)
    rows = await svc.fetch_cross_task_promoted(region="USA")
    statements = [r.statement for r in rows]
    assert statements == ["h_high", "h_mid", "h_low"]


@pytest.mark.asyncio
async def test_fetch_cross_task_promoted_limit_honored(db_session):
    from backend.models import Hypothesis
    from backend.services.hypothesis_service import HypothesisService

    for i in range(10):
        db_session.add(Hypothesis(
            statement=f"h{i}", region="USA", status="PROMOTED",
            pass_count=3, alpha_count=5, sharpe_avg=1.5 + i * 0.1,
            is_active=True,
        ))
    await db_session.commit()

    svc = HypothesisService(db_session)
    rows = await svc.fetch_cross_task_promoted(region="USA", limit=3)
    assert len(rows) == 3
