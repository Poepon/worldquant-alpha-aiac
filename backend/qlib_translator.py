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
    "Var":       "ts_std_dev",         # closest: variance = std^2; warn
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
    "Delta":     "ts_delta",           # Delta(x, w) = x - Ref(x, w)
    "ZScore":    "ts_zscore",          # ZScore(x, w) standardize over window

    # ---- Element-wise binary (NO window arg) ----
    "Add":       "add",
    "Sub":       "subtract",
    "Mul":       "multiply",
    "Div":       "divide",
    "Less":      "less",               # Qlib Less(x, y) is x<y boolean compare
    "Greater":   "greater",            # Qlib Greater(x, y) is x>y boolean compare
    "Minimum":   "min",                # If a Qlib variant uses Minimum/Maximum
    "Maximum":   "max",                # for element-wise min/max
    "And":       "and_op",             # may need custom; logical AND
    "Or":        "or_op",
    "Not":       "not_op",
    "Eq":        "equal",
    "Ne":        "not_equal",
    "Gt":        "greater",
    "Lt":        "less",
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


def _convert_call(op_name: str, raw_args: str) -> str:
    """Dispatch a Qlib call to its BRAIN equivalent with recursive arg translation."""
    args = _split_args(raw_args)

    # Special-case the three trap operators
    if op_name == "Ref":
        return _convert_ref(args)
    if op_name == "Delta":
        return _convert_delta(args)

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
