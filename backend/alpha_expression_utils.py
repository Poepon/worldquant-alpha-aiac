"""
Alpha Expression Utilities — structural helpers salvaged from the retired
factor_tier_classifier module, plus a pillar-classifiability check used by KB
upsert validation.

These helpers are intentionally tier-agnostic — they parse expression structure
without assigning a Tier 1/2/3 label. Two consumers depend on them:

1. ``sim_settings.smart_simulation_settings`` — peeks at top-level structure
   to choose neutralization / decay defaults.
2. ``KnowledgeRepository.upsert_pattern`` — refuses SUCCESS_PATTERN rows whose
   expression text the Five-Pillars classifier cannot place anywhere meaningful
   (proxy for "LLM emitted nonsense fields/operators").

Public API:
    derive_control_expression(expression) -> Optional[str]
    is_pillar_classifiable(expression) -> bool

Internal helpers (consumed by ``sim_settings``):
    _strip_outer_parens, _top_level_call, _is_negation_wrapper
"""

from __future__ import annotations

import re
from typing import List, Optional, Set

from backend.alpha_semantic_validator import BUILTIN_GROUPS, OperatorRegistry
from backend.pillar_classifier import infer_pillar


# =============================================================================
# Built-in operator categories (fallback when DB-backed registry unloaded)
# =============================================================================

_BUILTIN_TS_OPS: Set[str] = {
    "ts_rank", "ts_zscore", "ts_mean", "ts_std_dev", "ts_delta", "ts_delay",
    "ts_decay_linear", "ts_arg_max", "ts_arg_min", "ts_quantile", "ts_sum",
    "ts_max", "ts_min", "ts_corr", "ts_count_nans", "ts_av_diff", "ts_skewness",
    "ts_kurtosis", "ts_product", "ts_returns", "ts_scale", "ts_step",
    "ts_median", "ts_co_kurtosis", "ts_co_skewness", "ts_partial_corr",
    "ts_regression", "ts_theilsen", "ts_moment", "ts_decay_exp_window",
    "ts_ir", "ts_max_diff", "ts_min_diff", "ts_av_volatility",
}

_BUILTIN_GROUP_OPS: Set[str] = {
    "group_neutralize", "group_rank", "group_zscore", "group_normalize",
    "group_demean", "group_mean", "group_max", "group_min", "group_sum",
    "group_count", "group_median", "group_std_dev", "group_backfill",
    "group_extra", "group_percentage", "group_scale", "group_vector_neut",
    "group_vector_proj", "group_cartesian_product",
}

_BUILTIN_VEC_OPS: Set[str] = {
    "vec_avg", "vec_max", "vec_min", "vec_sum", "vec_l2_norm", "vec_norm",
    "vec_median", "vec_skewness", "vec_kurtosis", "vec_count", "vec_range",
    "vec_std_dev", "vec_powersum", "vec_choose", "vec_ir",
}

_BUILTIN_PURE_XS_OPS: Set[str] = {
    "rank", "zscore", "normalize", "quantile", "winsorize", "signed_power",
    "scale", "scale_down", "regression_neut", "vector_neut",
}

_T2_SMOOTHING_OPS: Set[str] = {
    "ts_decay_linear", "ts_mean", "ts_std_dev", "ts_max", "ts_min",
    "ts_decay_exp_window", "ts_median",
}

_BUILTIN_FIELDS: Set[str] = {
    "open", "close", "high", "low", "volume", "vwap", "returns", "cap",
    "adv20", "adv60", "adv120", "shares", "amount", "open_interest",
    "days_to_announcement",
}

_DB_FIELD_CACHE: Set[str] = set()


def _ts_ops() -> Set[str]:
    reg = OperatorRegistry.get_instance()
    return reg.ts_operators or _BUILTIN_TS_OPS


def _group_ops() -> Set[str]:
    reg = OperatorRegistry.get_instance()
    return reg.group_operators or _BUILTIN_GROUP_OPS


def _vec_ops() -> Set[str]:
    reg = OperatorRegistry.get_instance()
    return reg.vec_operators or _BUILTIN_VEC_OPS


def populate_known_fields(field_ids: Set[str]) -> None:
    """Populate the field cache so structural helpers recognize DB-backed names."""
    global _DB_FIELD_CACHE
    _DB_FIELD_CACHE = {f.lower() for f in field_ids if f}


def is_known_field(token: str) -> bool:
    """Best-effort field recognition: builtin set ∪ DataField cache."""
    if not token:
        return False
    t = token.strip().lower()
    if not t:
        return False
    if re.fullmatch(r"-?\d+(\.\d+)?", t):
        return False
    if t in {"true", "false", "nan", "inf", "-inf"}:
        return False
    if t in BUILTIN_GROUPS:
        return False
    if t in _BUILTIN_FIELDS:
        return True
    if t in _DB_FIELD_CACHE:
        return True
    if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", t):
        all_ops = _ts_ops() | _group_ops() | _vec_ops() | _BUILTIN_PURE_XS_OPS | {"trade_when"}
        return t not in all_ops
    return False


# =============================================================================
# Expression structure parser (no AST, balanced-paren scan)
# =============================================================================

_FUNC_CALL_RE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")


