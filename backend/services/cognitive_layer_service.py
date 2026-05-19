"""B5 R8-v3 cognitive-layer service (Phase 4 Sprint 3 / plan v5 §6.11).

7 cognitive-layer "research lenses" — macro / behavioral / technical /
fundamental / microstructure / cross-sectional / time-series MR. Each
round, ONE layer is selected and its prompt block spliced into the
hypothesis prompt to nudge the LLM into that style of thinking.

Distinct from existing R8 RAG (knowledge-base retrieval) and from B6
hypothesis-forest (cross-task hypothesis reuse): R8-v3 is *prior* —
a research-style prior applied BEFORE retrieval, biasing the LLM's
direction of search.

Three select strategies:
  - **bandit** (Beta-Bernoulli): per-layer α/β state; Thompson sample
    a layer's PASS-probability and pick the max
  - **round_robin**: rotate through layers in fixed order
  - **deficit_aware**: pick the layer with the LOWEST recent PASS rate
    (boost coverage of under-explored lenses)

Token budget guard: a hard 8k-token cap on total hypothesis-prompt
size; when cognitive-layer + context exceeds the budget, drop the
least-essential blocks in this order:
  1. dedup_blacklist (oldest entries)
  2. cross_task_forest (G8 — fewest pass_count first)
  3. macro_narrative (P2-A)
The layer block itself NEVER drops — it's the whole point of R8-v3.

Pure-function module — bandit state is loaded from / persisted to the
``BanditState`` table by the orchestrator (mining_agent), not by this
module directly. Keeps testability + decouples from DB.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)


_LAYERS_YAML = (
    Path(__file__).resolve().parent.parent / "data" / "cognitive_layers.yaml"
)

# Strategy identifiers
SELECT_BANDIT = "bandit"
SELECT_ROUND_ROBIN = "round_robin"
SELECT_DEFICIT_AWARE = "deficit_aware"
_VALID_STRATEGIES = (SELECT_BANDIT, SELECT_ROUND_ROBIN, SELECT_DEFICIT_AWARE)

# Drop order for token-budget guard. Keys correspond to PromptContext
# field names; the orchestrator (build_hypothesis_prompt) consults this
# list when total prompt tokens exceed budget.
_DROP_ORDER: List[str] = [
    "dedup_blacklist",
    "cross_task_hypotheses",
    "macro_narratives",
]

# Default token budget — operator can override via
# COGNITIVE_LAYER_PROMPT_TOKEN_BUDGET setting.
DEFAULT_TOKEN_BUDGET = 8000


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class CognitiveLayer:
    """One research-lens definition loaded from cognitive_layers.yaml."""
    layer_id: str
    name: str
    prompt: str
    few_shot: List[str] = field(default_factory=list)
    pillar_affinity: List[str] = field(default_factory=list)
    research_question: str = ""

    def render_block(self) -> str:
        """Return a markdown block ready to splice into the hypothesis prompt."""
        lines = [
            f"## Research Lens — {self.name}",
            "",
            self.prompt.strip(),
            "",
        ]
        if self.research_question:
            lines.append(f"_Guiding question:_ {self.research_question}")
            lines.append("")
        if self.few_shot:
            lines.append("**Example expressions in this style:**")
            for ex in self.few_shot[:3]:
                lines.append(f"  - `{ex}`")
            lines.append("")
        return "\n".join(lines)


@dataclass
class BanditArmStats:
    """Thompson-sampling state for one layer.

    α = 1 + pass_count (success), β = 1 + fail_count (failure).
    Uniform prior at (α=1, β=1) → no-data → flat 50% expected.
    """
    layer_id: str
    pass_count: int = 0
    fail_count: int = 0

    @property
    def alpha(self) -> float:
        return 1.0 + float(self.pass_count)

    @property
    def beta(self) -> float:
        return 1.0 + float(self.fail_count)

    @property
    def expected_pass_rate(self) -> float:
        a = self.alpha
        b = self.beta
        return a / (a + b)


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_layers_yaml() -> List[CognitiveLayer]:
    """Lazy-load + cache the layer definitions from YAML.

    Soft-fall to [] on missing / corrupt YAML — callers (select_layer,
    render_block) treat [] as "feature disabled this process" and the
    hypothesis prompt renders byte-for-byte legacy.
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        logger.warning(
            "[cognitive_layer] PyYAML missing — R8-v3 disabled until installed"
        )
        return []

    try:
        with _LAYERS_YAML.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning(
            "[cognitive_layer] YAML missing at %s — R8-v3 disabled",
            _LAYERS_YAML,
        )
        return []
    except Exception as e:
        logger.warning(
            "[cognitive_layer] YAML parse failed (%s) — R8-v3 disabled", e
        )
        return []

    if not isinstance(raw, list) or not raw:
        logger.warning(
            "[cognitive_layer] YAML root must be a non-empty list — got %s",
            type(raw).__name__,
        )
        return []

    layers: List[CognitiveLayer] = []
    seen_ids: set = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        layer_id = entry.get("layer_id")
        prompt = entry.get("prompt")
        few_shot = entry.get("few_shot") or []
        if (
            not isinstance(layer_id, str)
            or not isinstance(prompt, str)
            or not isinstance(few_shot, list)
            or layer_id in seen_ids
        ):
            continue
        seen_ids.add(layer_id)
        layers.append(CognitiveLayer(
            layer_id=layer_id,
            name=entry.get("name") or layer_id,
            prompt=prompt,
            few_shot=[str(x) for x in few_shot if isinstance(x, str)],
            pillar_affinity=[
                str(x) for x in (entry.get("pillar_affinity") or [])
                if isinstance(x, str)
            ],
            research_question=str(entry.get("research_question") or ""),
        ))
    return layers


