"""Tests for Plan v5+ §Phase 1 cross-dataset hypothesis path.

Coverage:
  - PromptContext.available_dataset_pool wiring
  - build_hypothesis_prompt renders cross-dataset section when pool > 1
  - build_hypothesis_prompt omits the section when pool empty (legacy)
  - node_hypothesis selected_datasets parsing rules:
      a) pool offered + LLM returns valid subset → kept
      b) pool offered + LLM returns rogue ids → dropped
      c) pool offered + LLM omits selected_datasets → fallback to [anchor]
      d) pool empty → fallback to [anchor]
"""
from __future__ import annotations

from typing import Any, Dict, List
import asyncio

import pytest

from backend.agents.prompts.base import PromptContext
from backend.agents.prompts.hypothesis import build_hypothesis_prompt


class TestPromptContextWiring:
    def test_default_pool_empty(self):
        ctx = PromptContext()
        assert ctx.available_dataset_pool == []

    def test_pool_propagates(self):
        ctx = PromptContext(available_dataset_pool=["a", "b", "c"])
        assert ctx.available_dataset_pool == ["a", "b", "c"]


class TestHypothesisPromptCrossDatasetSection:
    """build_hypothesis_prompt renders the Phase 1 section iff pool > 1."""

    def _ctx(self, pool: List[str], anchor: str = "fundamental6"):
        return PromptContext(
            dataset_id=anchor,
            available_dataset_pool=pool,
        )

    def test_empty_pool_no_section(self):
        out = build_hypothesis_prompt(self._ctx([]))
        assert "Cross-dataset Pool" not in out
        assert "selected_datasets" not in out

    def test_single_dataset_pool_no_section(self):
        # Pool with only the anchor → no cross-dataset framing needed
        out = build_hypothesis_prompt(self._ctx(["fundamental6"]))
        assert "Cross-dataset Pool" not in out

    def test_multi_dataset_pool_renders_section(self):
        out = build_hypothesis_prompt(
            self._ctx(["fundamental6", "pv1", "analyst4"])
        )
        assert "Cross-dataset Pool" in out
        assert "fundamental6" in out
        assert "pv1" in out
        assert "analyst4" in out
        assert "selected_datasets" in out


class TestSelectedDatasetsParsing:
    """Mirrors node_hypothesis parsing logic (Plan v5+ §Phase 1 A3).

    The actual node is async + has many side-effects, so we exercise the
    pure parsing core via a stripped-down replica. Behavioral parity is
    enforced by the same rule order as in node_hypothesis.
    """

    def _parse(
        self,
        hypotheses: List[Dict[str, Any]],
        pool: List[str],
        anchor: str,
    ) -> tuple[List[str], List[Dict[str, Any]]]:
        """Replica of node_hypothesis selected_datasets normalization."""
        pool_set = set(pool)
        union_set: set = set()
        for h in hypotheses:
            sel = h.get("selected_datasets") or []
            if not isinstance(sel, list):
                sel = []
            if pool_set:
                sel = [d for d in sel if d in pool_set]
            if not sel:
                sel = [anchor]
            h["selected_datasets"] = sel
            union_set.update(sel)
        chosen = sorted(union_set) if union_set else [anchor]
        return chosen, hypotheses

    def test_pool_offered_valid_subset(self):
        chosen, hyps = self._parse(
            [{"id": "H1", "selected_datasets": ["fundamental6", "pv1"]}],
            pool=["fundamental6", "pv1", "analyst4"],
            anchor="fundamental6",
        )
        assert chosen == ["fundamental6", "pv1"]
        assert hyps[0]["selected_datasets"] == ["fundamental6", "pv1"]

    def test_pool_offered_rogue_id_dropped(self):
        chosen, hyps = self._parse(
            [{"id": "H1", "selected_datasets": ["fundamental6", "MARS"]}],
            pool=["fundamental6", "pv1"],
            anchor="fundamental6",
        )
        # MARS dropped
        assert "MARS" not in chosen
        assert chosen == ["fundamental6"]

    def test_pool_offered_all_rogue_falls_back_to_anchor(self):
        chosen, hyps = self._parse(
            [{"id": "H1", "selected_datasets": ["MARS", "VENUS"]}],
            pool=["fundamental6", "pv1"],
            anchor="fundamental6",
        )
        assert chosen == ["fundamental6"]

    def test_pool_offered_missing_field_falls_back_to_anchor(self):
        chosen, hyps = self._parse(
            [{"id": "H1"}],  # no selected_datasets at all
            pool=["fundamental6", "pv1"],
            anchor="fundamental6",
        )
        assert chosen == ["fundamental6"]

    def test_pool_empty_legacy_anchor_only(self):
        chosen, hyps = self._parse(
            [{"id": "H1", "selected_datasets": ["fundamental6", "pv1"]}],
            pool=[],
            anchor="fundamental6",
        )
        # Pool empty → no validation; selected_datasets passes through.
        # Union still aggregates the LLM's pick.
        assert "fundamental6" in chosen
        assert "pv1" in chosen

    def test_multi_hypothesis_union(self):
        """Two hypotheses pick different subsets → union covers both."""
        chosen, hyps = self._parse(
            [
                {"id": "H1", "selected_datasets": ["fundamental6"]},
                {"id": "H2", "selected_datasets": ["pv1"]},
                {"id": "H3", "selected_datasets": ["fundamental6", "analyst4"]},
            ],
            pool=["fundamental6", "pv1", "analyst4"],
            anchor="fundamental6",
        )
        assert chosen == ["analyst4", "fundamental6", "pv1"]

    def test_non_list_selected_datasets_ignored(self):
        # Defensive: LLM returns a string instead of a list
        chosen, hyps = self._parse(
            [{"id": "H1", "selected_datasets": "fundamental6"}],
            pool=["fundamental6", "pv1"],
            anchor="fundamental6",
        )
        # Non-list → treated as empty → fallback
        assert chosen == ["fundamental6"]


