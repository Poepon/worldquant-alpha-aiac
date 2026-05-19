"""B5 R8-v3 cognitive_layer_service unit tests (Phase 4 Sprint 3 / plan v5 §6.11).

Coverage:
  - YAML loading + 7 layers × required fields
  - select_layer round_robin / bandit / deficit_aware
  - pillar_hint pre-filter
  - bandit Thompson sample favors arms with more passes
  - deficit_aware picks lowest PASS-rate arm
  - render_block contains layer name + few-shot examples
  - estimate_tokens + enforce_token_budget drop order
  - soft-fall on missing/corrupt YAML
"""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from backend.services import cognitive_layer_service as cls


@pytest.fixture(autouse=True)
def _isolate_layer_cache():
    cls.clear_layer_cache()
    yield
    cls.clear_layer_cache()


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------

def test_load_returns_seven_layers():
    layers = cls.load_cognitive_layers()
    assert len(layers) == 7


def test_each_layer_has_required_fields():
    for layer in cls.load_cognitive_layers():
        assert isinstance(layer.layer_id, str) and layer.layer_id
        assert isinstance(layer.name, str) and layer.name
        assert isinstance(layer.prompt, str) and len(layer.prompt) > 100
        assert isinstance(layer.few_shot, list) and len(layer.few_shot) >= 1


def test_all_layer_ids_unique():
    ids = [l.layer_id for l in cls.load_cognitive_layers()]
    assert len(ids) == len(set(ids))


def test_load_yaml_missing_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(cls, "_LAYERS_YAML", tmp_path / "missing.yaml")
    cls.clear_layer_cache()
    assert cls.load_cognitive_layers() == []


def test_load_yaml_corrupt_returns_empty(monkeypatch, tmp_path):
    bad = tmp_path / "corrupt.yaml"
    bad.write_text("- layer_id: x\n  prompt: [unclosed", encoding="utf-8")
    monkeypatch.setattr(cls, "_LAYERS_YAML", bad)
    cls.clear_layer_cache()
    assert cls.load_cognitive_layers() == []


def test_load_yaml_non_list_root_returns_empty(monkeypatch, tmp_path):
    bad = tmp_path / "dict.yaml"
    bad.write_text("layer_id: x\nprompt: y", encoding="utf-8")
    monkeypatch.setattr(cls, "_LAYERS_YAML", bad)
    cls.clear_layer_cache()
    assert cls.load_cognitive_layers() == []


def test_load_yaml_duplicate_ids_kept_first_only(monkeypatch, tmp_path):
    yaml_content = """
- layer_id: dup
  name: First
  prompt: "First prompt text for testing layer loading"
  few_shot: ["expr1"]
- layer_id: dup
  name: Second
  prompt: "Second prompt text"
  few_shot: ["expr2"]
"""
    p = tmp_path / "dup.yaml"
    p.write_text(yaml_content.strip(), encoding="utf-8")
    monkeypatch.setattr(cls, "_LAYERS_YAML", p)
    cls.clear_layer_cache()
    layers = cls.load_cognitive_layers()
    assert len(layers) == 1
    assert layers[0].name == "First"


# ---------------------------------------------------------------------------
# select_layer
# ---------------------------------------------------------------------------

def test_select_round_robin_cycles_through_all_layers():
    layers = cls.load_cognitive_layers()
    n = len(layers)
    seen = set()
    for i in range(n):
        picked = cls.select_layer(
            strategy=cls.SELECT_ROUND_ROBIN, round_index=i,
        )
        assert picked is not None
        seen.add(picked.layer_id)
    assert len(seen) == n


def test_select_round_robin_wraps_around():
    layers = cls.load_cognitive_layers()
    n = len(layers)
    first = cls.select_layer(strategy=cls.SELECT_ROUND_ROBIN, round_index=0)
    after_wrap = cls.select_layer(strategy=cls.SELECT_ROUND_ROBIN, round_index=n)
    assert first.layer_id == after_wrap.layer_id


def test_select_bandit_favors_high_pass_arm():
    """Layer with 50 passes / 5 fails should beat one with 0/0 (uniform)
    in most Thompson samples."""
    rng = random.Random(42)
    layers = cls.load_cognitive_layers()
    target_id = layers[0].layer_id
    stats = {
        target_id: cls.BanditArmStats(layer_id=target_id, pass_count=50, fail_count=5),
    }
    picks = []
    for _ in range(100):
        picked = cls.select_layer(
            strategy=cls.SELECT_BANDIT, stats=stats, rng=rng,
        )
        picks.append(picked.layer_id)
    # Strong-arm should be picked > 50% of the time
    target_share = picks.count(target_id) / len(picks)
    assert target_share > 0.5