def clear_layer_cache() -> None:
    """Test helper — drops the lru_cache so a monkey-patched YAML path
    takes effect on the next load."""
    _load_layers_yaml.cache_clear()


def load_cognitive_layers() -> List[CognitiveLayer]:
    """Return the cached layer list (loads on first call)."""
    return list(_load_layers_yaml())


# ---------------------------------------------------------------------------
# select_layer — 3 strategies
# ---------------------------------------------------------------------------

def _select_round_robin(
    layers: List[CognitiveLayer],
    round_index: int,
) -> Optional[CognitiveLayer]:
    if not layers:
        return None
    return layers[round_index % len(layers)]


def _select_bandit(
    layers: List[CognitiveLayer],
    stats: Dict[str, BanditArmStats],
    rng: Optional[random.Random] = None,
) -> Optional[CognitiveLayer]:
    """Thompson-sample (Beta) per layer, pick argmax of the drawn samples."""
    if not layers:
        return None
    rng = rng or random.Random()
    best_sample = -1.0
    best_layer: Optional[CognitiveLayer] = None
    for layer in layers:
        arm = stats.get(layer.layer_id) or BanditArmStats(layer.layer_id)
        sample = rng.betavariate(arm.alpha, arm.beta)
        if sample > best_sample:
            best_sample = sample
            best_layer = layer
    return best_layer


def _select_deficit_aware(
    layers: List[CognitiveLayer],
    stats: Dict[str, BanditArmStats],
) -> Optional[CognitiveLayer]:
    """Pick the layer with the LOWEST recent PASS rate — boosts coverage
    of under-explored lenses. Ties broken by lowest pass_count (favors
    truly-unexplored arms over arms that fired many times with low PASS).
    """
    if not layers:
        return None
    best_score = float("inf")
    best_layer: Optional[CognitiveLayer] = None
    best_attempts = float("inf")
    for layer in layers:
        arm = stats.get(layer.layer_id) or BanditArmStats(layer.layer_id)
        rate = arm.expected_pass_rate
        attempts = arm.pass_count + arm.fail_count
        if rate < best_score or (rate == best_score and attempts < best_attempts):
            best_score = rate
            best_attempts = attempts
            best_layer = layer
    return best_layer


