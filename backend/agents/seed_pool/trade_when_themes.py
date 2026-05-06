"""Plan v5+ §决策 3 — trade_when 主题条件库 loader.

Loads backend/agents/seed_pool/trade_when_themes.yaml into a Python dict and
provides theme→conditions lookup with region-aware field guards and
expected_signal alias resolution.

Usage:
    from backend.agents.seed_pool.trade_when_themes import (
        get_theme_conditions, resolve_signal_to_theme,
    )
    conditions = get_theme_conditions("momentum", region="USA")
    # → list of {"name", "expression", "rationale"} dicts

The library is loaded ONCE at module import time. To reload after editing
the YAML in place, call reload_themes().
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger("agents.trade_when_themes")

_YAML_PATH = Path(__file__).resolve().parent / "trade_when_themes.yaml"
_LOADED: Optional[Dict[str, Any]] = None


def _load() -> Dict[str, Any]:
    global _LOADED
    if _LOADED is not None:
        return _LOADED
    try:
        with _YAML_PATH.open("r", encoding="utf-8") as f:
            _LOADED = yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"[trade_when_themes] failed to load YAML: {e}")
        _LOADED = {"themes": {}, "aliases": {}, "region_field_guards": {}}
    return _LOADED


def reload_themes() -> None:
    """Force reload from disk (test / hot-reload helper)."""
    global _LOADED
    _LOADED = None
    _load()


def resolve_signal_to_theme(expected_signal: Optional[str]) -> str:
    """Map Hypothesis.expected_signal to a canonical theme key.

    Args:
        expected_signal: e.g. "momentum", "volatility_regime", "unknown"

    Returns:
        canonical theme key present in YAML.themes; "default" if no match.
    """
    if not expected_signal:
        return "default"
    sig = expected_signal.lower().strip()
    data = _load()
    themes = data.get("themes", {})
    aliases = data.get("aliases", {})

    if sig in themes:
        return sig
    if sig in aliases:
        target = aliases[sig]
        if target in themes:
            return target
    return "default"


_FIELD_REF_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\b", flags=re.IGNORECASE)


def _has_blocked_field(expression: str, blocked: set) -> bool:
    """Check if any token in the expression matches a blocked field."""
    for token in _FIELD_REF_RE.findall(expression):
        if token in blocked:
            return True
    return False


def get_theme_conditions(
    expected_signal: Optional[str],
    region: str = "USA",
) -> List[Dict[str, str]]:
    """Return condition dicts for the matched theme, region-filtered.

    Args:
        expected_signal: Hypothesis.expected_signal (or None for default)
        region: USA / CHN / EUR / ASI / GLB — drives region_field_guards

    Returns:
        list of {"name", "expression", "rationale"} dicts. Conditions
        referencing region-blocked fields are filtered out.

        If no theme matches AND default has no surviving conditions,
        returns []. Caller should fall back to legacy TRADE_WHEN_TEMPLATES
        in that case.
    """
    data = _load()
    theme_key = resolve_signal_to_theme(expected_signal)
    themes = data.get("themes", {})
    theme = themes.get(theme_key)
    if theme is None:
        return []

    conditions = theme.get("conditions") or []

    # Region-aware field guards
    blocked: set = set()
    guards = data.get("region_field_guards", {}) or {}
    if region in guards:
        blocked = set(guards[region].get("skip_fields") or [])

    if not blocked:
        return list(conditions)

    out = []
    for c in conditions:
        expr = c.get("expression", "")
        if _has_blocked_field(expr, blocked):
            logger.debug(
                f"[trade_when_themes] skip {c.get('name', '?')} for region={region} "
                f"(blocked field in expr={expr[:80]!r})"
            )
            continue
        out.append(c)
    return out


def list_all_themes() -> List[str]:
    """Return sorted list of all theme keys (for diagnostic / validation)."""
    return sorted((_load().get("themes") or {}).keys())


def list_aliases() -> Dict[str, str]:
    """Return alias→theme dict (for diagnostic / validation)."""
    return dict((_load().get("aliases") or {}))


def total_condition_count() -> int:
    """Total conditions across all themes (excluding aliases)."""
    n = 0
    for theme in (_load().get("themes") or {}).values():
        n += len(theme.get("conditions") or [])
    return n