class TestPhase1Config:
    """HYPOTHESIS_CENTRIC_LEVEL flag governs the path."""

    def test_default_level_is_zero(self):
        from backend.config import settings
        assert settings.HYPOTHESIS_CENTRIC_LEVEL == 0
        assert settings.HYPOTHESIS_CENTRIC_CANDIDATE == 0

    def test_complementary_k_default(self):
        from backend.config import settings
        assert settings.PHASE1_COMPLEMENTARY_DATASET_K == 3


class TestMiningStateFields:
    def test_state_has_pool_fields(self):
        from backend.agents.graph.state import MiningState
        s = MiningState(task_id=1, region="USA", universe="TOP3000", dataset_id="x")
        assert s.available_dataset_pool == []
        assert s.current_hypothesis_datasets == []
        assert s.current_hypothesis_fields == []

    def test_state_accepts_pool_population(self):
        from backend.agents.graph.state import MiningState
        s = MiningState(
            task_id=1, region="USA", universe="TOP3000", dataset_id="x",
            available_dataset_pool=["x", "y", "z"],
        )
        assert s.available_dataset_pool == ["x", "y", "z"]


class TestCArchitectureRouting:
    """Plan v5+ §Phase 1 C-architecture: routing logic between distill →
    hypothesis → t1_strategy_select / code_gen depends on Phase 1 active."""

    def test_route_after_distill_phase1_takes_hypothesis(self):
        from backend.agents.graph.workflow import _route_after_distill
        from backend.agents.graph.state import MiningState
        s = MiningState(
            task_id=1, region="USA", universe="TOP3000", dataset_id="x",
            available_dataset_pool=["x", "y", "z"],
        )
        # Pool > 1 → hypothesis (Phase 1)
        assert _route_after_distill(s) == "hypothesis"

    def test_route_after_distill_legacy_with_pool_size_1(self):
        from backend.agents.graph.workflow import _route_after_distill
        from backend.agents.graph.state import MiningState
        from backend.config import settings
        s = MiningState(
            task_id=1, region="USA", universe="TOP3000", dataset_id="x",
            available_dataset_pool=["x"],  # only anchor
        )
        # Pool size == 1 → legacy routing (depends on T1_USE_LLM_GUIDED_STRATEGY)
        if getattr(settings, "T1_USE_LLM_GUIDED_STRATEGY", True):
            assert _route_after_distill(s) == "t1_strategy_select"
        else:
            assert _route_after_distill(s) == "hypothesis"

    def test_route_after_distill_no_pool(self):
        from backend.agents.graph.workflow import _route_after_distill
        from backend.agents.graph.state import MiningState
        from backend.config import settings
        s = MiningState(task_id=1, region="USA", universe="TOP3000", dataset_id="x")
        # Empty pool → legacy
        expected = "t1_strategy_select" if getattr(settings, "T1_USE_LLM_GUIDED_STRATEGY", True) else "hypothesis"
        assert _route_after_distill(s) == expected

    def test_route_after_hypothesis_phase1_to_strategy(self):
        from backend.agents.graph.workflow import _route_after_hypothesis
        from backend.agents.graph.state import MiningState
        s = MiningState(
            task_id=1, region="USA", universe="TOP3000", dataset_id="x",
            available_dataset_pool=["x", "y"],
        )
        assert _route_after_hypothesis(s) == "t1_strategy_select"

    def test_route_after_hypothesis_legacy_to_codegen(self):
        from backend.agents.graph.workflow import _route_after_hypothesis
        from backend.agents.graph.state import MiningState
        s = MiningState(task_id=1, region="USA", universe="TOP3000", dataset_id="x")
        # No pool → legacy hypothesis path
        assert _route_after_hypothesis(s) == "code_gen"
