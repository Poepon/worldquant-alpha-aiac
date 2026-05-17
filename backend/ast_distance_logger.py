"""Phase 1 R3/Q8 (2026-05-17) — AST distance logger.

Standalone helper that computes ast_distance for newly-generated alpha
candidates against the task's recent K alpha history, and writes
aggregated distances to the dedicated ``ast_distance_log`` table. Used
by ``backend/agents/graph/nodes/generation.py`` at the end of
``node_code_gen`` when ``ENABLE_AST_DIVERSITY_DIM=True``.

Why standalone (not part of DiversityTracker):
- DiversityTracker is production-dormant per plan §2.4 — wiring it into
  generation hot path would require additional state-setup. The standalone
  helper has zero dependency on DiversityTracker instance state and can
  fire fire-and-forget from any code-gen call site.
- Mirrors the R1a v1.6 + R2/Q7 dedicated-table pattern
  ([[feedback_r1a_dedicated_log_table]]):  bypass alpha.metrics persistence
  routing, write to dedicated table independently.
"""
from __future__ import annotations

import hashlib
import logging
from typing import List, Optional

from sqlalchemy import select

from backend.config import settings

logger = logging.getLogger(__name__)


def _hash_expr(expression: str) -> str:
    return hashlib.sha256(expression.encode("utf-8")).hexdigest()[:16]


async def log_round_ast_distances(
    task_id: Optional[int],
    round_idx: Optional[int],
    new_expressions: List[str],
    *,
    history_window: Optional[int] = None,
    max_depth: Optional[int] = None,
) -> int:
    """Compute ast_distance for each new expression vs the task's recent
    history, batch-INSERT one row per new expression to ast_distance_log.

    Returns the number of rows written. Soft-fail on any DB / parse error —
    never raises.

    Flag-gated: returns 0 immediately if ENABLE_AST_DIVERSITY_DIM is False.
    """
    if not getattr(settings, "ENABLE_AST_DIVERSITY_DIM", False):
        return 0
    if not new_expressions:
        return 0

    k = int(history_window or getattr(settings, "AST_DIVERSITY_HISTORY_K", 20))
    max_d = int(max_depth or getattr(settings, "AST_DIVERSITY_MAX_DEPTH", 3))

    # Lazy imports — keep module load light + dodge potential cycles
    try:
        from backend.database import AsyncSessionLocal
        from backend.models.alpha import Alpha
        from backend.models.ast_distance_log import AstDistanceLog
        from backend.knowledge_extraction import (
            ast_distance_from_expressions,
            extract_operator_tree,
        )
    except Exception as e:
        logger.warning(f"[R3/Q8] log helper import failure (non-fatal): {e}")
        return 0

    history: List[str] = []
    if task_id is not None:
        try:
            async with AsyncSessionLocal() as s:
                rows = (await s.execute(
                    select(Alpha.expression)
                    .where(Alpha.task_id == task_id)
                    .order_by(Alpha.created_at.desc())
                    .limit(k)
                )).scalars().all()
            history = [r for r in rows if r]
        except Exception as e:
            # Fail-soft: empty history → ast_distance == 1.0 for all new
            # expressions (treated as max-novel) which is sane behaviour
            logger.debug(f"[R3/Q8] history query failed (non-fatal): {e}")

    written = 0
    try:
        from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: F401
        async with AsyncSessionLocal() as s:
            for expr in new_expressions:
                if not expr or not expr.strip():
                    continue
                # Compute distances vs each history entry — O(n²) but n,k small
                distances = [
                    ast_distance_from_expressions(expr, h, max_d)
                    for h in history
                ]
                if distances:
                    d_min = min(distances)
                    d_mean = sum(distances) / len(distances)
                    d_max = max(distances)
                    nn_idx = min(range(len(distances)), key=lambda i: distances[i])
                    nn_hash = _hash_expr(history[nn_idx])
                else:
                    # No history → treat as maximally novel
                    d_min = d_mean = d_max = 1.0
                    nn_hash = None

                tree = None
                try:
                    tree = extract_operator_tree(expr, max_d)
                except Exception:
                    pass
                skeleton = tree.to_skeleton(max_d) if tree else None

                s.add(AstDistanceLog(
                    task_id=task_id,
                    round_idx=round_idx,
                    expression=expr,
                    expression_hash=_hash_expr(expr),
                    skeleton=skeleton,
                    ast_distance_min=d_min,
                    ast_distance_mean=d_mean,
                    ast_distance_max=d_max,
                    nearest_neighbor_hash=nn_hash,
                    history_window=len(history),
                    tracker_version="v1",
                ))
                written += 1
            await s.commit()
    except Exception as e:
        logger.warning(f"[R3/Q8] log batch INSERT failed (non-fatal): {e}")

    return written
