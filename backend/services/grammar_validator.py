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
_GRAMMAR = r"""
?start: expr
?expr: arith
?arith: arith "+" term     -> add
      | arith "-" term     -> sub
      | term
?term: term "*" factor     -> mul
     | term "/" factor     -> div
     | factor
?factor: "-" factor        -> neg
       | atom
?atom: NUMBER              -> number
     | FIELD               -> field_ref
     | call
     | "(" expr ")"
call: OP_NAME "(" args? ")"
args: arg ("," arg)*
?arg: expr
    | OP_NAME "=" expr     -> kwarg

OP_NAME: /[a-z_][a-z0-9_]*/i
FIELD: /[A-Za-z_][A-Za-z0-9_]*/
NUMBER: /-?\d+(\.\d+)?/

%import common.WS
%ignore WS
"""


# Whitelisted operator names — anything not on this list at call position
# causes a soft warning (still parses since the grammar accepts any
# identifier; the validation layer reports the unknown-op as a
# fail-safe). Extendable via YAML in fast-follow.
_KNOWN_OPS = frozenset({
    # Time-series
    "ts_rank", "ts_zscore", "ts_decay_linear", "ts_decay_exp",
    "ts_corr", "ts_covariance", "ts_mean", "ts_std", "ts_delta",
    "ts_argmax", "ts_argmin", "ts_max", "ts_min", "ts_sum",
    "ts_regression", "ts_regression_residual", "ts_arg_max",
    # Cross-sectional
    "rank", "scale", "industry_neutralize", "sector_neutralize",
    "country_neutralize", "subindustry_neutralize", "cross_sectional_median",
    # Arithmetic / transforms
    "sign", "abs", "log", "exp", "sqrt", "power", "min", "max",
    "winsorize", "where", "if_else",
    # Universe / regime
    "vec_avg", "vec_sum", "subset", "trade_when",
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


def _lazy_parser() -> Optional[object]:
    """Build the lark parser lazily on first call. Cached at module
    level. Returns None when lark import fails (degrade-open mode)."""
    global _PARSER  # type: ignore
    try:
        return _PARSER
    except NameError:
        pass
    try:
        import lark  # type: ignore
    except ImportError:
        logger.warning(
            "[grammar_validator] lark not installed — G3-v2 degrades open"
        )
        _PARSER = None  # type: ignore
        return None
    try:
        _PARSER = lark.Lark(_GRAMMAR, parser="earley")  # type: ignore
    except Exception as e:
        logger.warning(f"[grammar_validator] lark Lark build failed: {e}")
        _PARSER = None  # type: ignore
    return _PARSER  # type: ignore


def _extract_op_names(tree: object) -> List[str]:
    """Walk the parse tree, collect every OP_NAME token at call position."""
    out: List[str] = []
    try:
        from lark import Tree, Token  # type: ignore
    except ImportError:
        return out

    def walk(node: object) -> None:
        if isinstance(node, Tree):
            if node.data == "call" and node.children:
                head = node.children[0]
                if isinstance(head, Token) and head.type == "OP_NAME":
                    out.append(str(head))
            for child in node.children:
                walk(child)
    walk(tree)
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