def test_select_deficit_aware_picks_lowest_pass_rate():
    layers = cls.load_cognitive_layers()
    high_id = layers[0].layer_id
    low_id = layers[1].layer_id
    stats = {
        high_id: cls.BanditArmStats(layer_id=high_id, pass_count=30, fail_count=5),  # ~85%
        low_id: cls.BanditArmStats(layer_id=low_id, pass_count=3, fail_count=20),    # ~13%
    }
    picked = cls.select_layer(strategy=cls.SELECT_DEFICIT_AWARE, stats=stats)
    assert picked.layer_id == low_id


def test_select_deficit_aware_prefers_unexplored_on_tie():
    """If two arms have same PASS rate (e.g. both 0/0 uniform), prefer
    the one with fewer attempts. With all stats empty, returns the
    first layer (all have 0 attempts → tie broken by iteration order)."""
    picked = cls.select_layer(strategy=cls.SELECT_DEFICIT_AWARE, stats={})
    assert picked is not None


def test_select_unknown_strategy_returns_none():
    picked = cls.select_layer(strategy="garbage", stats={})
    assert picked is None


def test_select_with_pillar_hint_prefilters():
    """When pillar_hint matches a layer's pillar_affinity, that layer
    is preferred (filtered pool)."""
    layers = cls.load_cognitive_layers()
    # value pillar maps to fundamental_value layer
    picked = cls.select_layer(
        strategy=cls.SELECT_ROUND_ROBIN,
        round_index=0,
        pillar_hint="value",
    )
    assert picked is not None
    assert "value" in (picked.pillar_affinity or [])


def test_select_pillar_hint_no_match_falls_back():
    """If no layer matches the pillar hint, fall back to full pool."""
    picked = cls.select_layer(
        strategy=cls.SELECT_ROUND_ROBIN,
        round_index=0,
        pillar_hint="nonexistent_pillar",
    )
    assert picked is not None  # full-pool fallback


# ---------------------------------------------------------------------------
# render_block
# ---------------------------------------------------------------------------

def test_render_block_contains_layer_name_and_few_shot():
    layers = cls.load_cognitive_layers()
    block = cls.build_cognitive_layer_block(layers[0])
    assert "Research Lens" in block
    assert layers[0].name in block
    assert layers[0].few_shot[0] in block


def test_render_block_none_returns_empty():
    assert cls.build_cognitive_layer_block(None) == ""


# ---------------------------------------------------------------------------
# Token budget guard
# ---------------------------------------------------------------------------

def test_estimate_tokens_basic():
    # 4 chars ≈ 1 token
    assert cls.estimate_tokens("") == 0
    assert cls.estimate_tokens("ab") == 1  # ceil to min 1
    assert cls.estimate_tokens("a" * 40) == 10


def test_enforce_token_budget_below_budget_no_drops():
    blocks = {"a": "x" * 200, "b": "x" * 200, "c": "x" * 200}
    before = dict(blocks)
    out = cls.enforce_token_budget(blocks=blocks, budget=10000)
    assert out == before


def test_enforce_token_budget_drops_in_order():
    blocks = {
        "dedup_blacklist": "x" * 16_000,  # ~4000 tokens
        "cross_task_hypotheses": "x" * 16_000,
        "macro_narratives": "x" * 16_000,
        "main": "x" * 8_000,  # ~2000 tokens
    }
    # Budget=4000 means we need to drop 2 blocks
    out = cls.enforce_token_budget(blocks=blocks, budget=4000)
    # dedup_blacklist dropped first
    assert out["dedup_blacklist"] == ""
    # cross_task_hypotheses dropped second
    assert out["cross_task_hypotheses"] == ""
    # main never touched (not in drop_order)
    assert out["main"] == "x" * 8_000


def test_enforce_token_budget_empty_blocks_no_crash():
    out = cls.enforce_token_budget(blocks={}, budget=100)
    assert out == {}


def test_enforce_token_budget_cognitive_layer_block_never_dropped():
    """The whole point of R8-v3 is the layer block — drop order should
    not include it."""
    assert "cognitive_layer_block" not in cls._DROP_ORDER


# ---------------------------------------------------------------------------
# BanditArmStats math
# ---------------------------------------------------------------------------

def test_bandit_arm_stats_uniform_prior():
    arm = cls.BanditArmStats(layer_id="x")
    assert arm.alpha == 1.0
    assert arm.beta == 1.0
    assert arm.expected_pass_rate == 0.5


def test_bandit_arm_stats_after_observations():
    arm = cls.BanditArmStats(layer_id="x", pass_count=8, fail_count=2)
    assert arm.alpha == 9.0  # 1 + 8
    assert arm.beta == 3.0   # 1 + 2
    assert arm.expected_pass_rate == pytest.approx(9.0 / 12.0)
