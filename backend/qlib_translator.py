"""Qlib expression → BRAIN DSL translator (Phase 0 Q3, v1.5 single-version).

Source-of-truth design: function-only module, no classes. Mirrors the style
of backend/pillar_classifier.py (large module-level dicts + thin pure
functions that operate on them).

Scope:
    - Translates Qlib Alpha158 expressions (from
      qlib.contrib.data.handler.Alpha158.get_feature_config) into BRAIN
      fastexpr DSL.
    - 100% pure regex + balanced-paren scanning (no AST library
      dependency added, per plan §3.3).
    - Single output per input (v1.5 simplification — no user vs consultant
      dual translation; meta_data['requires_role']='both' for every row).

Three known traps (plan §3.4):
    1. Ref(x, -N)  →  ts_delay(x, N)      (sign reversal, abs the negative arg)
    2. Rank(x, w)  →  ts_rank(x, w)        (Qlib Rank IS already time-series
                                            percentile — same semantics as
                                            BRAIN ts_rank, not BRAIN rank
                                            which is cross-sectional)
    3. $close      →  close                (strip BRAIN-illegal $ prefix)

Operator mapping confidence: derived from public Qlib docs + Qlib source
(qlib.data.ops). Final correctness is verified by
backend/tests/unit/test_qlib_translator.py + post-translate
alpha_semantic_validator.validate_expression run inside
import_alpha158_knowledge (plan §3.6).
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple


# =============================================================================
# Operator mapping table
# =============================================================================
#
# Qlib operator name (case-sensitive) → BRAIN fastexpr name.
# Empty-string mapping means "drop the call wrapper, keep args". None means
# "no mapping yet — translate() raises NotImplementedError so the caller
# logs to scripts/alpha158_translation_failures.log and skips that row".
#
# Source: Qlib qlib.data.ops module (rolling + element-wise + cross-section).
# Verified mappings (1:1 semantic equivalents per BRAIN docs):

QLIB_TO_BRAIN_OPERATORS: Dict[str, str] = {
    # ---- Rolling (time-series, take a window arg) ----
    "Ref":       "ts_delay",           # Ref(x, p) — lag by p periods
    "Mean":      "ts_mean",            # Mean(x, w)
    "Sum":       "ts_sum",
    "Std":       "ts_std_dev",
    # NOTE: "Var" is NOT in the dispatch table — it gets a custom handler
    # (_convert_var) below that emits power(ts_std_dev(x, w), 2). The naive
    # mapping ts_std_dev was mathematically wrong (variance = std², differ by
    # squared scale). Review M8 (2026-05-18).
    "Max":       "ts_max",
    "Min":       "ts_min",
    "Quantile":  "ts_quantile",        # Quantile(x, w, q)
    "Med":       "ts_median",
    "Skew":      "ts_skewness",
    "Kurt":      "ts_kurtosis",
    "IdxMax":    "ts_argmax",          # 1-based vs 0-based may differ
    "IdxMin":    "ts_argmin",
    "Rank":      "ts_rank",            # TRAP #2: Qlib Rank IS time-series
    "Corr":      "ts_corr",
    "Cov":       "ts_covariance",
    "WMA":       "ts_decay_linear",    # Weighted MA → BRAIN linear-decay
    "EMA":       "ts_decay_exp",       # Exp MA (may not exist; check)
    "Slope":     "ts_regression",      # Slope ≈ regression slope
    "Resi":      "ts_regression_residual",  # may need custom translate
    "Rsquare":   "ts_rsquare",         # R^2 of x-vs-time linear fit
    "Delta":     "ts_delta",           # Delta(x, w) = x - Ref(x, w)
    "ZScore":    "ts_zscore",          # ZScore(x, w) standardize over window

    # ---- Element-wise binary (NO window arg) ----
    # IMPORTANT (Qlib quirk): Qlib's `Less` and `Greater` are element-wise
    # min and max (np.minimum / np.maximum), NOT boolean comparisons. Boolean
    # comparisons in Qlib use `Gt`/`Lt`/`Ge`/`Le`/`Eq`/`Ne` (or Python infix
    # `>`/`<` which the parser turns into those).
    "Add":       "add",
    "Sub":       "subtract",
    "Mul":       "multiply",
    "Div":       "divide",
    "Less":      "min",                # element-wise min(x, y) — NOT boolean
    "Greater":   "max",                # element-wise max(x, y) — NOT boolean
    "Minimum":   "min",                # alias for Less in some Qlib versions
    "Maximum":   "max",                # alias for Greater in some Qlib versions
    "And":       "and_op",
    "Or":        "or_op",
    "Not":       "not_op",
    "Eq":        "equal",
    "Ne":        "not_equal",
    "Gt":        "greater",            # boolean x > y
    "Lt":        "less",               # boolean x < y
    "Ge":        "greater_equal",
    "Le":        "less_equal",

    # ---- Element-wise unary ----
    "Abs":       "abs",
    "Sign":      "sign",
    "Log":       "log",
    "Sqrt":      "sqrt",
    "Power":     "power",
    "SignedPower": "signed_power",     # symmetric x*|x|^(p-1)

    # ---- Control flow ----
    "If":        "if_else",            # If(cond, a, b)

    # =========================================================================
    # Phase 1 Q6 (2026-05-17): Alpha191 / JoinQuant pseudo-code operators
    # =========================================================================
    # JoinQuant alpha191.py uses ALLCAPS keyword convention (`MEAN`, `STD`,
    # `DELAY`, ...) instead of CamelCase. Add UPPERCASE alias keys so the
    # uppercase-first-letter regex (_OPERATOR_CALL_RE = `[A-Z]\w*`) catches
    # them too. CamelCase Qlib entries above stay authoritative; these are
    # synonyms.
    "MEAN":      "ts_mean",
    "SUM":       "ts_sum",
    "STD":       "ts_std_dev",
    # "VAR" — see CamelCase "Var" note above; routed through _convert_var.
    # "MAX"/"MIN"/"TSMAX"/"TSMIN"/"MAXIMUM"/"MINIMUM" — arity-aware
    # dispatch via _convert_max_min (review M9, 2026-05-18). Alpha191
    # uses `MAX(a, b)` as element-wise max while Qlib/JoinQuant use
    # `MAX(x, w)` as rolling window max; the dispatcher picks based on
    # the 2nd arg being a small int literal vs a field/expression.
    # Entries kept here as fall-through table values for arity=1 or
    # legacy callers, though normal flow goes through _convert_max_min.
    "MAX":       "ts_max",
    "MIN":       "ts_min",
    "TSMAX":     "ts_max",
    "TSMIN":     "ts_min",
    "TSRANK":    "ts_rank",            # time-series rank, same as Qlib Rank
    "YSRANK":    "ts_rank",            # Alpha191 variant of TSRANK
    "RANK":      "rank",               # Alpha191/Alpha101 RANK is cross-sectional rank
    "DELTA":     "ts_delta",
    "DELAY":     "ts_delay",
    "CORR":      "ts_corr",
    "COVIANCE":  "ts_covariance",      # Alpha191 sometimes mis-spelled
    "COV":       "ts_covariance",
    "SMA":       "ts_mean",            # simple moving average ≈ ts_mean
    "WMA":       "ts_decay_linear",
    "DECAYLINEAR": "ts_decay_linear",
    "LOG":       "log",
    "ABS":       "abs",
    "SIGN":      "sign",
    "SQRT":      "sqrt",
    "POWER":     "power",
    "SIGNEDPOWER": "signed_power",
    "HIGHDAY":   "ts_argmax",          # Alpha191 HIGHDAY(x, w) ≈ ts_argmax(x, w)
    "LOWDAY":    "ts_argmin",
    "IF":        "if_else",            # ternary keyword form
    "FILTER":    "if_else",            # FILTER(x, cond) ≈ if_else(cond, x, NULL); use with care
    "PROD":      "ts_product",
    "TSPROD":    "ts_product",
    "REGBETA":   "ts_regression",      # Alpha191 regression beta
    "REGRESI":   "ts_regression_residual",
}


# Qlib datafield → BRAIN datafield name (mostly just $-prefix removal +
# casing). $close, $open, $high, $low, $volume, $vwap, $factor are the
# standard Alpha158 inputs.
QLIB_FIELD_TO_BRAIN: Dict[str, str] = {
    "$close":  "close",
    "$open":   "open",
    "$high":   "high",
    "$low":    "low",
    "$volume": "volume",
    "$vwap":   "vwap",
    "$factor": "adv20",  # Qlib $factor doesn't have a direct BRAIN equivalent;
                          # adv20 is the closest "volume-anchored normalizer".
                          # If accuracy matters, replace per-alpha by hand.
}


# =============================================================================
# Translation pipeline
# =============================================================================

_FIELD_PREFIX_RE = re.compile(r"\$(\w+)")
_OPERATOR_CALL_RE = re.compile(r"\b([A-Z]\w*)\s*\(")


def _strip_field_prefix(expr: str) -> str:
    """TRAP #3: convert `$close` etc. to BRAIN bare names per QLIB_FIELD_TO_BRAIN."""
    def sub(m: re.Match) -> str:
        full = m.group(0)
        return QLIB_FIELD_TO_BRAIN.get(full, m.group(1))  # fallback to bare name
    return _FIELD_PREFIX_RE.sub(sub, expr)


