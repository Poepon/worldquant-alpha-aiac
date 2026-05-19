"""Phase 4 Sprint 1 A1.3 — assistant-mode template library + composer.

Plan: docs/phase4_a_b_plan_v5_2026-05-19.md §6.1 (A1.3)

Pure-function module — no DB, no LLM, no async. Load templates from
`backend/data/assistant_mode_templates.yaml` once at module import and
expose two functions::

    match_template(hypothesis_text, *, pillar=None, top_k=1)
        → list of (template_id, score) — best matches by keyword overlap.

    compose_expression(template, *, slot_overrides=None)
        → BRAIN DSL string with {{slot}} placeholders filled from
          (slot_overrides | template.slots.<slot>.default).

Design rationale
----------------
1. **Keyword-overlap scoring** beats embedding-similarity for A1.3
   because the corpus is small (10 templates), interpretability matters
   for debugging operator decisions, and we don't want to introduce a
   new embedding-model dependency.

2. **Slot defaults baked into YAML** make the composer total — no
   undefined-slot KeyError ever; worst case it emits a template's
   default for a slot the LLM didn't fill.

3. **No fuzzy operator inference** at A1.3 — caller (node_code_gen) is
   expected to pass `pillar` if it knows; if not, we fall through all
   pillars and pick best overlap.

4. **Soft-fail loading**: yaml file missing / corrupt → `_load_templates`
   returns an empty list; callers degrade to "no template matched" path.
   This lets the assistant-mode branch silently fall through to author
   mode when the template library isn't shipped (defense in depth).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("services.assistant_template")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEMPLATES_YAML = (
    Path(__file__).resolve().parent.parent / "data" / "assistant_mode_templates.yaml"
)

# Min Jaccard-like overlap to consider a template "matched" — below this
# we treat the LLM's hypothesis text as unrelated and the caller should
# fall through (or pick a pillar-default template).
_MIN_SCORE = 0.05


# Module-level cache of loaded templates. Lazy-loaded on first
# match_template call; reload via clear_template_cache() (testing).
_TEMPLATE_CACHE: Optional[List[Dict[str, Any]]] = None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _load_templates() -> List[Dict[str, Any]]:
    """Read + normalize the YAML. Soft-fails on missing file / parse error."""
    if not _TEMPLATES_YAML.exists():
        logger.warning(
            "[assistant_template] templates file missing: %s — "
            "assistant mode will fall through to author",
            _TEMPLATES_YAML,
        )
        return []
    try:
        import yaml  # local import — yaml is optional at framework level
        raw = yaml.safe_load(_TEMPLATES_YAML.read_text(encoding="utf-8"))
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[assistant_template] failed to parse %s: %s — falling back to []",
            _TEMPLATES_YAML, ex,
        )
        return []
    if not isinstance(raw, list):
        logger.warning(
            "[assistant_template] YAML root must be a list, got %s — falling back to []",
            type(raw).__name__,
        )
        return []

    templates: List[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        tid = entry.get("template_id")
        skeleton = entry.get("expression_skeleton")
        if not isinstance(tid, str) or not isinstance(skeleton, str):
            continue
        templates.append({
            "template_id": tid,
            "pillar": entry.get("pillar", "other"),
            "hypothesis_keywords": list(entry.get("hypothesis_keywords") or []),
            "expression_skeleton": skeleton,
            "slots": dict(entry.get("slots") or {}),
            "recommended_operators": list(entry.get("recommended_operators") or []),
            "description": entry.get("description", ""),
        })
    if not templates:
        logger.warning(
            "[assistant_template] no valid templates loaded from %s", _TEMPLATES_YAML,
        )
    return templates


def get_templates(*, force_reload: bool = False) -> List[Dict[str, Any]]:
    """Return the cached template list. ``force_reload`` re-reads YAML
    (used by tests to swap the file)."""
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is None or force_reload:
        _TEMPLATE_CACHE = _load_templates()
    return list(_TEMPLATE_CACHE)


def clear_template_cache() -> None:
    """Test hook — reset the module cache so a fresh load picks up YAML
    edits done mid-test."""
    global _TEMPLATE_CACHE
    _TEMPLATE_CACHE = None


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _tokenize(text: str) -> set:
    """Lowercase identifier-style tokens. Phrases (multi-word keywords)
    contribute each constituent token — keeps Jaccard semantics simple.
    """
    if not text:
        return set()
    return {tok.lower() for tok in _WORD_RE.findall(text)}


def _score(hyp_tokens: set, template_keywords: List[str]) -> float:
    """Average Jaccard-similarity between hypothesis tokens and each
    keyword phrase's tokens. Higher = better match. Range [0, 1]."""
    if not hyp_tokens or not template_keywords:
        return 0.0
    scores: List[float] = []
    for kw in template_keywords:
        kw_tokens = _tokenize(kw)
        if not kw_tokens:
            continue
        inter = len(hyp_tokens & kw_tokens)
        union = len(hyp_tokens | kw_tokens)
        if union == 0:
            continue
        scores.append(inter / union)
    if not scores:
        return 0.0
    # Average across keyword phrases — a template with multiple matching
    # keywords scores higher than one with a single weak match.
    return sum(scores) / len(scores)


