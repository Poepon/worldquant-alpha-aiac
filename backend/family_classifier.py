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
from typing import Iterable, List, Optional, Sequence, Tuple, TYPE_CHECKING

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


def apply_family_hard_ban(
    alphas: Sequence,
    *,
    pnl_corr_matrix: Optional["object"] = None,
    threshold: float = 0.65,
) -> List[int]:
    """Apply Phase 4 R10-v2 family hard-ban (per plan v5 §6.10).

    Within each family (same family_signature), any pair whose pairwise
    PnL correlation ≥ threshold triggers a ban on the lower-scoring
    member. Stamp-only — caller stamps ``metrics["_r10v2_hard_banned"]
    = True``; FAIL classification is deferred to evaluation node's
    finalize pass so multiple stamps coexist for the互验 SQL output.

    Distinct from ``apply_family_cap`` (top-K structural cap):
      * R10 family-cap     = same family + same pillar → keep top-K
        (coarse-grain, ignores how *similar* the alpha's actual PnL is)
      * R10-v2 hard-ban    = same family + pairwise PnL corr ≥ τ →
        ban (fine-grain, real-portfolio diversification signal)

    Args:
        alphas: sequence of alpha-like objects with .expression /
            .metrics / .quality_status. Each must additionally have
            either ``.alpha_id`` (BRAIN id; used as pnl_corr_matrix
            key) or ``.id`` (internal DB id).
        pnl_corr_matrix: optional pandas.DataFrame indexed by alpha_id
            on both axes (square, symmetric, diag=1.0). When None or
            missing alpha rows, the function returns an empty list
            (no ban) — caller is expected to soft-skip when matrix
            unavailable for the round.
        threshold: τ ∈ [0, 1]. ≥ τ → ban the lower-scoring sibling.
            Default 0.65 (conservative; the R10-calib spike's recommend
            output will tune this per region — see
            scripts/calibrate_r10_pairwise_corr.py).

    Returns:
        Sorted list of integer indices (into alphas) to mark banned.
        Caller responsibility:
            for i in ban_idx:
                alphas[i].metrics["_r10v2_hard_banned"] = True
                alphas[i].metrics["_r10v2_hard_ban_reason"] = ...

    Pure-function — no DB / BRAIN calls. PnL matrix must be supplied by
    caller (typically via CorrelationService.refresh_os_alpha_cache +
    pandas .corr).
    """
    if not alphas:
        return []
    if pnl_corr_matrix is None:
        return []
    if not (0.0 <= threshold <= 1.0):
        logger.warning(
            f"[family_hard_ban] threshold={threshold} out of [0,1] — skipping"
        )
        return []

    # Group by family_signature (skip terminal-fail alphas; their FAIL
    # is already accounted for and they should not occupy a sibling slot).
    groups: dict[str, List[Tuple[float, int, str]]] = {}
    for idx, a in enumerate(alphas):
        status = getattr(a, "quality_status", None)
        status_str = getattr(status, "value", status) if status is not None else None
        if status_str in _FAMILY_CAP_EXCLUDED_STATUSES:
            continue
        aid = getattr(a, "alpha_id", None) or getattr(a, "id", None)
        if aid is None:
            continue
        expr = getattr(a, "expression", "") or ""
        sig = family_signature(expr)
        if sig == "<empty>":
            continue
        score = _alpha_score(a)
        groups.setdefault(sig, []).append((score, idx, str(aid)))

    ban_idx: set[int] = set()
    for sig, members in groups.items():
        if len(members) < 2:
            continue
        # Sort descending by score — preserve the highest, ban siblings
        # whose corr against the surviving set exceeds threshold.
        members.sort(key=lambda x: x[0], reverse=True)
        survivors: List[Tuple[float, int, str]] = []
        for score, idx, aid in members:
            ban_this = False
            for _s, _i, surv_aid in survivors:
                try:
                    c = pnl_corr_matrix.loc[aid, surv_aid]
                except KeyError:
                    continue
                except Exception:  # noqa: BLE001 — corr lookup must never break round
                    continue
                # pandas .at / .loc returns numpy scalar; safe float convert
                try:
                    cf = float(c)
                except (TypeError, ValueError):
                    continue
                if cf >= threshold:
                    ban_this = True
                    break
            if ban_this:
                ban_idx.add(idx)
            else:
                survivors.append((score, idx, aid))
        if len(members) - len(survivors) > 0:
            logger.debug(
                f"[family_hard_ban] family={sig[:8]} kept {len(survivors)}/{len(members)} "
                f"(τ={threshold:.2f})"
            )

    return sorted(ban_idx)


__all__ = ["family_signature", "apply_family_cap", "apply_family_hard_ban"]