def _split_args(arg_text: str) -> List[str]:
    """Split a top-level comma-separated arg list, respecting paren depth."""
    args: List[str] = []
    depth = 0
    buf: List[str] = []
    for ch in arg_text:
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        args.append("".join(buf).strip())
    return args


def _convert_ref(args: List[str]) -> str:
    """TRAP #1: Ref(x, -N) → ts_delay(x, N) — abs the negative window arg."""
    if len(args) != 2:
        raise NotImplementedError(f"Ref expects 2 args, got {len(args)}: {args}")
    x, w = args
    w_stripped = w.strip()
    if w_stripped.startswith("-"):
        w_stripped = w_stripped[1:].strip()
    return f"ts_delay({translate(x)}, {w_stripped})"


def _convert_delta(args: List[str]) -> str:
    """Delta(x, w) is equivalent to ts_delta(x, w) — keep semantics."""
    if len(args) != 2:
        raise NotImplementedError(f"Delta expects 2 args, got {len(args)}: {args}")
    x, w = args
    return f"ts_delta({translate(x)}, {w.strip()})"


def _convert_var(args: List[str]) -> str:
    """Review M8 (2026-05-18): Var(x, w) = variance ≠ std. Emit
    ``power(ts_std_dev(x, w), 2)`` so the result truly is variance scaled
    by squared units, not standard deviation. The previous mapping
    ``ts_std_dev`` was silently wrong by a squared factor.
    """
    if len(args) != 2:
        raise NotImplementedError(f"Var expects 2 args, got {len(args)}: {args}")
    x, w = args
    return f"power(ts_std_dev({translate(x)}, {w.strip()}), 2)"


