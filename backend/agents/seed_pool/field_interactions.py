"""Plan v5+ #2 — Field interaction graph generator.

Two-pass: (1) classify field_id → semantic role via regex patterns;
(2) enumerate role-pair templates that produce Quasi-T1-like
expressions. Avoids O(N²) LLM call across thousands of fields by
encoding the financial knowledge as role × role → template rules.

Yields per-region "Quasi-T1+" candidates that extend the static 15-pattern
white-list in factor_tier_classifier._QUASI_T1_PATTERNS:
  - synthetic_returns / intraday_range / pe / pb / ev_ebit / accruals /
    debt_to_equity / overnight_gap / etc.
The classifier's `_is_quasi_t1` runs each emitted expression through the
same canonical AST check, so generated expressions either pass or are
filtered out.

Usage at expand-time:
    from backend.agents.seed_pool.field_interactions import (
        generate_pair_candidates,
    )
    candidates = generate_pair_candidates(
        available_fields=["close", "high", "low", "volume", ...],
        region="USA",
    )
    # → List[Dict] each {expression, role_pair, template_id, operator}
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

logger = logging.getLogger("agents.field_interactions")

_YAML_PATH = Path(__file__).resolve().parent / "field_interactions.yaml"
_LOADED: Optional[Dict[str, Any]] = None


def _load() -> Dict[str, Any]:
    global _LOADED
    if _LOADED is not None:
        return _LOADED
    try:
        with _YAML_PATH.open("r", encoding="utf-8") as f:
            _LOADED = yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"[field_interactions] failed to load YAML: {e}")
        _LOADED = {"roles": {}, "interactions": []}
    return _LOADED


def reload() -> None:
    global _LOADED
    _LOADED = None
    _load()


def classify_field_role(field_id: str) -> Optional[str]:
    """Classify a field_id to its semantic role via YAML role.patterns.
    Returns the FIRST role whose pattern matches; None if no match."""
    if not field_id:
        return None
    fid = field_id.lower().strip()
    data = _load()
    for role_name, role_def in (data.get("roles") or {}).items():
        for pat in role_def.get("patterns") or []:
            try:
                if re.match(pat, fid, flags=re.IGNORECASE):
                    return role_name
            except re.error:
                continue
    return None


def _index_fields_by_role(available_fields: List[str]) -> Dict[str, List[str]]:
    """Group available field_ids by their classified role.

    Returns: {role_name: [field_id, ...]}. Fields with no matching role
    are silently dropped (they're not in our interaction templates).
    """
    by_role: Dict[str, List[str]] = {}
    for fid in available_fields:
        role = classify_field_role(fid)
        if role is None:
            continue
        by_role.setdefault(role, []).append(fid)
    return by_role


def generate_pair_candidates(
    available_fields: List[str],
    region: str = "USA",
    max_per_template: int = 1,
) -> List[Dict[str, Any]]:
    """Generate Quasi-T1+ candidates from the field interaction graph.

    Args:
        available_fields: list of field_ids known to exist for this
            region/dataset (caller should pre-filter to active fields).
        region: BRAIN region — passed for downstream validator gating;
            not used internally for filtering yet (templates are region-
            agnostic; field availability does the filtering).
        max_per_template: cap on candidates emitted per template id.
            Defaults to 1 (one canonical instance per template).

    Returns:
        list of dicts: {expression, template_id, role_pair, operator}.
        Caller should run them through factor_tier_classifier._is_quasi_t1
        + _dedup_and_validate before adding to mining round.
    """
    data = _load()
    roles_idx = _index_fields_by_role(available_fields)
    interactions = data.get("interactions") or []

    candidates: List[Dict[str, Any]] = []
    field_set = set(f.lower() for f in available_fields)

    for tpl in interactions:
        tpl_id = tpl.get("id", "?")
        role_pair = tpl.get("role_pair") or []
        if len(role_pair) != 2:
            continue
        r1, r2 = role_pair[0], role_pair[1]

        # Skip if either role has no fields available
        if r1 not in roles_idx or r2 not in roles_idx:
            continue

        # Templates that reference extra fields beyond {f1}/{f2} need
        # those fields available too. Specifically: intraday_range_relative
        # uses literal "close"; close_in_range uses "low"; overnight_gap
        # uses ts_delay({f2}). We sniff for hardcoded field references.
        expr_template = tpl.get("expression", "")
        # Find hardcoded field references (not {f1}/{f2}/{ts_delay_*})
        hardcoded_refs = _find_hardcoded_field_refs(expr_template)
        if hardcoded_refs and not all(ref in field_set for ref in hardcoded_refs):
            logger.debug(
                f"[field_interactions] skip template {tpl_id} for region={region} "
                f"(missing hardcoded fields: {hardcoded_refs - field_set})"
            )
            continue

        # Pick first available field per role (deterministic; could be
        # randomized later, but determinism aids reproducibility now)
        emitted = 0
        for f1 in roles_idx[r1][:max_per_template]:
            for f2 in roles_idx[r2][:max_per_template]:
                if f1 == f2 and r1 == r2:
                    # Same field for both slots only OK for "self-referential"
                    # templates like synthetic_returns that explicitly use
                    # ts_delay(f1, 1) on f1 again — those have role_pair=[X,X]
                    # by design; we let it through.
                    pass
                expr = expr_template.format(f1=f1, f2=f2)
                candidates.append({
                    "expression": expr,
                    "template_id": tpl_id,
                    "role_pair": [r1, r2],
                    "operator": tpl.get("operator", "?"),
                    "rationale": tpl.get("rationale", ""),
                    "field_pair": [f1, f2],
                })
                emitted += 1
                if emitted >= max_per_template:
                    break
            if emitted >= max_per_template:
                break

    return candidates


def _find_hardcoded_field_refs(expression_template: str) -> Set[str]:
    """Extract hardcoded field tokens from a template (not {f1}/{f2}).

    Tokens are word-like identifiers that are NOT operators, NOT placeholders.
    Used to verify region availability of templates that reference specific
    fields like close / low / etc.
    """
    # Strip placeholder braces so {f1} / {f2} aren't matched
    stripped = re.sub(r"\{[^}]+\}", "", expression_template)
    # Operator names that should not be flagged as fields
    operators = {
        "add", "subtract", "multiply", "divide", "signed_power", "abs",
        "min", "max", "sign",
        "rank", "zscore", "normalize", "quantile", "winsorize", "scale",
        "ts_rank", "ts_zscore", "ts_mean", "ts_std_dev", "ts_delta",
        "ts_delay", "ts_sum", "ts_corr", "ts_decay_linear", "ts_arg_max",
        "ts_arg_min", "ts_av_diff", "ts_count_nans", "ts_product",
        "ts_scale", "ts_step", "ts_regression", "ts_covariance",
        "ts_backfill", "ts_max", "ts_min", "ts_quantile",
        "group_neutralize", "group_rank", "group_zscore",
        "group_mean", "group_scale",
        "trade_when", "if_else", "less", "greater", "equal",
    }
    refs = set()
    for m in re.finditer(r"\b([a-z_][a-z0-9_]*)\b", stripped, flags=re.IGNORECASE):
        token = m.group(1).lower()
        if token in operators:
            continue
        if token.isdigit():
            continue
        if re.match(r"^\d+$", token):
            continue
        refs.add(token)
    return refs


def list_all_roles() -> List[str]:
    return sorted((_load().get("roles") or {}).keys())


def list_all_templates() -> List[str]:
    return [tpl.get("id", "?") for tpl in (_load().get("interactions") or [])]


def template_count() -> int:
    return len(_load().get("interactions") or [])
