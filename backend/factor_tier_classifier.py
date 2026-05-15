"""
Factor Tier Classifier — assigns each alpha expression to T1 / T2 / T3 / None.

Tier definitions (mirrors plan §"Tier 定义"):
- T1: 仅时序维度 — single ts_* operator over a single known field
       e.g. ts_rank(close, 20), ts_zscore(returns, 5), ts_decay_linear(field, 10)
- T2: 横截面 wrapper / smoothing wrapper applied to a T1 signal
       e.g. group_neutralize(ts_rank(close, 20), industry),
            rank(ts_zscore(returns, 5)),
            ts_decay_linear(ts_rank(field, 5), 10)  # nested ts is treated as smoothing wrapper
- T3: trade_when(... , <T2 expr>, ...) entry-filter wrapper
- None: multi-field arithmetic, single-layer cross-sectional on raw field, unknown form

The classifier is purely structural — it does NOT consult metrics or BRAIN. Backfill
applies it once per alpha; runtime uses it to gate KB upserts and validate wrapper output.

Design principles:
- No AST. Use regex + balanced-paren scanner. Operator categories come from
  alpha_semantic_validator.OperatorRegistry (loaded from DB).
- For unloaded OperatorRegistry (test contexts, cold start), fall back to a
  curated built-in set covering the operators referenced by the plan.
- Field validation is best-effort: a token that's not a known operator and not a
  literal is treated as a field. is_known_field() can optionally consult DataField.

Public API:
    classify_tier(expression) -> Optional[int]
    is_t1_expression(expression) -> bool
    is_known_field(token) -> bool
    extract_tier1_seed(expression) -> Optional[str]
    _dedup_and_validate(variants, target_tier, region) -> List[Dict]
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Set

from loguru import logger

from backend.alpha_semantic_validator import (
    AlphaSemanticValidator,
    BUILTIN_GROUPS,
    OperatorRegistry,
    compute_expression_hash,
)


# =============================================================================
# Built-in operator categories (fallback when DB-backed registry unloaded)
# =============================================================================
# These cover the ops referenced by plan §"Tier 定义" / §"T2 算子目录" / §"trade_when 模板".
# When OperatorRegistry is loaded from DB, that data takes precedence.

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

# Pure cross-sectional ops (single-arg or arg + scalar param)
_BUILTIN_PURE_XS_OPS: Set[str] = {
    "rank", "zscore", "normalize", "quantile", "winsorize", "signed_power",
    "scale", "scale_down", "regression_neut", "vector_neut",
}

# Smoothing ts ops that are valid as T2 wrappers around a T1 signal
_T2_SMOOTHING_OPS: Set[str] = {
    "ts_decay_linear", "ts_mean", "ts_std_dev", "ts_max", "ts_min",
    "ts_decay_exp_window", "ts_median",
}


# =============================================================================
# Quasi-T1 whitelist — Plan v5+ §"Quasi-T1 准一阶白名单 v1.0"
# =============================================================================
# Quasi-T1 captures finance-classical two-field arithmetic constructs that
# strict T1 (single ts_op over one field) excludes. The whitelist is explicit,
# size-bounded (≤15), and matched via mini-AST not string canonicalization
# (per Plan v5+ §V-7 — robust to whitespace/ordering surface variants).
#
# Pattern grammar (recursive tuple form):
#   ("op_name", [arg, ...])  where arg is one of:
#     - str literal of a field name → matched against is_known_field()
#     - str "<int>"                  → any integer literal
#     - str "<num>"                  → any numeric literal (int or float)
#     - str "1" / "0" / etc.         → exact numeric match
#     - nested tuple                 → recursive pattern
#
# Constraints (enforced by _is_quasi_t1):
#   - No statistical ts_op (ts_mean/std/rank/zscore/corr/arg_max/...) — listed
#     in _QUASI_T1_FORBIDDEN_OPS
#   - Only allowed ops: add/subtract/multiply/divide/signed_power + ts_delay
#     (ts_delay is pointer-type, not statistical aggregation)
#   - No group_op / rank / zscore / quantile / scale / trade_when
#
# Lifecycle (Plan v5+ §V-4): whitelist starts at 15 v1.0 entries; monthly
# scripts/quasi_t1_candidates_audit.py mines alpha table for new patterns.

_QUASI_T1_ALLOWED_OPS: Set[str] = {
    "add", "subtract", "multiply", "divide", "signed_power", "ts_delay",
}

_QUASI_T1_FORBIDDEN_OPS: Set[str] = {
    # statistical ts_op (any aggregation over a window)
    "ts_mean", "ts_std_dev", "ts_rank", "ts_zscore", "ts_corr", "ts_arg_max",
    "ts_arg_min", "ts_quantile", "ts_sum", "ts_decay_linear", "ts_av_diff",
    "ts_count_nans", "ts_product", "ts_scale", "ts_step", "ts_regression",
    "ts_covariance", "ts_backfill", "ts_max", "ts_min", "ts_delta",
    "ts_skewness", "ts_kurtosis", "ts_median", "ts_returns",
    # group ops
    "group_neutralize", "group_rank", "group_zscore", "group_mean",
    "group_scale", "group_normalize", "group_demean",
    # cross-sectional ops
    "rank", "zscore", "normalize", "quantile", "winsorize", "scale",
    "regression_neut", "vector_neut",
    # event ops
    "trade_when",
}

# v1.0 — 15 finance-classical patterns (Plan §"Quasi-T1 准一阶白名单 v1.0").
_QUASI_T1_PATTERNS: tuple = (
    # Q-PR-01: synthetic returns — subtract(divide(close, ts_delay(close, d)), 1)
    ("subtract", [("divide", ["close", ("ts_delay", ["close", "<int>"])]), "1"]),
    # Q-ID-01: intraday range / close
    ("divide", [("subtract", ["high", "low"]), "close"]),
    # Q-ID-02: close position within range
    ("divide", [("subtract", ["close", "low"]), ("subtract", ["high", "low"])]),
    # Q-ID-03: intraday close-open return
    ("divide", [("subtract", ["close", "open"]), "open"]),
    # Q-VL-01: PE proxy
    ("divide", ["close", "eps"]),
    # Q-VL-02: PB proxy
    ("divide", ["close", "book_value_per_share"]),
    # Q-VL-03: earnings yield (EBIT/EV)
    ("divide", ["ebit", "ev"]),
    # Q-PV-01: liquidity ratio
    ("divide", ["close", "volume"]),
    # Q-PV-02: turnover proxy
    ("divide", ["amount", "cap"]),
    # Q-FN-01: accrual quality
    ("divide", ["cfo", "net_income"]),
    # Q-FN-02: cash flow yield
    ("divide", ["cfo", "cap"]),
    # Q-FN-03: asset turnover
    ("divide", ["sales", "total_assets"]),
    # Q-FN-04: debt-to-equity
    ("divide", ["total_debt", "total_equity"]),
    # Q-GP-01: overnight gap
    (
        "divide",
        [
            ("subtract", ["open", ("ts_delay", ["close", "<int>"])]),
            ("ts_delay", ["close", "<int>"]),
        ],
    ),
    # Q-CR-01: close-vwap deviation
    ("subtract", ["close", "vwap"]),

    # Plan v5+ #2 (2026-05-07) — STRUCTURAL patterns matching field_interactions.yaml
    # templates. Each accepts ANY pair of bare-identifier fields (via <field>
    # wildcard). Together with classify_field_role, these expand the white-list
    # from 15 → effectively unlimited (constrained by financial-meaningful
    # role pairs in YAML), without requiring per-region pattern duplication.
    #
    # SAFETY: <field> only matches bare identifiers (not numerics, not nested
    # calls), so structural patterns only catch "field × field" combinations.
    # Financially meaningless combinations (e.g. divide(close, returns)) will
    # type-check as T1 but rarely PASS quality eval — same as today's behavior
    # for hand-typed expressions.

    # Generic ratio / spread / product (covers PE / PB / EV-EBIT / accruals /
    # debt-to-equity / dollar-volume / etc. once role-classified)
    ("divide", ["<field>", "<field>"]),
    ("subtract", ["<field>", "<field>"]),
    ("multiply", ["<field>", "<field>"]),

    # 3-leg structures used by intraday_range / close_in_range / overnight_gap
    ("divide", [("subtract", ["<field>", "<field>"]), "<field>"]),
    ("divide", [("subtract", ["<field>", "<field>"]), ("subtract", ["<field>", "<field>"])]),

    # Synthetic returns / leading-vs-lagging
    ("subtract", [("divide", ["<field>", ("ts_delay", ["<field>", "<int>"])]), "<num>"]),
    ("divide", [("subtract", ["<field>", ("ts_delay", ["<field>", "<int>"])]), ("ts_delay", ["<field>", "<int>"])]),
)


def _ts_ops() -> Set[str]:
    reg = OperatorRegistry.get_instance()
    return reg.ts_operators or _BUILTIN_TS_OPS


def _group_ops() -> Set[str]:
    reg = OperatorRegistry.get_instance()
    return reg.group_operators or _BUILTIN_GROUP_OPS


def _vec_ops() -> Set[str]:
    reg = OperatorRegistry.get_instance()
    return reg.vec_operators or _BUILTIN_VEC_OPS


# =============================================================================
# Built-in known fields (when DataField table is unavailable)
# =============================================================================

_BUILTIN_FIELDS: Set[str] = {
    "open", "close", "high", "low", "volume", "vwap", "returns", "cap",
    "adv20", "adv60", "adv120", "shares", "amount", "open_interest",
    "days_to_announcement",
}

# Known field cache populated lazily from DB; set externally via populate_known_fields()
_DB_FIELD_CACHE: Set[str] = set()


def populate_known_fields(field_ids: Set[str]) -> None:
    """Populate the field cache so classify_tier can recognize DB-backed field names.

    Called by sync_datasets task or AlphaSemanticValidator init. Without this
    the classifier falls back to _BUILTIN_FIELDS only — adequate for unit tests
    but causes T1 mis-classification on real expressions referencing fundamental
    fields (fnd6_..., return_equity, etc.).
    """
    global _DB_FIELD_CACHE
    _DB_FIELD_CACHE = {f.lower() for f in field_ids if f}


def is_known_field(token: str) -> bool:
    """Best-effort field recognition: builtin set ∪ DataField cache ∪ BUILTIN_GROUPS.

    Numeric literals and keywords return False.
    """
    if not token:
        return False
    t = token.strip().lower()
    if not t:
        return False
    # Numeric literals
    if re.fullmatch(r"-?\d+(\.\d+)?", t):
        return False
    if t in {"true", "false", "nan", "inf", "-inf"}:
        return False
    # Group built-ins are not "fields" in the T1 sense
    if t in BUILTIN_GROUPS:
        return False
    if t in _BUILTIN_FIELDS:
        return True
    if t in _DB_FIELD_CACHE:
        return True
    # Heuristic: identifier with no operator-like pattern; treat as field if
    # it's not a known operator. This is intentionally permissive — the
    # expression came from BRAIN simulator so unknown identifiers are usually
    # fields we haven't synced yet.
    if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", t):
        all_ops = _ts_ops() | _group_ops() | _vec_ops() | _BUILTIN_PURE_XS_OPS | {"trade_when"}
        return t not in all_ops
    return False


# =============================================================================
# Expression structure parser (no AST, just balanced-paren scan)
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
    """Parse `op(arg1, arg2, ...)` where op is the top-level operator.

    Returns (op_name, [arg_str, ...]) or None if expr is not a single function call.
    Args are returned as raw strings (whitespace-stripped) — recurse to classify them.
    """
    s = _strip_outer_parens(expr)
    m = _FUNC_CALL_RE.match(s)
    if not m:
        return None
    op = m.group(1).lower()
    after = s[m.end():]  # everything after the opening paren
    # Find matching closing paren
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
        return None  # unbalanced
    # Verify nothing meaningful trails the closing paren
    trailing = after[i + 1:].strip()
    if trailing:
        return None  # not a single top-level call (e.g. f(x) + g(y))
    inside = after[:i]
    # Split args at depth-0 commas
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
    # Strip empty trailing arg from cases like f(x,)
    args = [a for a in args if a != ""]
    return op, args


def _is_single_field_arg(arg: str) -> bool:
    """True if arg is a single field token (not a function call, not arithmetic)."""
    a = _strip_outer_parens(arg)
    # Reject if it contains operators / arithmetic / function calls
    if "(" in a or "+" in a or "-" in a[1:] or "*" in a or "/" in a or "<" in a or ">" in a:
        # Note: a[1:] handles leading minus on numeric literals; we don't expect those as fields
        return False
    return is_known_field(a)


def _is_scalar_or_param(arg: str) -> bool:
    """True if arg is a numeric/string/bool literal or a known scalar param like industry/std=4."""
    a = arg.strip()
    if not a:
        return False
    # Numeric
    if re.fullmatch(r"-?\d+(\.\d+)?", a):
        return True
    # Quoted string
    if (a.startswith('"') and a.endswith('"')) or (a.startswith("'") and a.endswith("'")):
        return True
    # Built-in group token (industry / sector / etc.)
    if a.lower() in BUILTIN_GROUPS:
        return True
    # Bool / null-ish
    if a.lower() in {"true", "false", "nan", "inf"}:
        return True
    # Keyword arg like std=4
    if "=" in a:
        return True
    # Bare identifier — could be a group built-in we missed or a small enum
    if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", a):
        return True
    return False


# =============================================================================
# Tier classification
# =============================================================================

def _is_negation_wrapper(expr: str) -> Optional[str]:
    """Recognize sign-flip wrappers that don't change tier semantics.

    A negated tier-N expression is still a tier-N signal, just direction-
    flipped. Sources:
      - PR5 sign-flip retry in evaluation.node_evaluate (auto-flip FAIL with
        |sharpe| ≥ 0.5)
      - genetic_optimizer's _mutate_sign mutation
      - Any future code path that produces an inverted expression

    Recognized patterns (all BRAIN-valid):
      - multiply(-1, X)
      - multiply(X, -1)
      - subtract(0, X)

    Returns the inner expression X if the top-level call matches, else None.
    Caller is expected to recurse into X to determine the actual tier.
    """
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
    """T1: top-level is a ts_* op AND first arg is a single known field."""
    parsed = _top_level_call(expr)
    if not parsed:
        return False
    op, args = parsed
    if op not in _ts_ops():
        return False
    if not args:
        return False
    # First arg must be a single field; remaining args must be scalars/params
    if not _is_single_field_arg(args[0]):
        return False
    for a in args[1:]:
        if not _is_scalar_or_param(a):
            return False
    return True


def _match_atom(arg_str: str, atom) -> bool:
    """Match a single arg string against a pattern atom.

    Atom kinds:
      "<int>"  — any integer literal
      "<num>"  — any int or float literal
      "field_name" — literal field name (case-insensitive); matched as a
                     bare token equal to that field
      "1" / "0" / numeric str — exact numeric match (after stripping)
      tuple    — nested call pattern (handled by _match_pattern)
    """
    if isinstance(atom, tuple):
        return _match_pattern(arg_str, atom)
    s = _strip_outer_parens(arg_str.strip())
    if atom == "<int>":
        return bool(re.fullmatch(r"-?\d+", s))
    if atom == "<num>":
        return bool(re.fullmatch(r"-?\d+(\.\d+)?", s))
    # Plan v5+ #2 (2026-05-07) — <field> wildcard for structural Quasi-T1
    # patterns. Matches any bare identifier that is NOT a number AND NOT
    # an operator-call expression (ie. it's a leaf field reference).
    if atom == "<field>":
        # Reject if it's a numeric literal
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            return False
        # Reject if it's a function call (operator)
        if "(" in s:
            return False
        # Accept bare identifiers
        return bool(re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", s))
    # Numeric literal exact match
    if re.fullmatch(r"-?\d+(\.\d+)?", atom):
        return s == atom
    # Field name: case-insensitive bare token comparison
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", s):
        return False
    return s.lower() == atom.lower()


def _match_pattern(expr_str: str, pattern: tuple) -> bool:
    """Recursively match expr against (op_name, [arg_pattern, ...])."""
    parsed = _top_level_call(expr_str)
    if not parsed:
        return False
    op, args = parsed
    pat_op, pat_args = pattern
    if op != pat_op:
        return False
    if len(args) != len(pat_args):
        return False
    return all(_match_atom(a, p) for a, p in zip(args, pat_args))


def _expression_uses_only_allowed_ops(expr: str) -> bool:
    """True iff every operator token in expr is in _QUASI_T1_ALLOWED_OPS.

    Defense-in-depth check: even if a pattern accidentally matched, reject
    the expression if any forbidden op (statistical ts_op / group_op / etc.)
    appears anywhere in the tree.
    """
    # Find every `ident(` in the expression — these are operator calls
    for m in re.finditer(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", expr):
        op = m.group(1).lower()
        if op in _QUASI_T1_FORBIDDEN_OPS:
            return False
        if op not in _QUASI_T1_ALLOWED_OPS:
            # Unknown op — reject. Whitelist is closed-set by design.
            return False
    return True


def _is_quasi_t1(expr: str) -> bool:
    """Whitelist match: expression must structurally equal one of the
    pre-approved Quasi-T1 patterns. No fuzzy match — mini-AST comparison
    handles whitespace/ordering surface variants robustly.
    """
    s = _strip_outer_parens(expr.strip())
    if not _expression_uses_only_allowed_ops(s):
        return False
    return any(_match_pattern(s, p) for p in _QUASI_T1_PATTERNS)


def _is_t1_or_quasi_t1(expr: str) -> bool:
    """Gate used by classify_tier and downstream T2 wrapping — strict T1
    and Quasi-T1 are treated identically for mining-pipeline purposes.
    """
    return _is_t1(expr) or _is_quasi_t1(expr)


_COMPOSITE_PREPROCESS_OPS: Set[str] = {"winsorize", "ts_backfill"}


def _peel_composite_preprocess(expr: str) -> str:
    """Strip outer winsorize / ts_backfill layers used as preprocess wrappers.

    V-22.6 (2026-05-12) emits composites in the canonical shape:
        ts_op(winsorize(ts_backfill(<quasi_t1>, W), std=S), w)
    Both winsorize and ts_backfill are transparent preprocess steps from a
    tier-classification standpoint — they don't change the underlying signal's
    structural identity. This helper unwraps them so the underlying composite
    can be matched against _is_quasi_t1.

    Peels up to 3 layers (in practice the V-22.6 wrap is at most 2 deep).
    """
    s = _strip_outer_parens(expr.strip())
    for _ in range(3):
        parsed = _top_level_call(s)
        if not parsed:
            return s
        op, args = parsed
        if op in _COMPOSITE_PREPROCESS_OPS and args:
            s = _strip_outer_parens(args[0].strip())
            continue
        return s
    return s


def _is_t2_composite(expr: str) -> bool:
    """V-22.6 (2026-05-12) composite-field T2 path.

    Recognizes ts_op(<preprocess>(<quasi_t1>, ...), w) where <preprocess> is
    any combination of winsorize / ts_backfill layers (or absent).

    Examples that match (all T2):
        ts_rank(divide(ebit, enterprise_value), 20)
        ts_zscore(ts_backfill(divide(ebit, ev), 120), 60)
        ts_rank(winsorize(ts_backfill(divide(ebit, ev), 120), std=4), 20)

    Distinct from _is_t2_via_wrapper because:
      - Outer op here is a *statistical* ts_op (ts_rank/ts_zscore/...), NOT
        a smoothing wrapper. Strict T2 via wrapper requires the outer to be a
        group / pure-xs / smoothing-ts op around a T1 inner.
      - Inner here is multi-field arithmetic (Quasi-T1), NOT a single-field
        T1 expression.

    Together these give composite-field alphas a clean T2 home so the
    pipeline can mine them via expand_t1_strategy + allowed_tiers={1, 2}.
    """
    parsed = _top_level_call(expr)
    if not parsed:
        return False
    op, args = parsed
    if op not in _ts_ops():
        return False
    if not args:
        return False
    inner = _peel_composite_preprocess(args[0])
    if not _is_quasi_t1(inner):
        return False
    for a in args[1:]:
        if not _is_scalar_or_param(a):
            return False
    return True


def _is_t2_via_wrapper(expr: str) -> bool:
    """T2: top-level is a T2-eligible wrapper (group_* / pure_xs / vec_* / smoothing ts) AND first arg is T1.

    Note: smoothing ts ops (ts_decay_linear / ts_mean / ts_std_dev / ts_max / ts_min) at top level
    with a T1 inner = T2 (per plan §"Tier 边界澄清" — nested ts is smoothing wrapper).
    A bare `ts_decay_linear(field, 10)` is T1 because its inner is a single field, not a T1 expr.

    Also accepts the cap-weighted residualize form generated by
    factor_wrapping for `use_group_mean`:
        subtract(T1, group_mean(T1, weight, group))
    — top-level subtract whose right operand is group_mean of the same
    T1 kernel. We classify this as T2 because semantically it is one
    residualize wrapper layer over the T1 signal.
    """
    parsed = _top_level_call(expr)
    if not parsed:
        return False
    op, args = parsed
    if not args:
        return False

    # Special case: subtract(T1, group_mean(T1, weight, group)) — cap-weighted
    # within-group residualize. Treat as a single T2 wrapper layer.
    if op == "subtract" and len(args) == 2:
        left = args[0].strip()
        right = args[1].strip()
        right_parsed = _top_level_call(_strip_outer_parens(right))
        if (
            right_parsed
            and right_parsed[0] == "group_mean"
            and classify_tier(left) == 1
        ):
            return True

    inner = args[0]
    is_wrapper = (
        op in _group_ops()
        or op in _vec_ops()
        or op in _BUILTIN_PURE_XS_OPS
        or op in _T2_SMOOTHING_OPS
    )
    if not is_wrapper:
        return False
    # Inner must be a T1 expression
    if classify_tier(inner) != 1:
        return False
    # Remaining args must be scalars / group tokens / params
    for a in args[1:]:
        if not _is_scalar_or_param(a):
            return False
    return True


def classify_tier(expression: str) -> Optional[int]:
    """Classify an alpha expression into tier 1, 2, 3 or None.

    Returns:
        1 — ts_op(field, ...) single-field time-series signal
        2 — wrapper(T1_expr, ...) cross-sectional or smoothing wrapper
        3 — trade_when(..., <T2 or T3-eligible expr>, ...) entry-filter
        None — multi-field arithmetic, single-layer rank on field, unknown form

    Sign-flip wrappers (`multiply(-1, X)` etc.) are transparent — the tier of
    `multiply(-1, X)` equals the tier of X.
    """
    if not expression or not expression.strip():
        return None
    s = _strip_outer_parens(expression.strip())

    # Negation wrappers are transparent — recurse into the inner expression.
    # Done first so the inner classification governs tier assignment regardless
    # of whether the outer is multiply(-1, ts_rank(...)) or multiply(-1, group_*(...)).
    inner_negated = _is_negation_wrapper(s)
    if inner_negated is not None:
        return classify_tier(inner_negated)

    # T3: trade_when(...) at top level
    parsed = _top_level_call(s)
    if parsed and parsed[0] == "trade_when":
        # Validate inner: trade_when(condition, expr, exit) — second arg is the wrapped expression
        if len(parsed[1]) >= 2:
            inner = parsed[1][1]
            inner_tier = classify_tier(inner)
            if inner_tier in (1, 2, 3):  # accept any valid tier inside; classify outer as 3
                return 3
        return None  # malformed trade_when

    # T1 takes priority over T2 since T2 requires T1 inner — checking T1 first short-circuits cleanly.
    # _is_t1_or_quasi_t1 admits the strict T1 form plus the 15 Quasi-T1 finance-classical
    # two-field arithmetic patterns (Plan v5+ §"Quasi-T1 准一阶白名单 v1.0").
    if _is_t1_or_quasi_t1(s):
        return 1
    if _is_t2_via_wrapper(s) or _is_t2_composite(s):
        return 2
    return None


def is_t1_expression(expression: str) -> bool:
    """Convenience predicate: True iff classify_tier(expression) == 1."""
    return classify_tier(expression) == 1


def derive_control_expression(expression: str) -> Optional[str]:
    """Derive a signal-vs-control "control" expression for an alpha.

    Strips the T1 signal core (ts_op(field, ...)) down to its bare field,
    keeping all structural wrappers (T2 cross-sectional / T3 trade_when /
    negation) intact.  The caller simulates both expressions and compares
    Δ(sharpe_signal − sharpe_control) to attribute whether performance comes
    from the hypothesis signal or from the structural wrapper.

    Supported shapes:
        T1  ts_rank(close, 20)                              → close
        T2  group_neutralize(ts_rank(close,20), industry)   → group_neutralize(close, industry)
        T2s ts_decay_linear(ts_rank(close,5), 10)           → ts_decay_linear(close, 10)
        T3  trade_when(cond, group_rank(ts_zscore(r,5)), -1)→ trade_when(cond, group_rank(r), -1)
        neg multiply(-1, ts_rank(close, 20))                → multiply(-1, close)

    Returns None when no clean control can be derived:
        - Quasi-T1 multi-field arithmetic (divide(ebit,ev)) — no single signal core
        - classify_tier == None — unknown / unsupported structure
        - Empty or malformed expression

    来源: docs/alphagbm_skills_research_2026-05-15.md P0 — signal-vs-control 双跑归因
    """
    if not expression or not expression.strip():
        return None

    def _control(expr: str) -> Optional[str]:
        s = _strip_outer_parens(expr.strip())
        if not s:
            return None

        # Transparent: negation wrappers preserve tier semantics.
        # Recurse into inner; rebuild multiply(-1, <sub>) so the control
        # remains structurally parallel to the signal.
        neg_inner = _is_negation_wrapper(s)
        if neg_inner is not None:
            sub = _control(neg_inner)
            return None if sub is None else f"multiply(-1, {sub})"

        parsed = _top_level_call(s)
        if not parsed:
            return None
        op, args = parsed

        # T3: trade_when(condition, inner_expr, exit_rule, ...)
        # Recurse into the alpha expression (args[1]); keep condition and exit.
        if op == "trade_when":
            if len(args) < 2:
                return None
            sub = _control(args[1])
            if sub is None:
                return None
            new_args = [args[0], sub] + args[2:]
            return f"trade_when({', '.join(new_args)})"

        # T1 kernel: ts_op(field, scalar_params...). Strip to bare field.
        # _is_t1 guarantees args[0] is a single known field identifier.
        if _is_t1(s):
            return _strip_outer_parens(args[0].strip())

        # T2 wrapper (group_* / vec_* / pure-xs / smoothing-ts).
        # Recurse into the first arg (T1 inner); keep all remaining args verbatim.
        t2_ops = _group_ops() | _vec_ops() | _BUILTIN_PURE_XS_OPS | _T2_SMOOTHING_OPS
        if op in t2_ops and args:
            sub = _control(args[0])
            if sub is None:
                return None
            new_args = [sub] + args[1:]
            return f"{op}({', '.join(new_args)})"

        # Everything else (Quasi-T1 multi-field, _is_t2_composite, subtract-
        # group_mean residualize, completely unknown structure) → no clean control.
        return None

    return _control(expression)


def extract_tier1_seed(expression: str) -> Optional[str]:
    """Strip one wrapper layer from a T2 (or T3-via-T2) expression to get its T1 kernel.

    Used by:
    - backfill: confirm T2/T3 → parent T1 ancestry by expression_hash lookup
    - RAG cold-start: synthesize T1 few-shot from historical T2 KB
    - lineage tree: render ancestor chain

    Returns None if extraction fails (malformed structure / not a T2/T3 expr).
    Note: only one layer is stripped. T3 → strip → T2 (not T1). For T3 → T1, call twice.

    Negation wrappers (`multiply(-1, X)`) are transparent — we recurse into X
    so a negated T2 still yields the T1 kernel correctly.
    """
    if not expression:
        return None
    s = _strip_outer_parens(expression.strip())

    # Transparent unwrap of sign-flip wrapper before tier-stripping.
    inner_negated = _is_negation_wrapper(s)
    if inner_negated is not None:
        return extract_tier1_seed(inner_negated)

    parsed = _top_level_call(s)
    if not parsed:
        return None
    op, args = parsed
    if op == "trade_when":
        # T3 → strip to inner T2
        if len(args) >= 2:
            inner = args[1].strip()
            return inner if classify_tier(inner) in (1, 2) else None
        return None
    if op in _group_ops() or op in _vec_ops() or op in _BUILTIN_PURE_XS_OPS or op in _T2_SMOOTHING_OPS:
        # T2 → strip to inner T1
        if args:
            inner = args[0].strip()
            return inner if classify_tier(inner) == 1 else None
    return None


# =============================================================================
# Cross-tier shared utility — dedup + validate variants
# =============================================================================

def _dedup_and_validate(
    variants: List[Dict],
    target_tier: int,
    region: str,
    validator: Optional[AlphaSemanticValidator] = None,
    allowed_tiers: Optional[Set[int]] = None,
) -> List[Dict]:
    """Three-pass filter applied at the end of T1 expand and T2/T3 wrap:
       1. expression_hash dedup (drop duplicates within batch)
       2. semantic validation (drop syntactically/semantically invalid)
       3. tier roundtrip check (classify_tier(out) must be in allowed_tiers)

    Args:
        variants: list of dicts, each containing at minimum {"expression": str, ...}
        target_tier: the tier this batch is nominally producing (1/2/3) — used
                     for log labels.
        region: BRAIN region — passed to validator if instantiated here
        validator: optional pre-built AlphaSemanticValidator; if None, validation
                   is reduced to "non-empty expression" (caller is expected to
                   supply a fields-loaded validator for full check)
        allowed_tiers: optional set of tiers to keep (default = {target_tier}).
                       P1 (2026-05-07): T1 expand passes {1, 2} when emitting
                       both raw ts_op candidates and ts_decay_linear-wrapped
                       smoothed twins. Decay-wrapped variants classify as T2
                       (smoothing wrapper); both shapes get mined together.

    Returns:
        Filtered list of variant dicts; each kept entry has its `factor_tier`
        field populated with the actual classified tier so downstream
        persistence can apply the correct tier-aware gate.
    """
    if allowed_tiers is None:
        allowed_tiers = {target_tier}

    seen_hashes: Set[str] = set()
    out: List[Dict] = []
    n_dup = 0
    n_invalid = 0
    n_tier_mismatch = 0

    for v in variants:
        expr = v.get("expression", "").strip()
        if not expr:
            n_invalid += 1
            continue

        # 1. dedup
        h = compute_expression_hash(expr)
        if h in seen_hashes:
            n_dup += 1
            continue
        seen_hashes.add(h)

        # 2. semantic validate (optional)
        if validator is not None:
            result = validator.validate(expr)
            if not result.valid:
                logger.warning(
                    f"[T{target_tier} expand] dropped invalid: {expr[:80]} errors={result.errors}"
                )
                n_invalid += 1
                continue

        # 3. tier roundtrip — keep if classified into any allowed tier
        actual_tier = classify_tier(expr)
        if actual_tier not in allowed_tiers:
            logger.warning(
                f"[T{target_tier} expand] tier mismatch: {expr[:80]} got_tier={actual_tier} allowed={sorted(allowed_tiers)}"
            )
            n_tier_mismatch += 1
            continue

        # Tag the variant with its actual tier so caller / persistence can
        # apply tier-aware gate logic (e.g. T2 sharpe_min=1.0 vs T1 1.25).
        v_out = dict(v)
        v_out["factor_tier"] = actual_tier
        out.append(v_out)

    if n_dup or n_invalid or n_tier_mismatch:
        logger.info(
            f"[T{target_tier} expand] kept {len(out)}/{len(variants)} "
            f"(dup={n_dup} invalid={n_invalid} tier_mismatch={n_tier_mismatch}) "
            f"allowed_tiers={sorted(allowed_tiers)} region={region}"
        )
    return out