# Window-arg heuristic for MAX/MIN arity-aware dispatch (review M9
# 2026-05-18). A rolling window arg is typically an int literal in
# [2, 252] (≈ one trading year). Anything else (a field name, an
# arithmetic expression, a float literal, an int outside that range)
# is treated as the second operand of an element-wise call.
_WINDOW_LITERAL_RE = re.compile(r"^-?\d+$")


def _looks_like_window_literal(arg: str) -> bool:
    """True iff arg is an int literal that plausibly represents a
    rolling-window size (2..252). Used by _convert_max_min to
    disambiguate Alpha191 element-wise ``MAX(a, b)`` from rolling
    Qlib/JoinQuant ``MAX(x, w)``.
    """
    s = arg.strip()
    if not _WINDOW_LITERAL_RE.match(s):
        return False
    try:
        n = int(s)
    except ValueError:
        return False
    return 2 <= n <= 252


def _convert_max_min(op_name: str, args: List[str]) -> str:
    """Review M9 (2026-05-18): Alpha191 uses ``MAX(a, b)`` as
    element-wise max (two operands, no window) while Qlib/JoinQuant
    use ``MAX(x, w)`` as rolling window max. Disambiguate by arity +
    window heuristic:

        * 1 arg                      → rolling (unusual; pass through)
        * 2 args, 2nd is int 2..252  → rolling ts_max/ts_min
        * 2 args, 2nd is anything else → element-wise max/min(a, b)
        * 3+ args                    → element-wise variadic max/min

    BRAIN element-wise op names are ``max`` / ``min`` (already used by
    Qlib ``Less``/``Greater`` mapping above), so reuse them rather than
    introducing if_else. CamelCase ``Max``/``Min`` in Alpha158 stay
    rolling-only and continue to dispatch through the generic table —
    only the ALLCAPS aliases (Alpha191 surface) hit this handler.
    """
    if not args:
        raise NotImplementedError(f"{op_name} expects ≥1 arg, got 0")

    is_min = op_name.upper() in ("MIN", "MINIMUM", "TSMIN")
    rolling_brain = "ts_min" if is_min else "ts_max"
    elementwise_brain = "min" if is_min else "max"

    if len(args) == 1:
        # Single arg — preserve as rolling-style call without window;
        # downstream validator will likely reject, but at least we
        # don't silently mis-classify intent.
        return f"{rolling_brain}({translate(args[0])})"

    if len(args) == 2:
        x, w = args
        if _looks_like_window_literal(w):
            return f"{rolling_brain}({translate(x)}, {w.strip()})"
        # Element-wise: two operands, recursively translate both
        return f"{elementwise_brain}({translate(x)}, {translate(w)})"

    # 3+ args: treat as variadic element-wise (BRAIN max/min accept >2 args)
    translated = ", ".join(translate(a) for a in args)
    return f"{elementwise_brain}({translated})"


