"""Phase 2 R10 Family-cap signature + cap helper (2026-05-18).

Per master plan §4.4 R10: Hubble v2 Table 1 — same pillar + same family
keeps top-K=2 by score; the rest get marked FAIL with
`_r10_family_cap_dropped=True` so optimization queue / persistence skips them.

"Family" definition (S1: operator-sequence signature is simpler than full AST):
  family_signature(expr) = sha256-prefix of canonicalized operator sequence
  extracted from the expression. Two expressions sharing the same operator
  pipeline (regardless of field/window/literal) → same family.

Why operator-sequence vs full AST:
- Full AST parsing of BRAIN DSL requires the validator's grammar — overkill
  for a coarse-grain family bucket
- pillar_classifier._extract_operators already does the regex extraction;
  reusing it keeps R10 dependency-light
- R3/Q8 AST distance (ast_distance_logger) is the fine-grain signal; R10
  family-cap is the coarse-grain "structural duplicate" filter — they
  serve different purposes per plan §4.4 "family 定义可基于 AST skeleton 聚类"

Pure-function module, zero DB dependency.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Iterable, List, Optional, Sequence, Tuple

from backend.pillar_classifier import _extract_operators

logger = logging.getLogger(__name__)


def family_signature(expression: str) -> str:
    """Hash the operator sequence of an expression into a short family id.

    Same operator pipeline regardless of fields/windows/literals → same sig.
    Empty / no-op expressions get the empty-family signature '<empty>'.
    """
    if not expression or not isinstance(expression, str):
        return "<empty>"
    ops = _extract_operators(expression)
    if not ops:
        return "<empty>"
    op_seq = "|".join(ops)
    return hashlib.sha256(op_seq.encode("utf-8")).hexdigest()[:16]


def _alpha_score(alpha, *, score_key: str = "sharpe") -> float:
    """Resolve an alpha's composite score for ranking inside a family.

    Tries metrics["composite_score"] first (R5 + R1a combined), then sharpe,
    then 0.0. Negative scores acceptable (alpha could be in flip-retry).
    """
    metrics = getattr(alpha, "metrics", None) or {}
    if isinstance(metrics, dict):
        comp = metrics.get("composite_score")
        if isinstance(comp, (int, float)):
            return float(comp)
        sharpe = metrics.get(score_key)
        if isinstance(sharpe, (int, float)):
            return float(sharpe)
    return 0.0


def _alpha_pillar(alpha) -> str:
    """Resolve an alpha's pillar — prefer LLM-emitted, fall back to inferred."""
    metrics = getattr(alpha, "metrics", None) or {}
    if isinstance(metrics, dict):
        p = metrics.get("pillar")
        if isinstance(p, str) and p:
            return p
    # Fall back to inference from expression
    try:
        from backend.pillar_classifier import infer_pillar
        return infer_pillar(expression=getattr(alpha, "expression", ""))
    except Exception:
        return "other"


#: Terminal-fail QualityStatus values that should NOT participate in the
#: family-cap top-K race. These alphas are already discarded by downstream
#: persistence / optimization — counting them toward the per-family quota
#: would crowd out viable PROV/PASS/PENDING candidates AND re-stamp
#: quality_status='FAIL' on rows already marked FAIL (cosmetic pollution
#: in drop counts + R10 logs). See M4 in a425937..HEAD review.
_FAMILY_CAP_EXCLUDED_STATUSES = frozenset({"FAIL", "REJECT"})


def apply_family_cap(
    alphas: Sequence,
    *,
    top_k: int = 2,
    score_key: str = "sharpe",
) -> List[int]:
    """Apply Hubble v2 family-cap on a batch of alpha candidates.

    Groups by (pillar, family_signature(expression)), keeps top-K by score
    within each group, returns INDEX LIST of alphas to drop.

    Caller is responsible for marking dropped alphas:
        for i in drop_idx:
            alphas[i].quality_status = "FAIL"
            alphas[i].metrics["_r10_family_cap_dropped"] = True

    Alphas already in a terminal-fail state (``quality_status`` in
    ``_FAMILY_CAP_EXCLUDED_STATUSES`` = {FAIL, REJECT}) are skipped before
    the per-(pillar, family) partition so they do not occupy a top-K slot
    nor get re-stamped FAIL. They are NOT included in the returned drop
    index list (they were not dropped by the cap — they were already
    failing). The cap therefore only enforces among PROV / PASS / PENDING
    / OPTIMIZE / PASS_PROVISIONAL candidates.

    Args:
        alphas: sequence of AlphaCandidate (or any obj with .expression /
            .metrics / .quality_status attrs)
        top_k: cap per (pillar, family) group — default 2 per Hubble v2
        score_key: which metric key to rank by (default "sharpe")

    Returns:
        List of integer indices (into alphas) to drop. Empty list when no
        family exceeds top_k.
    """
    if top_k < 1:
        # Defensive: top_k=0 would drop everything; reject + log
        logger.warning(f"[family_cap] invalid top_k={top_k}, treating as 1")
        top_k = 1

    # Group: (pillar, family_sig) → list of (score, idx) tuples.
    # M4: skip alphas already in a terminal-fail status — they must not
    # occupy a top-K slot nor be re-stamped FAIL by the caller.
    groups: dict[Tuple[str, str], List[Tuple[float, int]]] = {}
    for idx, a in enumerate(alphas):
        status = getattr(a, "quality_status", None)
        # Normalize to str (handles QualityStatus enum or raw str)
        status_str = getattr(status, "value", status) if status is not None else None
        if status_str in _FAMILY_CAP_EXCLUDED_STATUSES:
            continue
        expr = getattr(a, "expression", "") or ""
        sig = family_signature(expr)
        pillar = _alpha_pillar(a)
        score = _alpha_score(a, score_key=score_key)
        groups.setdefault((pillar, sig), []).append((score, idx))

    drop_idx: List[int] = []
    for (pillar, sig), members in groups.items():
        if len(members) <= top_k:
            continue
        # Sort by score descending — keep highest, drop the rest
        members.sort(key=lambda x: x[0], reverse=True)
        for score, idx in members[top_k:]:
            drop_idx.append(idx)
        logger.debug(
            f"[family_cap] dropped {len(members) - top_k} from "
            f"(pillar={pillar} sig={sig[:8]}) — kept top {top_k} by {score_key}"
        )

    return sorted(drop_idx)


__all__ = ["family_signature", "apply_family_cap"]
