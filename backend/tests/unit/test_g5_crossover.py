"""G5 Phase A unit tests — trajectory crossover (2026-05-19).

Coverage:

A. llm_crossover_alpha module — pure functions
  - build_crossover_prompt renders both parents + metrics + region + top_k
  - _parse_offspring drops malformed entries (missing <A> or <B>, dup)
  - _substitute_parents replaces both placeholders
  - llm_crossover_alpha empty parent → empty list
  - llm_crossover_alpha identical parents → empty list (degenerate)
  - llm_crossover_alpha LLM exception → soft-fail empty
  - llm_crossover_alpha success path returns substituted offspring

B. g5_persistence helpers
  - persist_offspring_after_round empty input → False no-op
  - persist_offspring_after_round writes to task.config + commits
  - consume_pending_offspring None / missing → None
  - consume_pending_offspring pops + clears slot
  - consume_pending_offspring DB commit error → None + rollback

C. MiningState.g5_offspring_candidates default empty
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.agents import llm_crossover_alpha as g5
from backend.agents.graph.nodes.g5_persistence import (
    CONFIG_KEY_PENDING_OFFSPRING,
    consume_pending_offspring,
    persist_offspring_after_round,
)
from backend.agents.graph.state import MiningState


# ---------------------------------------------------------------------------
# A. llm_crossover_alpha pure-function helpers
# ---------------------------------------------------------------------------


def test_build_prompt_renders_parents_and_metrics():
    p = g5.build_crossover_prompt(
        "ts_rank(returns, 20)",
        "group_neutralize(ts_mean(close, 10), subindustry)",
        parent_a_metrics={"sharpe": 1.50, "fitness": 1.20, "turnover": 0.40},
        parent_b_metrics={"sharpe": 1.80, "fitness": 1.00, "turnover": 0.50},
        parent_a_pillar="momentum",
        parent_b_pillar="value",
        region="USA",
        top_k=2,
    )
    assert "ts_rank(returns, 20)" in p
    assert "group_neutralize" in p
    assert "1.500" in p  # parent_a sharpe formatted to 3 decimals
    assert "1.800" in p
    assert "momentum" in p
    assert "value" in p
    assert "USA" in p
    assert "Return at most 2" in p


def test_build_prompt_handles_missing_metrics_with_question_mark():
    p = g5.build_crossover_prompt(
        "a_expr", "b_expr",
        parent_a_metrics=None, parent_b_metrics={"sharpe": None},
        region="CHN", top_k=1,
    )
    assert "sharpe=?" in p


def test_parse_offspring_drops_missing_placeholder():
    content = """{
        "offspring": [
            {"expression": "multiply(<A>, <B>)", "combination_strategy": "weighted_sum", "rationale": "ok"},
            {"expression": "rank(<A>)", "combination_strategy": "single_parent", "rationale": "missing B"},
            {"expression": "<A>", "combination_strategy": "x", "rationale": "missing B too"}
        ]
    }"""
    out = g5._parse_offspring(content, max_offspring=5)
    assert len(out) == 1
    assert out[0]["expression"] == "multiply(<A>, <B>)"


def test_parse_offspring_dedupes():
    content = """{"offspring": [
        {"expression": "multiply(<A>, <B>)", "combination_strategy": "x", "rationale": "ok"},
        {"expression": "multiply(<A>, <B>)", "combination_strategy": "y", "rationale": "dup"}
    ]}"""
    out = g5._parse_offspring(content, max_offspring=5)
    assert len(out) == 1


def test_parse_offspring_respects_max():
    content = """{"offspring": [
        {"expression": "add(<A>, <B>)", "combination_strategy": "x", "rationale": "1"},
        {"expression": "subtract(<A>, <B>)", "combination_strategy": "y", "rationale": "2"},
        {"expression": "multiply(<A>, <B>)", "combination_strategy": "z", "rationale": "3"}
    ]}"""
    out = g5._parse_offspring(content, max_offspring=2)
    assert len(out) == 2


def test_parse_offspring_malformed_returns_empty():
    assert g5._parse_offspring("not json", max_offspring=2) == []
    assert g5._parse_offspring('{"wrong_key": []}', max_offspring=2) == []
    assert g5._parse_offspring('{"offspring": "not a list"}', max_offspring=2) == []


def test_substitute_parents_replaces_both():
    o = {"expression": "add(<A>, multiply(<B>, 0.5))", "combination_strategy": "x", "rationale": "y"}
    out = g5._substitute_parents(o, "expr_a", "expr_b")
    assert out["expression"] == "add(expr_a, multiply(expr_b, 0.5))"
    assert out["combination_strategy"] == "x"


@pytest.mark.asyncio
async def test_llm_crossover_empty_parent_returns_empty():
    svc = MagicMock()
    svc.call = AsyncMock()
    out = await g5.llm_crossover_alpha("", "expr_b", region="USA", llm_service=svc)
    assert out == []
    svc.call.assert_not_called()


@pytest.mark.asyncio
async def test_llm_crossover_identical_parents_returns_empty():
    svc = MagicMock()
    svc.call = AsyncMock()
    out = await g5.llm_crossover_alpha("expr_x", "expr_x", region="USA", llm_service=svc)
    assert out == []
    svc.call.assert_not_called()


@pytest.mark.asyncio
async def test_llm_crossover_llm_exception_returns_empty():
    svc = MagicMock()
    svc.call = AsyncMock(side_effect=RuntimeError("provider down"))
    svc.model = "deepseek-chat"
    out = await g5.llm_crossover_alpha(
        "expr_a", "expr_b", region="USA", llm_service=svc, top_k=2,
    )
    assert out == []


@pytest.mark.asyncio
async def test_llm_crossover_success_path_substitutes():
    svc = MagicMock()
    svc.model = "deepseek-chat"
    resp = MagicMock()
    resp.content = """{"offspring": [
        {"expression": "multiply(rank(<A>), rank(<B>))", "combination_strategy": "cross_sectional_confirm", "rationale": "agreement amplifier"}
    ]}"""
    svc.call = AsyncMock(return_value=resp)
    out = await g5.llm_crossover_alpha(
        "ts_rank(returns, 20)",
        "ts_mean(close, 10)",
        region="USA",
        llm_service=svc,
        top_k=2,
    )
    assert len(out) == 1
    assert out[0]["expression"] == "multiply(rank(ts_rank(returns, 20)), rank(ts_mean(close, 10)))"
    assert out[0]["combination_strategy"] == "cross_sectional_confirm"
    svc.call.assert_awaited_once()


# ---------------------------------------------------------------------------
# B. g5_persistence helpers
# ---------------------------------------------------------------------------


def _make_task_config_db(initial_config=None):
    """Build a mock task + db that records config writes + commits."""
    task = MagicMock()
    task.id = 42
    task.config = dict(initial_config or {})
    db = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return task, db


@pytest.mark.asyncio
async def test_persist_offspring_empty_input_returns_false():
    task, db = _make_task_config_db()
    out = await persist_offspring_after_round(task, db, [])
    assert out is False
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_persist_offspring_writes_clean_entries():
    task, db = _make_task_config_db()
    offspring = [
        {"expression": "valid expr", "combination_strategy": "x", "rationale": "y"},
        {"expression": "", "combination_strategy": "z", "rationale": "w"},  # dropped
        {"not_a_dict": True},  # dropped
    ]
    out = await persist_offspring_after_round(task, db, offspring)
    assert out is True
    assert CONFIG_KEY_PENDING_OFFSPRING in task.config
    persisted = task.config[CONFIG_KEY_PENDING_OFFSPRING]
    assert len(persisted) == 1
    assert persisted[0]["expression"] == "valid expr"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_offspring_commit_error_returns_false():
    task, db = _make_task_config_db()
    db.commit = AsyncMock(side_effect=RuntimeError("db down"))
    offspring = [{"expression": "x", "combination_strategy": "y", "rationale": "z"}]
    out = await persist_offspring_after_round(task, db, offspring)
    assert out is False
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_consume_pending_offspring_none_when_missing():
    task, db = _make_task_config_db()
    out = await consume_pending_offspring(task, db)
    assert out is None
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_consume_pending_offspring_pops_and_clears():
    stash = [
        {"expression": "a", "combination_strategy": "x", "rationale": "y"},
        {"expression": "b", "combination_strategy": "x", "rationale": "z"},
    ]
    task, db = _make_task_config_db({CONFIG_KEY_PENDING_OFFSPRING: stash, "other_key": "preserved"})
    out = await consume_pending_offspring(task, db)
    assert out is not None
    assert len(out) == 2
    # Slot cleared
    assert CONFIG_KEY_PENDING_OFFSPRING not in task.config
    # Other keys preserved
    assert task.config["other_key"] == "preserved"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_consume_pending_offspring_malformed_data_returns_none():
    task, db = _make_task_config_db({CONFIG_KEY_PENDING_OFFSPRING: "not a list"})
    out = await consume_pending_offspring(task, db)
    assert out is None


@pytest.mark.asyncio
async def test_consume_pending_offspring_filters_empty_expressions():
    """All entries have empty expression → return None (no-op)."""
    stash = [{"expression": "", "x": 1}, {"not_dict": True}]
    task, db = _make_task_config_db({CONFIG_KEY_PENDING_OFFSPRING: stash})
    out = await consume_pending_offspring(task, db)
    assert out is None


@pytest.mark.asyncio
async def test_consume_pending_offspring_db_error_returns_none():
    stash = [{"expression": "x"}]
    task, db = _make_task_config_db({CONFIG_KEY_PENDING_OFFSPRING: stash})
    db.commit = AsyncMock(side_effect=RuntimeError("db down"))
    out = await consume_pending_offspring(task, db)
    assert out is None
    db.rollback.assert_awaited_once()


# ---------------------------------------------------------------------------
# C. MiningState.g5_offspring_candidates field
# ---------------------------------------------------------------------------


def test_mining_state_g5_field_default_empty():
    s = MiningState(
        task_id=1, region="USA", universe="TOP3000", dataset_id="pv1",
        fields=[], operators=[], num_alphas_target=3,
    )
    assert s.g5_offspring_candidates == []


def test_mining_state_g5_field_accepts_inject():
    s = MiningState(
        task_id=1, region="USA", universe="TOP3000", dataset_id="pv1",
        fields=[], operators=[], num_alphas_target=3,
        g5_offspring_candidates=[
            {"expression": "x", "parent_a_alpha_id": 10, "parent_b_alpha_id": 20},
        ],
    )
    assert len(s.g5_offspring_candidates) == 1
    assert s.g5_offspring_candidates[0]["parent_a_alpha_id"] == 10
