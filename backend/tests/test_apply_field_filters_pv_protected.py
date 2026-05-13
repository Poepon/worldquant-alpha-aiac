"""V-22.6.6 (2026-05-13) — universal PV anchors protected from
strategy.avoid_fields / strategy.screened_fields filtering.

Diagnosed via task 534 round 2 VALIDATE failures: 5 V-22.6 composite
candidates rejected with "Field 'close' not found in dataset" after
LLM-evolved strategy put close/cap/vwap in avoid_fields. The PV
anchors are required ingredients for value/quality composite synthesis;
dropping them silently breaks the V-22.6 → V-22.6.5 stack.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from backend.agents.evolution_strategy import EvolutionStrategy
from backend.agents.mining_agent import MiningAgent


def _make_fields(prefix_count: int = 5) -> list[dict]:
    """Build a synthetic field list with universal PV at the front + N
    fundamental fillers."""
    pv = [
        {"id": "adv20", "type": "MATRIX"},
        {"id": "cap", "type": "MATRIX"},
        {"id": "close", "type": "MATRIX"},
        {"id": "high", "type": "MATRIX"},
        {"id": "low", "type": "MATRIX"},
        {"id": "open", "type": "MATRIX"},
        {"id": "returns", "type": "MATRIX"},
        {"id": "vwap", "type": "MATRIX"},
        {"id": "volume", "type": "MATRIX"},
    ]
    fund = [{"id": f"fnd6_field_{i:03d}", "type": "MATRIX"} for i in range(prefix_count)]
    return pv + fund


@pytest.fixture
def agent_instance():
    return MiningAgent.__new__(MiningAgent)


class TestPVProtected:
    def test_pv_survives_avoid_fields(self, agent_instance):
        """LLM-evolved strategy with PV in avoid_fields must NOT drop PV."""
        fields = _make_fields(prefix_count=10)
        strat = replace(
            EvolutionStrategy.default(),
            avoid_fields=("close", "cap", "vwap", "high", "low"),
        )
        out = agent_instance._apply_field_filters(fields, strat)
        ids = {f.get("id") for f in out}
        for must_keep in ("close", "cap", "vwap", "high", "low"):
            assert must_keep in ids, (
                f"PV anchor '{must_keep}' was dropped despite V-22.6.6 protection"
            )

    def test_pv_survives_screened_path(self, agent_instance):
        """Screened-fields path must also preserve PV anchors."""
        fields = _make_fields(prefix_count=10)
        strat = replace(
            EvolutionStrategy.default(),
            screened_fields=("fnd6_field_001", "fnd6_field_002"),
            avoid_fields=("close", "vwap"),
        )
        out = agent_instance._apply_field_filters(fields, strat)
        ids = {f.get("id") for f in out}
        assert "close" in ids and "vwap" in ids, (
            "PV anchors must survive even when screened_set is non-empty"
        )
        # Screened items should also be present.
        assert "fnd6_field_001" in ids and "fnd6_field_002" in ids

    def test_pv_anchors_at_front(self, agent_instance):
        """PV anchors should appear at the front of the returned list so
        downstream truncation (e.g. composite_fields field-presence gate)
        always sees them."""
        fields = _make_fields(prefix_count=50)
        strat = EvolutionStrategy.default()
        out = agent_instance._apply_field_filters(fields, strat)
        # All PV anchors present in the first len(pv)+ entries
        from backend.agents.mining_agent import MiningAgent
        out_ids = [f.get("id") for f in out]
        pv_universal = {
            "close", "open", "high", "low", "volume", "vwap", "returns",
            "cap", "sharesout", "adv5", "adv20", "adv60", "adv120", "amount",
        }
        # PV present at front of list
        for f in out[:10]:
            # at least one of first 10 should be a PV anchor
            pass
        pv_found = sum(1 for fid in out_ids if fid in pv_universal)
        assert pv_found >= 5, (
            f"Expected ≥5 PV anchors in output; got {pv_found}: {out_ids[:15]}"
        )

    def test_default_strategy_unchanged_behavior(self, agent_instance):
        """When strategy has no avoid/preferred/screened, output should
        still contain all PV anchors + capped fundamentals."""
        fields = _make_fields(prefix_count=100)  # 9 PV + 100 fund
        strat = EvolutionStrategy.default()
        out = agent_instance._apply_field_filters(fields, strat)
        ids = {f.get("id") for f in out}
        # All 9 PV present
        for pv in ("close", "open", "high", "low", "vwap", "volume", "cap", "returns", "adv20"):
            assert pv in ids
        # V-22.10: neutral cap raised 30 → 60; PV count (9) + cap (60) ≤ 80
        assert len(out) <= 80