def match_template(
    hypothesis_text: str,
    *,
    pillar: Optional[str] = None,
    top_k: int = 1,
    min_score: float = _MIN_SCORE,
) -> List[Tuple[Dict[str, Any], float]]:
    """Return up to ``top_k`` (template_dict, score) tuples sorted by
    descending score.

    Args:
      hypothesis_text: LLM-emitted hypothesis statement (English).
      pillar: when provided, restrict candidates to this pillar (case-
              insensitive). None → consider all templates.
      top_k: how many matches to return.
      min_score: prune results below this Jaccard threshold.

    Returns empty list when no template clears ``min_score`` — caller
    should fall through to author mode.
    """
    templates = get_templates()
    hyp_tokens = _tokenize(hypothesis_text)

    candidates = templates
    if pillar is not None:
        plow = pillar.lower()
        candidates = [t for t in templates if str(t.get("pillar", "")).lower() == plow]

    scored: List[Tuple[Dict[str, Any], float]] = []
    for t in candidates:
        s = _score(hyp_tokens, t.get("hypothesis_keywords", []))
        if s >= min_score:
            scored.append((t, s))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[: max(1, top_k)]


# ---------------------------------------------------------------------------
# Composing
# ---------------------------------------------------------------------------


_SLOT_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def compose_expression(
    template: Dict[str, Any],
    *,
    slot_overrides: Optional[Dict[str, Any]] = None,
) -> str:
    """Fill the template's ``expression_skeleton`` placeholders with
    values from ``slot_overrides`` (if present) else slot defaults.

    Args:
      template: a template dict from get_templates() / match_template().
      slot_overrides: ``{slot_name: value}`` map. Missing entries fall
                      back to ``template["slots"][slot].default``;
                      missing-AND-no-default slots stay placeholder
                      (caller can detect via "{{" in returned string).

    Returns the composed BRAIN DSL string.

    Raises ValueError ONLY when template dict is malformed (missing
    expression_skeleton). Slot-fill failures soft-fail to leave the
    placeholder visible — caller can branch on `"{{" in result`.
    """
    skeleton = template.get("expression_skeleton")
    if not isinstance(skeleton, str):
        raise ValueError(
            f"template {template.get('template_id')!r} missing expression_skeleton"
        )

    slot_defs = template.get("slots") or {}
    overrides = slot_overrides or {}

    def _resolve(match: re.Match) -> str:
        name = match.group(1)
        # 1. explicit override
        if name in overrides:
            return str(overrides[name])
        # 2. YAML default
        slot_def = slot_defs.get(name)
        if isinstance(slot_def, dict) and "default" in slot_def:
            return str(slot_def["default"])
        # 3. leave placeholder visible — caller can detect + fall through
        logger.debug(
            "[assistant_template] slot %r has no override or default for "
            "template_id=%s", name, template.get("template_id"),
        )
        return match.group(0)

    return _SLOT_RE.sub(_resolve, skeleton)


def compose_for_hypothesis(
    hypothesis_text: str,
    *,
    pillar: Optional[str] = None,
    slot_overrides: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Convenience composer used by node_code_gen — find best template,
    return ``{template_id, pillar, expression, score, description}`` or
    None if nothing matched above min_score (caller falls through).

    Soft-fail on any composition error → returns None (don't break
    code_gen on a single template-library glitch).
    """
    try:
        matches = match_template(hypothesis_text, pillar=pillar, top_k=1)
        if not matches:
            return None
        template, score = matches[0]
        expr = compose_expression(template, slot_overrides=slot_overrides)
        if "{{" in expr:
            # Composition left placeholders — refuse to emit a broken DSL
            logger.warning(
                "[assistant_template] composed expression still has "
                "placeholders for template_id=%s — refusing to emit: %s",
                template.get("template_id"), expr,
            )
            return None
        return {
            "template_id": template["template_id"],
            "pillar": template.get("pillar"),
            "expression": expr,
            "score": score,
            "description": template.get("description", ""),
        }
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[assistant_template] compose_for_hypothesis soft-fail: %s", ex,
        )
        return None


__all__ = [
    "get_templates",
    "clear_template_cache",
    "match_template",
    "compose_expression",
    "compose_for_hypothesis",
]