def _strip_outer_parens(expr: str) -> str:
    """Strip a single layer of fully-enclosing parentheses if present."""
    s = expr.strip()
    while s.startswith("(") and s.endswith(")"):
        depth = 0
        wraps_whole = True
        for i, c in enumerate(s):
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0 and i < len(s) - 1:
                    wraps_whole = False
                    break
        if wraps_whole:
            s = s[1:-1].strip()
        else:
            break
    return s


def _top_level_call(expr: str) -> Optional[tuple]:
    """Parse ``op(arg1, arg2, ...)`` returning (op_name, [arg_str, ...]) or None."""
    s = _strip_outer_parens(expr)
    m = _FUNC_CALL_RE.match(s)
    if not m:
        return None
    op = m.group(1).lower()
    after = s[m.end():]
    depth = 1
    i = 0
    while i < len(after):
        c = after[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0:
        return None
    trailing = after[i + 1:].strip()
    if trailing:
        return None
    inside = after[:i]
    args: List[str] = []
    depth = 0
    last = 0
    for j, c in enumerate(inside):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            args.append(inside[last:j].strip())
            last = j + 1
    args.append(inside[last:].strip())
    args = [a for a in args if a != ""]
    return op, args


def _is_single_field_arg(arg: str) -> bool:
    a = _strip_outer_parens(arg)
    if "(" in a or "+" in a or "-" in a[1:] or "*" in a or "/" in a or "<" in a or ">" in a:
        return False
    return is_known_field(a)


def _is_scalar_or_param(arg: str) -> bool:
    a = arg.strip()
    if not a:
        return False
    if re.fullmatch(r"-?\d+(\.\d+)?", a):
        return True
    if (a.startswith('"') and a.endswith('"')) or (a.startswith("'") and a.endswith("'")):
        return True
    if a.lower() in BUILTIN_GROUPS:
        return True
    if a.lower() in {"true", "false", "nan", "inf"}:
        return True
    if "=" in a:
        return True
    if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", a):
        return True
    return False


def _is_negation_wrapper(expr: str) -> Optional[str]:
    """Recognize sign-flip wrappers — multiply(-1, X) / multiply(X, -1) / subtract(0, X)."""
    parsed = _top_level_call(_strip_outer_parens(expr))
    if not parsed:
        return None
    op, args = parsed
    if op == "multiply" and len(args) == 2:
        a, b = args[0].strip(), args[1].strip()
        if a == "-1":
            return b
        if b == "-1":
            return a
    if op == "subtract" and len(args) == 2 and args[0].strip() == "0":
        return args[1].strip()
    return None


def _is_t1(expr: str) -> bool:
    """Single ts_op over a single known field — used internally by control derivation."""
    parsed = _top_level_call(expr)
    if not parsed:
        return False
    op, args = parsed
    if op not in _ts_ops():
        return False
    if not args:
        return False
    if not _is_single_field_arg(args[0]):
        return False
    for a in args[1:]:
        if not _is_scalar_or_param(a):
            return False
    return True


# =============================================================================
# Public API
# =============================================================================

def derive_control_expression(expression: str) -> Optional[str]:
    """Derive a signal-vs-control "control" expression for an alpha.

    Strips the single-field ts_op core down to its bare field, keeping all
    structural wrappers (cross-sectional / trade_when / negation) intact. The
    caller simulates signal + control and compares Δ(sharpe) to attribute
    whether performance comes from the hypothesis or the structural wrapper.

    Returns None when no clean control can be derived (multi-field arithmetic,
    unknown structure, empty input).

    来源: docs/alphagbm_skills_research_2026-05-15.md P0
    """
    if not expression or not expression.strip():
        return None

    def _control(expr: str) -> Optional[str]:
        s = _strip_outer_parens(expr.strip())
        if not s:
            return None

        neg_inner = _is_negation_wrapper(s)
        if neg_inner is not None:
            sub = _control(neg_inner)
            return None if sub is None else f"multiply(-1, {sub})"

        parsed = _top_level_call(s)
        if not parsed:
            return None
        op, args = parsed

        if op == "trade_when":
            if len(args) < 2:
                return None
            sub = _control(args[1])
            if sub is None:
                return None
            new_args = [args[0], sub] + args[2:]
            return f"trade_when({', '.join(new_args)})"

        if _is_t1(s):
            return _strip_outer_parens(args[0].strip())

        wrapper_ops = _group_ops() | _vec_ops() | _BUILTIN_PURE_XS_OPS | _T2_SMOOTHING_OPS
        if op in wrapper_ops and args:
            sub = _control(args[0])
            if sub is None:
                return None
            new_args = [sub] + args[1:]
            return f"{op}({', '.join(new_args)})"

        return None

    return _control(expression)


def is_pillar_classifiable(expression: str) -> bool:
    """Return True iff infer_pillar maps the expression to a real pillar (not 'other').

    Used by KnowledgeRepository.upsert_pattern to refuse SUCCESS_PATTERN rows
    whose expression text contains no recognizable fields/operators — the
    Five-Pillars classifier returning 'other' is a strong signal the LLM
    emitted nonsense.
    """
    if not expression or not expression.strip():
        return False
    try:
        pillar = infer_pillar(expression=expression)
    except Exception:
        return False
    return bool(pillar) and pillar != "other"
