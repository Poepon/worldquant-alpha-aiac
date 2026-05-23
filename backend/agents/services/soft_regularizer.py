"""Soft regularizer for code-gen candidates (P1: complexity + originality).

AlphaAgent (KDD 2025) regularizes generated alphas with a *soft* penalty
(originality + alignment + complexity) rather than a hard cap, steering toward
parsimonious, original, hypothesis-aligned factors without zeroing a round.

This module is the **pure-math** half — counting + penalty arithmetic — kept
dependency-free and unit-testable (per CLAUDE.md "standalone analytics
modules"). The orchestration that needs the DB (the originality AST-distance
history via ``alpha_originality.OriginalityChecker``) lives at the call site in
``agents/graph/nodes/evaluation.py``; it computes ``min_distance`` and hands it
to ``evaluate_candidate`` here, which composes the full per-candidate verdict.

P1 wires two legs (complexity + originality). The alignment leg (R5 c1/c2,
LLM-judged hypothesis↔factor) is reserved for P2 — ``w_alignment`` defaults to
0 and ``combine_penalty`` renormalizes over the active (non-zero-weight) legs,
so P1 behaviour is identical whether or not the alignment leg is supplied.

Penalties are all in [0, 1] where higher = "worse" (more complex / less
original / less aligned). The composite ``penalty`` multiplies into the
pre-simulate P(PASS) only in ``soft`` mode:

    effective_p_pass = p_pass * (1 - lambda * penalty)

In ``shadow`` mode the legs are stamped onto ``alpha.metrics`` for τ/weight
calibration (via the persisted ``_soft_reg_*`` keys) but never change which
candidates are simulated.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# Same regexes as alpha_semantic_validator._func_pattern / _field_pattern.
# NB: count_complexity counts operators by TOTAL invocations (findall), whereas
# the validator's _extract_operators dedups to distinct kinds (set) — a
# deliberate difference here (we want a depth/complexity reading), so the
# resulting complexity_score diverges from the validator's on any expression
# that repeats an operator. Field counting (distinct) does match the validator.
_FUNC_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")
_IDENT_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b")

# Identifiers that are group keywords / operator params / literals — NOT data
# fields. Kept in sync with AlphaSemanticValidator._extract_fields' skip set
# (backend/alpha_semantic_validator.py:994); see also portfolio_skeletons
# ._NON_FIELD_TOKENS. TODO: lift these three (regexes + skip) to one shared
# module-level source so the 4 copies can't drift.
_FIELD_SKIP = frozenset({
    "true", "false", "nan", "inf",
    "sector", "subindustry", "industry", "exchange", "country", "market",
    "std", "k", "mode", "lag", "rettype", "filter", "scale", "rate",
    "constant", "percentage", "driver", "sigma", "lower", "upper",
    "target", "dest", "event", "sensitivity", "force", "h", "t", "period",
    "stddev", "factor", "usetd", "limit", "gaussian", "uniform", "cauchy",
    "buckets", "range", "nth", "precise", "longscale", "shortscale",
})


def _clamp01(x: float) -> float:
    if x != x:  # NaN
        return 0.0
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def count_complexity(expression: str) -> Tuple[int, int]:
    """Return (distinct_data_fields, total_operator_invocations).

    Operators are counted by *total* invocations (each ``name(`` occurrence),
    so a nested ``ts_zscore(... ts_zscore(...))`` counts as 2 — this is the
    "expression complexity/depth" reading, not distinct operator kinds. Fields
    are *distinct* data-field identifiers (operators + group keys + params
    excluded).
    """
    if not expression:
        return (0, 0)
    operators = _FUNC_RE.findall(expression)          # total invocations (list)
    n_operators = len(operators)
    op_lower = {op.lower() for op in operators}
    fields = set()
    for ident in _IDENT_RE.findall(expression):
        il = ident.lower()
        if il in op_lower or il in _FIELD_SKIP or ident.isdigit():
            continue
        fields.add(ident)
    return (len(fields), n_operators)


def complexity_score(n_fields: int, n_operators: int) -> float:
    """Single complexity number: ``n_operators + 0.5 * n_fields`` (same formula
    as alpha_semantic_validator.complexity_score). NB the ``n_operators`` fed in
    is the TOTAL-invocation count from count_complexity, not the validator's
    distinct-kind count, so the score is intentionally depth-weighted."""
    return float(n_operators) + 0.5 * float(n_fields)


def complexity_penalty(
    n_fields: int,
    n_operators: int,
    c0: float = 6.0,
    cmax: float = 16.0,
) -> float:
    """Smooth ramp in [0, 1]: 0 below ``c0`` (free complexity), linearly up to
    1 at ``cmax`` (and saturating beyond). No hard cap — over-complex alphas
    are *down-weighted*, not rejected.
    """
    score = complexity_score(n_fields, n_operators)
    if cmax <= c0:  # guard degenerate config → treat as no penalty
        return 0.0
    return _clamp01((score - c0) / (cmax - c0))


def originality_penalty(min_distance: Optional[float]) -> float:
    """Map AST min-distance to history → penalty. Low distance (looks like an
    existing alpha) → high penalty. ``None`` (history empty / undecidable) →
    0 penalty (never punish on absence of evidence).
    """
    if min_distance is None:
        return 0.0
    return _clamp01(1.0 - float(min_distance))


def alignment_penalty(composite_score: Optional[float]) -> float:
    """P2 alignment leg. R5's composite (AlphaAgent C = 0.5*c1 + 0.5*c2, in
    [0,1], higher = hypothesis↔factor better aligned) → penalty. ``None``
    (candidate not judged / R5 abstained or failed) → 0 penalty, same
    "don't punish on absent evidence" rule as originality_penalty.
    """
    if composite_score is None:
        return 0.0
    return _clamp01(1.0 - float(composite_score))


def select_topk_indices(scores: list, k: int) -> list:
    """Indices of the top-``k`` entries by score (desc), tie-broken by index
    asc; result returned in ascending index order. ``k<=0`` / empty → ``[]``.
    Used to pick the most-promising candidates that earn an R5 alignment judge
    (the costly leg), so token spend lands on the candidates competing for the
    scarce BRAIN sim slots.
    """
    if k <= 0 or not scores:
        return []
    ranked = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
    return sorted(ranked[:k])


@dataclass
class SoftRegResult:
    """Per-candidate soft-regularization legs + composite penalty.

    ``penalty`` is the renormalized weighted blend over the *active* (non-zero
    weight) legs. ``to_metrics_dict`` produces the ``_soft_reg_*`` keys that get
    merged into ``alpha.metrics`` (persisted) for calibration.
    """
    n_fields: int
    n_operators: int
    complexity_pen: float
    originality_pen: float
    alignment_pen: float
    penalty: float
    mode: str = "shadow"
    p_pass_orig: Optional[float] = None
    p_pass_adjusted: Optional[float] = None
    # The raw 3-leg penalty BEFORE the one-sided max-clamp. Only differs from
    # ``penalty`` when the alignment leg was clamped out (good alignment, which
    # a regularizer must not reward). Stamped only in that case so shadow-mode
    # calibration can see the leg's would-be effect without recomputing.
    full_penalty: Optional[float] = None

    def to_metrics_dict(self) -> Dict[str, float]:
        d: Dict[str, float] = {
            "_soft_reg_mode": self.mode,
            "_soft_reg_n_fields": self.n_fields,
            "_soft_reg_n_operators": self.n_operators,
            "_soft_reg_complexity_pen": round(self.complexity_pen, 4),
            "_soft_reg_originality_pen": round(self.originality_pen, 4),
            "_soft_reg_alignment_pen": round(self.alignment_pen, 4),
            "_soft_reg_penalty": round(self.penalty, 4),
        }
        if self.p_pass_orig is not None:
            d["_soft_reg_p_pass_orig"] = round(self.p_pass_orig, 4)
        if self.p_pass_adjusted is not None:
            d["_soft_reg_p_pass_adjusted"] = round(self.p_pass_adjusted, 4)
        # Surface the pre-clamp penalty only when the clamp actually fired
        # (alignment leg diluted, then discarded) — otherwise it equals penalty.
        if (
            self.full_penalty is not None
            and round(self.full_penalty, 4) != round(self.penalty, 4)
        ):
            d["_soft_reg_full_pen"] = round(self.full_penalty, 4)
        return d


def combine_penalty(
    complexity_pen: float,
    originality_pen: float,
    alignment_pen: float = 0.0,
    *,
    w_complexity: float = 0.5,
    w_originality: float = 0.5,
    w_alignment: float = 0.0,
) -> float:
    """Weighted blend of the legs, renormalized over the *active* legs (those
    with weight > 0). With ``w_alignment=0`` (P1 default) the alignment leg is
    inert — P1 == P1+P2-with-zero-weight, byte-for-byte."""
    legs = (
        (w_complexity, complexity_pen),
        (w_originality, originality_pen),
        (w_alignment, alignment_pen),
    )
    wsum = sum(w for w, _ in legs if w > 0.0)
    if wsum <= 0.0:
        return 0.0
    blended = sum(w * _clamp01(p) for w, p in legs if w > 0.0) / wsum
    return _clamp01(blended)


def effective_p_pass(p_pass: float, penalty: float, lam: float) -> float:
    """Down-weight P(PASS) by the composite penalty (soft mode only).

    ``lam`` (lambda) is the max fraction of P(PASS) that a fully-penalized
    candidate loses. lam=0 → no effect; lam=1 → a penalty=1 candidate is
    fully suppressed.
    """
    return _clamp01(p_pass * (1.0 - _clamp01(lam) * _clamp01(penalty)))


def evaluate_candidate(
    expression: str,
    min_distance: Optional[float],
    p_pass: float,
    *,
    w_complexity: float = 0.5,
    w_originality: float = 0.5,
    w_alignment: float = 0.0,
    alignment_pen: float = 0.0,
    c0: float = 6.0,
    cmax: float = 16.0,
    lam: float = 0.5,
    mode: str = "shadow",
) -> SoftRegResult:
    """Compose the full soft-reg verdict for one candidate.

    Pure: the caller supplies the originality ``min_distance`` (from the
    DB-backed OriginalityChecker) and the classifier ``p_pass``; everything
    else is arithmetic. ``p_pass_adjusted`` is populated only in ``soft`` mode
    (in ``shadow`` the legs are computed for calibration but P(PASS) is left
    untouched).
    """
    n_fields, n_operators = count_complexity(expression)
    c_pen = complexity_penalty(n_fields, n_operators, c0=c0, cmax=cmax)
    o_pen = originality_penalty(min_distance)
    # The alignment leg is ONE-SIDED: it may only ADD penalty, never remove it.
    # combine_penalty renormalizes over active legs, so a third (alignment) leg
    # whose value is below the complexity/originality average would DILUTE the
    # composite — letting a judged candidate with good alignment outrank an
    # equal un-judged peer purely from being scored. A regularizer must never
    # reward, so clamp to the base 2-leg penalty as a floor. When w_alignment=0
    # (P1 / un-judged) base == full, so this is a no-op for those candidates.
    base_pen = combine_penalty(
        c_pen, o_pen, 0.0,
        w_complexity=w_complexity, w_originality=w_originality, w_alignment=0.0,
    )
    full_pen = combine_penalty(
        c_pen, o_pen, alignment_pen,
        w_complexity=w_complexity, w_originality=w_originality, w_alignment=w_alignment,
    )
    penalty = max(base_pen, full_pen)
    p_adj = effective_p_pass(p_pass, penalty, lam) if mode == "soft" else None
    return SoftRegResult(
        n_fields=n_fields, n_operators=n_operators,
        complexity_pen=c_pen, originality_pen=o_pen, alignment_pen=alignment_pen,
        penalty=penalty, mode=mode,
        p_pass_orig=p_pass, p_pass_adjusted=p_adj, full_penalty=full_pen,
    )