def _convert_call(op_name: str, raw_args: str) -> str:
    """Dispatch a Qlib call to its BRAIN equivalent with recursive arg translation."""
    args = _split_args(raw_args)

    # Special-case the trap operators
    if op_name == "Ref":
        return _convert_ref(args)
    if op_name == "Delta":
        return _convert_delta(args)
    # Review M8 (2026-05-18): Var ≠ Std; emit power(ts_std_dev, 2).
    if op_name in ("Var", "VAR"):
        return _convert_var(args)
    # Review M9 (2026-05-18): MAX/MIN ALLCAPS aliases are arity-aware
    # (Alpha191 element-wise vs Qlib rolling). MAXIMUM/MINIMUM/TSMAX/
    # TSMIN funnel through the same dispatcher so they share the
    # heuristic. CamelCase "Max"/"Min" (Alpha158 surface) stay rolling-
    # only and continue to dispatch through the generic table below.
    if op_name in ("MAX", "MIN", "MAXIMUM", "MINIMUM", "TSMAX", "TSMIN"):
        return _convert_max_min(op_name, args)

    brain_op = QLIB_TO_BRAIN_OPERATORS.get(op_name)
    if brain_op is None:
        raise NotImplementedError(f"unknown Qlib operator: {op_name!r}")

    # Generic recursive translation of each arg
    translated_args = ", ".join(translate(a) for a in args)
    return f"{brain_op}({translated_args})"


def translate(qlib_expr: str) -> str:
    """Translate a single Qlib expression string into BRAIN fastexpr DSL.

    The translation is recursive: nested calls are translated bottom-up.
    Literal numbers, datafields, and parenthesized subexpressions pass through
    unchanged (after $-prefix removal). Unknown operators raise
    NotImplementedError so callers (e.g. translate_batch +
    import_alpha158_knowledge) can log + skip them.
    """
    if not qlib_expr:
        return ""

    expr = _strip_field_prefix(qlib_expr.strip())

    # Walk left-to-right, looking for `Name(` patterns. If found, find the
    # matching `)` (balanced-paren scan) and recursively translate.
    match = _OPERATOR_CALL_RE.search(expr)
    if match is None:
        # No calls at all — leaf expression (literal / datafield / arithmetic)
        return expr

    op_name = match.group(1)
    paren_open_idx = match.end() - 1  # the '(' position
    depth = 0
    paren_close_idx = -1
    for i in range(paren_open_idx, len(expr)):
        if expr[i] == "(":
            depth += 1
        elif expr[i] == ")":
            depth -= 1
            if depth == 0:
                paren_close_idx = i
                break
    if paren_close_idx == -1:
        raise ValueError(f"unbalanced parentheses in {qlib_expr!r}")

    raw_args = expr[paren_open_idx + 1 : paren_close_idx]
    translated_call = _convert_call(op_name, raw_args)

    # Re-assemble: prefix + translated_call + suffix (suffix may have more calls)
    prefix = expr[: match.start()]
    suffix = expr[paren_close_idx + 1 :]
    translated_suffix = translate(suffix) if suffix.strip() else ""
    return f"{prefix}{translated_call}{translated_suffix}"


def translate_batch(qlib_exprs: List[str]) -> List[Tuple[str, Optional[str]]]:
    """Translate many Qlib expressions; failed ones yield (\"\", error_msg)."""
    out: List[Tuple[str, Optional[str]]] = []
    for q in qlib_exprs:
        try:
            out.append((translate(q), None))
        except (NotImplementedError, ValueError) as ex:
            out.append(("", str(ex)))
    return out