def select_layer(
    *,
    strategy: str,
    stats: Optional[Dict[str, BanditArmStats]] = None,
    round_index: int = 0,
    rng: Optional[random.Random] = None,
    pillar_hint: Optional[str] = None,
) -> Optional[CognitiveLayer]:
    """Pick one cognitive layer per the requested strategy.

    Args:
        strategy: "bandit" | "round_robin" | "deficit_aware"
        stats: per-layer pass/fail counts (BanditArmStats); empty dict
            ⇒ all arms have uniform prior. Required for bandit and
            deficit_aware; ignored for round_robin.
        round_index: current round counter (used by round_robin).
        rng: deterministic random.Random for testing.
        pillar_hint: optional pillar to PREFER — when a layer's
            pillar_affinity contains the hint, it's boosted (currently
            implemented by FIRST filtering to pillar-matching layers
            then falling through to the strategy among those; if no
            layers match, fall back to the full list).

    Returns:
        Selected CognitiveLayer, or None if no layers loaded /
        unknown strategy.
    """
    layers = load_cognitive_layers()
    if not layers:
        return None
    if strategy not in _VALID_STRATEGIES:
        logger.warning(f"[cognitive_layer] unknown strategy {strategy!r}")
        return None

    # Pillar-hint prefilter (best-effort)
    pool = layers
    if pillar_hint:
        matched = [
            l for l in layers
            if pillar_hint in (l.pillar_affinity or [])
        ]
        if matched:
            pool = matched

    stats = stats or {}
    if strategy == SELECT_ROUND_ROBIN:
        return _select_round_robin(pool, round_index)
    if strategy == SELECT_BANDIT:
        return _select_bandit(pool, stats, rng=rng)
    if strategy == SELECT_DEFICIT_AWARE:
        return _select_deficit_aware(pool, stats)
    return None  # unreachable


# ---------------------------------------------------------------------------
# Block rendering
# ---------------------------------------------------------------------------

def build_cognitive_layer_block(layer: Optional[CognitiveLayer]) -> str:
    """Return the markdown block for the chosen layer, or "" when None.

    Empty string preserves the byte-for-byte legacy splice contract
    (mirrors P2-A/B/C/D + G8 pattern). Caller (prompts.hypothesis)
    splices this directly into the template.
    """
    if layer is None:
        return ""
    return layer.render_block()


# ---------------------------------------------------------------------------
# Token-budget guard
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Cheap token estimate — 4 chars ≈ 1 token (GPT-family rule of thumb).

    Used by enforce_token_budget; not for billing or precise sizing.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def enforce_token_budget(
    *,
    blocks: Dict[str, str],
    budget: int = DEFAULT_TOKEN_BUDGET,
    drop_order: Optional[Sequence[str]] = None,
) -> Dict[str, str]:
    """Mutate ``blocks`` dict in-place to fit within ``budget`` tokens.

    Drops blocks in ``drop_order`` (default _DROP_ORDER) one at a time
    until the total estimated token count ≤ budget. The cognitive_layer
    block is NEVER dropped — it's the prior R8-v3 was built to inject.

    Args:
        blocks: dict mapping block_name → rendered text. Caller is
            responsible for assembling the full prompt from this dict.
        budget: max total tokens allowed.
        drop_order: blocks to drop in order when over budget.

    Returns:
        The same dict (mutated). Dropped blocks have value "".

    Diagnostics:
        Logs INFO with which blocks were dropped + final token count
        when any drop occurs.
    """
    if not blocks:
        return blocks
    drop_order = list(drop_order or _DROP_ORDER)
    total = sum(estimate_tokens(v) for v in blocks.values())
    if total <= budget:
        return blocks

    dropped: List[str] = []
    for name in drop_order:
        if total <= budget:
            break
        if name not in blocks or not blocks[name]:
            continue
        total -= estimate_tokens(blocks[name])
        blocks[name] = ""
        dropped.append(name)

    if dropped:
        logger.info(
            f"[cognitive_layer] token-budget drops {dropped} "
            f"(final≈{total}/budget={budget})"
        )
    return blocks


__all__ = [
    "CognitiveLayer",
    "BanditArmStats",
    "SELECT_BANDIT",
    "SELECT_ROUND_ROBIN",
    "SELECT_DEFICIT_AWARE",
    "DEFAULT_TOKEN_BUDGET",
    "load_cognitive_layers",
    "clear_layer_cache",
    "select_layer",
    "build_cognitive_layer_block",
    "estimate_tokens",
    "enforce_token_budget",
]
