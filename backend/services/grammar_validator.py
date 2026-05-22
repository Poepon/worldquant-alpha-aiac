"""B4.1 G3-v2 grammar-aware validator (Phase 4 Sprint 4 / plan v5 §6.14).

A lark-based parser for a SUBSET of the BRAIN DSL — enough to catch
structurally malformed alphas (unbalanced parens / unknown leading
operator / illegal nesting) BEFORE the LLM-generated text reaches the
existing G3 AST originality gate or the BRAIN simulator. Distinct from
G3 (originality / structural duplication), G3-v2 catches *syntax*
errors that the legacy regex-based ``alpha_semantic_validator`` misses.

Design (per plan v5 §6.14 / v4 §6.14):
  - Grammar covers ~30 most-common BRAIN operators (ts_rank, rank,
    ts_zscore, ts_decay_linear, ts_corr, sign, abs, log, etc.) +
    arithmetic + bracketed/numbered indexing. Coverage extension
    is YAML-driven (operator allowlist) — fast-follow.
  - Parse failure mode: ``validate(expression) -> (ok: bool,
    error_msg: str)``. On parse fail, callers may use
    ``retry_with_whole_output_hint`` (return False + a brief tip) so
    the LLM re-emits with the parse error in scope.
  - **Does NOT replace** G3 (which checks structural duplication, an
    orthogonal axis).
  - Fallback: when ``lark`` import fails (operator-only env without the
    optional dep), validate() returns ``(True, '')`` so the gate
    degrades open (caller falls through to existing checks).

Freeze constraint: this PR adds a NEW path (``ENABLE_GRAMMAR_VALIDATOR``);
the existing G3 shadow code at ``backend/alpha_originality.py`` stays
unchanged + marked ``@deprecated_pending_r12_decision`` (B4.2 in
Sprint 5 retires conditionally per R12 decision).

Pure-function module — no DB, no LLM.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# A subset of BRAIN DSL that the parser accepts. Operator names must be
# whitelisted; unknown identifiers in the leading position are a parse
# failure (catches typos like "ts_rank" → "ts_rnak").
#
# Grammar uses lark's Earley parser for forgiving (left-recursion-free)
# behavior on arithmetic precedence.
# F1 review fix (Sprint 4 R1+R2): the original grammar rejected 6 of 10
# common valid BRAIN shapes — comparisons (> < >= <=), ternary (? :),
# logical (&& ||), power (**), scientific-notation numbers (1.5e-3),
# string literals ('sector'), and dotted field refs (fnd6.assets).
# node_code_gen drops candidates on parse fail, so a too-narrow grammar
# silently discards good alphas. This widened grammar accepts those forms.
#
# Precedence (low → high): ternary < logical-or < logical-and <
# comparison < add/sub < mul/div < power < unary < atom.
_GRAMMAR = r"""
?start: expr
?expr: ternary
?ternary: logic_or "?" expr ":" expr  -> ternary
        | logic_or
?logic_or: logic_or "||" logic_and    -> lor
         | logic_and
?logic_and: logic_and "&&" cmp         -> land
          | cmp
?cmp: arith CMPOP arith                -> compare
    | arith
?arith: arith "+" term                 -> add
      | arith "-" term                 -> sub
      | term
?term: term "*" power                  -> mul
     | term "/" power                  -> div
     | power
?power: factor "**" power              -> pow
      | factor
?factor: "-" factor                    -> neg
       | atom
?atom: NUMBER                          -> number
     | STRING                          -> string_lit
     | FIELD                           -> field_ref
     | call
     | "(" expr ")"
call: OP_NAME "(" args? ")"
args: arg ("," arg)*
?arg: expr
    | OP_NAME "=" expr                 -> kwarg

CMPOP: "<=" | ">=" | "==" | "!=" | "<" | ">"
OP_NAME: /[a-z_][a-z0-9_]*/i
FIELD: /[A-Za-z_][A-Za-z0-9_.]*/
NUMBER: /-?\d+(\.\d+)?([eE][+-]?\d+)?/
STRING: /"[^"]*"/ | /'[^']*'/