# =============================================================================
# Phase 3 Q10 PR1a (2026-05-18): reverse direction — BRAIN → qlib DSL
# =============================================================================
#
# Plan: ~/.claude/plans/phase3-q10-pyqlib-prescreen-2026-05-18.md §2.2-§2.4.
# Powers `backend/qlib_prescreen.py` (PR1b) which evaluates a translated qlib
# expression on local OHLCV to compute an approximate Sharpe / IC before the
# real BRAIN simulate call. Untranslatable expressions return None — caller
# treats that as "no opinion, proceed to BRAIN".
#
# Coverage gap by design (plan §2.3 [V1.2-A1-3]):
#   * fundamental fields (`fnd*`, `analyst_*`, `news_*`) have no qlib analog
#   * `group_neutralize`, `trade_when` are BRAIN-only execution-layer ops
#   * unknown ops cascade → entire expression marked untranslatable
# Net translatable rate ~30-45% of T1 traffic (price-volume-only alphas).

from functools import lru_cache


BRAIN_TO_QLIB_OPERATORS: Dict[str, Optional[str]] = {
    # Direct one-to-one (semantic equivalent in qlib)
    "ts_mean":          "Mean",
    "ts_sum":           "Sum",
    "ts_std_dev":       "Std",
    "ts_max":           "Max",
    "ts_min":           "Min",
    "ts_rank":          "Rank",            # TRAP #2 reversed: BRAIN ts_rank IS qlib Rank
    "ts_corr":          "Corr",
    "ts_covariance":    "Cov",
    "ts_delta":         "Delta",
    "ts_decay_linear":  "WMA",
    "ts_zscore":        "ZScore",
    "ts_skewness":      "Skew",
    "ts_kurtosis":      "Kurt",
    "ts_argmax":        "IdxMax",
    "ts_argmin":        "IdxMin",
    "ts_median":        "Med",
    "ts_quantile":      "Quantile",
    "ts_product":       "PROD",
    # Element-wise arithmetic
    "add":              "Add",
    "subtract":         "Sub",
    "multiply":         "Mul",
    "divide":           "Div",
    "min":              "Less",            # qlib `Less` = element-wise minimum
    "max":              "Greater",         # qlib `Greater` = element-wise maximum
    # Unary
    "abs":              "Abs",
    "sign":             "Sign",
    "log":              "Log",
    "sqrt":             "Sqrt",
    "power":            "Power",
    "signed_power":     "SignedPower",
    # Control flow
    "if_else":          "If",
    # ts_delay handled by _brain_convert_ts_delay (sign flip — plan §2.2 TRAP #1)
    "ts_delay":         "Ref",
    # rank(x) single-arg = qlib cross-sectional Rank — emit as-is
    "rank":             "Rank",
    # ---- Explicitly untranslatable (None — plan §2.2 REJECT rows) ----
    "group_neutralize": None,
    "group_rank":       None,
    "group_zscore":     None,
    "group_mean":       None,
    "group_scale":      None,
    "trade_when":       None,
    "vector_neut":      None,
    "regression_neut":  None,
    "indneutralize":    None,
    "winsorize":        None,
    "rank_by_side":     None,
    "scale":            None,
}


BRAIN_TO_QLIB_FIELD: Dict[str, Optional[str]] = {
    # ---- Standard OHLCV in pyqlib's CSI300 / SP500 bundle ----
    "close":   "$close",
    "open":    "$open",
    "high":    "$high",
    "low":     "$low",
    "volume":  "$volume",
    "vwap":    "$vwap",
    # ---- Synthetic equivalents ----
    "adv20":   "Mean($volume, 20)",
    "returns": "Ref($close,-1)/$close-1",
    # ---- Out of scope in v1.0 (explicit None) ----
    "cap":     None,
    "pv6":     None,
    "pv13":    None,
    "fnd6":    None,
    "fnd28":   None,
}


_BRAIN_CALL_RE = re.compile(r"\b([a-z][a-z0-9_]*)\s*\(")
_BRAIN_FIELD_RE = re.compile(r"\b([a-z][a-z0-9_]*)\b")
_BRAIN_OP_NAMES = set(BRAIN_TO_QLIB_OPERATORS.keys())


def _brain_split_args(arg_text: str) -> List[str]:
    """Top-level comma split respecting paren depth — mirror of _split_args."""
    args: List[str] = []
    depth = 0
    buf: List[str] = []
    for ch in arg_text:
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        args.append("".join(buf).strip())
    return args


def _brain_field_to_qlib(token: str) -> Optional[str]:
    """Whitelist field-name map; unknown → None (plan §2.3 [V1.1-S3])."""
    if token in _BRAIN_OP_NAMES:
        return token
    if token.isdigit():
        return token
    if token in BRAIN_TO_QLIB_FIELD:
        return BRAIN_TO_QLIB_FIELD[token]
    return None


def _brain_replace_leaf_fields(expr: str) -> Optional[str]:
    """Replace bare field tokens with qlib equivalents in a leaf expression.

    Returns None if any token maps to None (cascade-to-untranslatable).
    """
    out_parts: List[str] = []
    last = 0
    for m in _BRAIN_FIELD_RE.finditer(expr):
        token = m.group(1)
        start, end = m.span(1)
        after = expr[end:end + 2].lstrip()
        if after.startswith("("):
            continue  # call site — handled by recursion
        if token in _BRAIN_OP_NAMES:
            continue
        if token.isdigit() or token in ("True", "False"):
            continue
        mapped = _brain_field_to_qlib(token)
        if mapped is None:
            return None
        out_parts.append(expr[last:start])
        out_parts.append(mapped)
        last = end
    out_parts.append(expr[last:])
    return "".join(out_parts)


def _brain_convert_ts_delay(args: List[str], region: str) -> Optional[str]:
    """TRAP #1 reversed: ts_delay(x, N) → Ref(x, -N) (sign flip)."""
    if len(args) != 2:
        return None
    inner = _brain_to_qlib_inner(args[0], region)
    if inner is None:
        return None
    w = args[1].strip()
    if w.startswith("-"):
        flipped = w[1:].strip()
    else:
        flipped = f"-{w}"
    return f"Ref({inner}, {flipped})"


def _brain_convert_call(op_name: str, raw_args: str, region: str) -> Optional[str]:
    """Recursive dispatch — return qlib call str or None on untranslatable."""
    if op_name == "ts_delay":
        return _brain_convert_ts_delay(_brain_split_args(raw_args), region)
    qlib_op = BRAIN_TO_QLIB_OPERATORS.get(op_name)
    if qlib_op is None:
        return None
    args = _brain_split_args(raw_args)
    translated: List[str] = []
    for a in args:
        inner = _brain_to_qlib_inner(a, region)
        if inner is None:
            return None
        translated.append(inner)
    return f"{qlib_op}({', '.join(translated)})"


def _brain_to_qlib_inner(brain_expr: str, region: str) -> Optional[str]:
    """Core recursive translator (no lru_cache — public entry caches)."""
    if not brain_expr:
        return None
    expr = brain_expr.strip()
    if not expr:
        return None
    match = _BRAIN_CALL_RE.search(expr)
    if match is None:
        return _brain_replace_leaf_fields(expr)
    op_name = match.group(1)
    paren_open = match.end() - 1
    depth = 0
    paren_close = -1
    for i in range(paren_open, len(expr)):
        if expr[i] == "(":
            depth += 1
        elif expr[i] == ")":
            depth -= 1
            if depth == 0:
                paren_close = i
                break
    if paren_close == -1:
        return None
    raw_args = expr[paren_open + 1:paren_close]
    translated_call = _brain_convert_call(op_name, raw_args, region)
    if translated_call is None:
        return None
    prefix = expr[:match.start()]
    suffix = expr[paren_close + 1:]
    prefix_translated = _brain_replace_leaf_fields(prefix) if prefix.strip() else prefix
    if prefix_translated is None:
        return None
    suffix_translated = ""
    if suffix.strip():
        s = _brain_to_qlib_inner(suffix, region)
        if s is None:
            return None
        suffix_translated = s
    return f"{prefix_translated}{translated_call}{suffix_translated}"


@lru_cache(maxsize=1024)
def brain_to_qlib(brain_expr: str, region: str = "USA") -> Optional[str]:
    """Reverse-translate a BRAIN expression to qlib DSL.

    Plan §2.4 — returns the qlib expression string on success, None on
    untranslatable (unknown op, unknown field, group_neutralize,
    trade_when, etc.). Caller treats None as 'skip pre-screen, go straight
    to BRAIN'.

    Memoized per process via lru_cache(maxsize=1024) keyed on (brain_expr,
    region) per plan §2.1 [V1.2-A1-2].

    Region: v1.0 unused (single shared table); v2 may region-partition.
    """
    if not brain_expr or not isinstance(brain_expr, str):
        return None
    try:
        return _brain_to_qlib_inner(brain_expr, region)
    except Exception:
        # Defensive — public contract is "returns None, never raises"
        return None


__all__ = [
    "QLIB_TO_BRAIN_OPERATORS",
    "QLIB_FIELD_TO_BRAIN",
    "translate",
    "translate_batch",
    "BRAIN_TO_QLIB_OPERATORS",
    "BRAIN_TO_QLIB_FIELD",
    "brain_to_qlib",
]