%import common.WS
%ignore WS
"""


# Whitelisted operator names — anything not on this list at call position
# causes a soft warning (still parses since the grammar accepts any
# identifier; the validation layer reports the unknown-op as a
# fail-safe). Extendable via YAML in fast-follow.
# Synced to the live BRAIN `operators` table (66 active ops, 2026-05-22).
# The pre-sync list was badly stale — it flagged real ops (multiply,
# group_neutralize, subtract, ts_std_dev, ...) as "unknown" and listed
# non-existent names (industry_neutralize, ts_std, ts_argmax), making the
# `_g3v2_unknown_ops` telemetry pure noise. This is informational only
# (unknown ops are stamped, never reject — see node_code_gen); HARD
# operator-validity against the live registry is enforced separately by
# alpha_semantic_validator (Tier 2b, plan a-streamed-wren). Keep this in
# sync when operators are re-synced from BRAIN; YAML-driven loading is the
# intended fast-follow (see module docstring).
_KNOWN_OPS = frozenset({
    # Arithmetic
    "abs", "add", "densify", "divide", "inverse", "log", "max", "min",
    "multiply", "power", "reverse", "sign", "signed_power", "sqrt", "subtract",
    # Cross-sectional
    "normalize", "quantile", "rank", "scale", "winsorize", "zscore",
    # Group
    "group_backfill", "group_mean", "group_neutralize", "group_rank",
    "group_scale", "group_zscore",
    # Logical
    "and", "equal", "greater", "greater_equal", "if_else", "is_nan",
    "less", "less_equal", "not", "not_equal", "or",
    # Time-series
    "days_from_last_change", "hump", "kth_element", "last_diff_value",
    "ts_arg_max", "ts_arg_min", "ts_av_diff", "ts_backfill", "ts_corr",
    "ts_count_nans", "ts_covariance", "ts_decay_linear", "ts_delay",
    "ts_delta", "ts_mean", "ts_product", "ts_quantile", "ts_rank",
    "ts_regression", "ts_scale", "ts_std_dev", "ts_step", "ts_sum", "ts_zscore",
    # Transformational
    "bucket", "trade_when",
    # Vector
    "vec_avg", "vec_sum",
})


@dataclass
class ValidationResult:
    ok: bool
    error_msg: str = ""
    error_position: Optional[int] = None
    unknown_ops: List[str] = None  # type: ignore

    def __post_init__(self) -> None:
        if self.unknown_ops is None:
            self.unknown_ops = []


# D1 review fix (Tier D): thread-safe lazy build + tri-state cache.
# Prior code cached None on ANY failure (incl. transient lark.Lark build
# errors) → one bad build permanently disabled validation for the process
# even after the cause cleared. Now distinguish:
#   _UNBUILT     — not yet attempted (or last attempt was a transient
#                  build failure → retry on next call)
#   _LARK_MISSING — lark import failed (permanent for this process)
#   <Lark inst>  — built OK
import threading as _threading

_UNBUILT = object()
_LARK_MISSING = object()
_parser_cache: object = _UNBUILT
_PARSER_LOCK = _threading.Lock()
_lark_missing_logged = False

# D3 review fix: max expression length before we skip Earley parsing
# (O(n³) worst case; a pathological 5000-char nested expr can take ~0.5s).
_MAX_EXPR_LEN = 2000


def _reset_parser_cache() -> None:
    """Test helper — force the next _lazy_parser() to rebuild."""
    global _parser_cache, _lark_missing_logged
    with _PARSER_LOCK:
        _parser_cache = _UNBUILT
        _lark_missing_logged = False


def _lazy_parser() -> Optional[object]:
    """Build the lark parser lazily (thread-safe). Returns None when lark
    is unavailable (degrade-open) OR a transient build failure occurred
    (retries on the next call — D1 review fix)."""
    global _parser_cache, _lark_missing_logged
    # Fast path: already resolved (no lock needed for a settled state)
    if _parser_cache is not _UNBUILT:
        return None if _parser_cache is _LARK_MISSING else _parser_cache

    with _PARSER_LOCK:
        # Double-checked: another thread may have built while we waited
        if _parser_cache is not _UNBUILT:
            return None if _parser_cache is _LARK_MISSING else _parser_cache
        try:
            import lark  # type: ignore
        except ImportError:
            _parser_cache = _LARK_MISSING
            # D2 review fix: when the FLAG is ON but lark is missing, the
            # operator believes validation runs — it does not. Emit a
            # one-time ERROR (not a warning) so the silent degrade-open is
            # visible. Falls back to warning when the flag is OFF.
            try:
                from backend.config import settings as _s
                if getattr(_s, "ENABLE_GRAMMAR_VALIDATOR", False) and not _lark_missing_logged:
                    logger.error(
                        "[grammar_validator] ENABLE_GRAMMAR_VALIDATOR=ON but "
                        "lark not installed — G3-v2 validation SILENTLY "
                        "DISABLED (degrade-open). Install lark or flip the "
                        "flag OFF."
                    )
                    _lark_missing_logged = True
                else:
                    logger.warning(
                        "[grammar_validator] lark not installed — G3-v2 degrades open"
                    )
            except Exception:  # noqa: BLE001
                logger.warning("[grammar_validator] lark not installed")
            return None
        try:
            parser = lark.Lark(_GRAMMAR, parser="earley")  # type: ignore
            _parser_cache = parser
            return parser
        except Exception as e:  # noqa: BLE001
            # Transient build failure — leave cache _UNBUILT so a later
            # call retries (do NOT permanently disable on a transient cause).
            logger.warning(
                f"[grammar_validator] lark build failed (will retry next call): {e}"
            )
            return None


def _extract_op_names(tree: object) -> List[str]:
    """Walk the parse tree, collect every OP_NAME token at call position.

    D3 review fix: explicit stack-based walk (was recursive → RecursionError
    on a ~800-deep nested expression at the default 1000-frame limit).
    """
    out: List[str] = []
    try:
        from lark import Tree, Token  # type: ignore
    except ImportError:
        return out

    stack: List[object] = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, Tree):
            if node.data == "call" and node.children:
                head = node.children[0]
                if isinstance(head, Token) and head.type == "OP_NAME":
                    out.append(str(head))
            stack.extend(node.children)
    return out


def validate(expression: str) -> ValidationResult:
    """Parse + sanity-check an alpha DSL expression.

    Returns ValidationResult.ok=True when:
      - expression parses cleanly
      - all leading-identifier-at-call-position operators are in
        _KNOWN_OPS (warning-only — operator can extend the allowlist)

    Returns .ok=False when:
      - empty / whitespace-only input
      - lark parse raises (unbalanced parens / unexpected token / etc.)
      - lark is unavailable AND expression is empty (otherwise degrades
        open)

    Pure function — no I/O.
    """
    if not expression or not expression.strip():
        return ValidationResult(ok=False, error_msg="empty expression")

    # D3 review fix: skip Earley parsing on pathologically long input
    # (O(n³) worst case → a 5000-char nested expr can take ~0.5s; 5 of
    # those per round = multi-second latency). Degrade-open — a genuine
    # 2000+ char alpha is rare and better simulated than dropped.
    if len(expression) > _MAX_EXPR_LEN:
        return ValidationResult(ok=True, error_msg="too_long_skipped")

    parser = _lazy_parser()
    if parser is None:
        # Degrade open — operator can install lark to enable
        return ValidationResult(ok=True, error_msg="lark_unavailable_degrade_open")

    try:
        tree = parser.parse(expression)
    except Exception as e:
        # lark raises lark.UnexpectedInput / UnexpectedToken / etc.
        # Extract column when present for retry hint.
        col = getattr(e, "column", None) or getattr(e, "pos_in_stream", None)
        return ValidationResult(
            ok=False,
            error_msg=f"parse_failed: {type(e).__name__}: {str(e)[:200]}",
            error_position=int(col) if col is not None else None,
        )

    # Soft-warn on unknown operators (do not fail the validation —
    # operator may have added a new BRAIN op not yet in our allowlist)
    op_names = _extract_op_names(tree)
    unknown = sorted(set(op for op in op_names if op not in _KNOWN_OPS))
    return ValidationResult(ok=True, unknown_ops=unknown)


def retry_with_whole_output_hint(
    expression: str,
    result: ValidationResult,
) -> str:
    """Return a brief hint string to feed back to the LLM for a re-emit.

    The hint is intentionally terse — just enough for the LLM to
    self-correct without re-generating from scratch.

    ⚠️ RESERVED — not yet wired into production (Sprint 4 F5 review fix).
    node_code_gen currently BUFFERS parse-fail candidates + degrades-open
    above a 50% drop floor; it does NOT call this hint or re-emit via LLM.
    A future PR may wire this into a bounded re-emit loop gated by
    GRAMMAR_VALIDATOR_RETRY_MAX. Kept + tested so the future wire is a
    drop-in. Called only from test_grammar_validator.py today.
    """
    if result.ok:
        return ""
    lines = [
        "Your previous expression failed grammar validation. Please re-emit a corrected version.",
    ]
    if result.error_msg:
        lines.append(f"Error: {result.error_msg}")
    if result.error_position is not None:
        lines.append(f"Position: column {result.error_position}")
    lines.append(f"Original: `{expression}`")
    return "\n".join(lines)


__all__ = [
    "ValidationResult",
    "validate",
    "retry_with_whole_output_hint",
]
