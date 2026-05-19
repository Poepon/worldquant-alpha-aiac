"""G3 Phase A — AST originality gate (shadow mode, 2026-05-19).

Promotes Phase 1 R3/Q8 ``ast_distance_log`` from log-only telemetry into a
**candidate-time** check that flags low-originality alphas in ``node_evaluate``.

Status
------
Phase A — *shadow* mode (no rejection): every candidate gets evaluated, but
``mode="shadow"`` only writes ``alpha.metrics['_g3_*']`` flags. Operators read
the ``/ops/g3/originality-stats`` endpoint to validate τ before promoting to
``mode="soft"`` (PROVISIONAL flag) or ``mode="hard"`` (REJECT).

Why a separate module
---------------------
``diversity_tracker.py`` is per-session in-memory state used by the bandit;
G3 needs to query *cross-task* / *cross-round* history that already lives in
the dedicated ``ast_distance_log`` and ``alphas`` tables. Putting G3 in its
own module keeps ``diversity_tracker.compute_ast_distance`` stable (per the
G3 spec: "不要 touch 现有 compute_ast_distance algorithm").

Complementarity with R10 (Phase 2 family-cap)
---------------------------------------------
* R10 looks at *operator-sequence signature* (coarse-grain — same op pipeline,
  any fields → same family) and caps top-K within a (pillar, family) group.
* G3 looks at *AST subtree set Jaccard* (fine-grain — wrapper-around-base
  alphas with the same shape still trigger). Catches "换皮" alphas that R10
  misses because the operator pipeline differs slightly.

Soft-fail invariant
-------------------
Any DB / parse error inside the checker is swallowed and downgraded to a
"pass" verdict so the evaluation round never breaks. This matches the R1a /
R10 / R5 conventions in evaluation.py.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import List, Literal, Optional, Sequence

from sqlalchemy import select

from backend.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

Verdict = Literal["pass", "blocked", "skipped"]
Mode = Literal["shadow", "soft", "hard"]


@dataclass
class OriginalityVerdict:
    """Outcome of a single OriginalityChecker.check() call.

    Fields are written verbatim into ``alpha.metrics`` under ``_g3_*`` keys
    (see ``apply_to_alpha``). Operators read these via /ops/g3/originality-stats.
    """

    verdict: Verdict
    min_distance: float
    mean_distance: float
    max_distance: float
    nearest_neighbor_hash: Optional[str]
    history_size: int
    threshold: float
    mode: Mode
    reason: str = ""
    # M-A: track exceptions caught inside the checker so /ops endpoint can
    # surface "G3 silently degrading" without trawling logs.
    error: Optional[str] = None

    def to_metrics_dict(self) -> dict:
        """Compact dict for embedding into alpha.metrics."""
        d = {
            "_g3_verdict": self.verdict,
            "_g3_min_distance": round(float(self.min_distance), 4),
            "_g3_mean_distance": round(float(self.mean_distance), 4),
            "_g3_max_distance": round(float(self.max_distance), 4),
            "_g3_nearest_neighbor": self.nearest_neighbor_hash,
            "_g3_history_size": int(self.history_size),
            "_g3_threshold": round(float(self.threshold), 4),
            "_g3_mode": self.mode,
        }
        if self.error:
            d["_g3_error"] = self.error[:200]  # cap to keep metrics row small
        if self.reason:
            d["_g3_reason"] = self.reason
        return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_expr(expression: str) -> str:
    """16-char sha256 prefix — same format ast_distance_logger uses for
    nearest_neighbor_hash so /ops endpoint can JOIN if needed."""
    return hashlib.sha256(expression.encode("utf-8")).hexdigest()[:16]


def _resolve_mode(mode_override: Optional[str]) -> Mode:
    """Coerce settings.AST_ORIGINALITY_MODE to a Mode literal."""
    raw = (mode_override or getattr(settings, "AST_ORIGINALITY_MODE", "shadow") or "shadow")
    val = str(raw).strip().lower()
    if val in ("shadow", "soft", "hard"):
        return val  # type: ignore[return-value]
    logger.warning("[G3] invalid AST_ORIGINALITY_MODE=%r, falling back to 'shadow'", raw)
    return "shadow"


# ---------------------------------------------------------------------------
# OriginalityChecker
# ---------------------------------------------------------------------------

class OriginalityChecker:
    """Decide whether a candidate alpha is AST-original enough vs history.

    Usage::

        checker = OriginalityChecker()
        await checker.load_history(task_id=task.id, region="USA")
        verdict = checker.check(expression="ts_rank(close, 20)")
        if verdict.verdict == "blocked":
            ...  # caller decides what to do based on mode

    History sources (tried in order):
      1. ``ast_distance_log.skeleton`` — already-pre-computed candidate
         expressions from prior rounds. Cheapest source.
      2. ``alphas.expression`` — passed-quality alphas in the task / region.
         Used when ast_distance_log is empty (e.g. first run on a fresh DB).

    Both queries are flag-gated and soft-fail to empty history.
    """

    def __init__(
        self,
        *,
        threshold: Optional[float] = None,
        history_k: Optional[int] = None,
        max_depth: Optional[int] = None,
        mode: Optional[str] = None,
    ):
        self.threshold = float(
            threshold
            if threshold is not None
            else getattr(settings, "AST_ORIGINALITY_MIN_DISTANCE", 0.15)
        )
        self.history_k = int(
            history_k
            if history_k is not None
            else getattr(settings, "AST_ORIGINALITY_HISTORY_K", 50)
        )
        self.max_depth = int(
            max_depth
            if max_depth is not None
            else getattr(settings, "AST_DIVERSITY_MAX_DEPTH", 3)
        )
        self.mode: Mode = _resolve_mode(mode)
        self._history: List[str] = []
        self._loaded = False

    # ---------------------------------------------------------------------
    # History loading
    # ---------------------------------------------------------------------

    async def load_history(
        self,
        task_id: Optional[int] = None,
        region: Optional[str] = None,
    ) -> int:
        """Populate self._history with up to K recent expressions.

        Returns the number of expressions loaded. Soft-fails to empty list on
        any DB error — checker then treats every candidate as "no history,
        verdict=pass" which is the safe default for shadow mode.
        """
        history: List[str] = []
        try:
            from backend.database import AsyncSessionLocal
            from backend.models.ast_distance_log import AstDistanceLog
            from backend.models.alpha import Alpha
        except Exception as e:
            logger.warning("[G3] history loader import failed (non-fatal): %s", e)
            self._history = []
            self._loaded = True
            return 0

        # Source 1: ast_distance_log (per-task slice if provided, else cross-task)
        try:
            async with AsyncSessionLocal() as s:
                q = select(AstDistanceLog.expression).order_by(
                    AstDistanceLog.created_at.desc()
                ).limit(self.history_k)
                if task_id is not None:
                    q = select(AstDistanceLog.expression).where(
                        AstDistanceLog.task_id == task_id
                    ).order_by(AstDistanceLog.created_at.desc()).limit(self.history_k)
                rows = (await s.execute(q)).scalars().all()
                history.extend(r for r in rows if r)
        except Exception as e:
            logger.debug("[G3] ast_distance_log history query failed: %s", e)

        # Source 2: passed alphas in region — only top up if we're short
        if len(history) < self.history_k:
            remaining = self.history_k - len(history)
            try:
                async with AsyncSessionLocal() as s:
                    q = select(Alpha.expression).order_by(
                        Alpha.created_at.desc()
                    ).limit(remaining)
                    if region is not None:
                        q = select(Alpha.expression).where(
                            Alpha.region == region,
                        ).order_by(Alpha.created_at.desc()).limit(remaining)
                    rows = (await s.execute(q)).scalars().all()
                    history.extend(r for r in rows if r)
            except Exception as e:
                logger.debug("[G3] alphas history query failed: %s", e)

        # Dedupe while preserving order
        seen = set()
        deduped: List[str] = []
        for expr in history:
            if expr in seen:
                continue
            seen.add(expr)
            deduped.append(expr)
        self._history = deduped[: self.history_k]
        self._loaded = True
        return len(self._history)

    def seed_history(self, expressions: Sequence[str]) -> None:
        """Test / standalone hook: bypass DB and inject history directly.

        Used by unit tests and the calibration script so they don't need
        an AsyncSessionLocal.
        """
        self._history = list(expressions)[: self.history_k]
        self._loaded = True

    # ---------------------------------------------------------------------
    # Decision
    # ---------------------------------------------------------------------

    def check(self, expression: str) -> OriginalityVerdict:
        """Compute ast_distance vs history and return a verdict.

        Soft-fail invariants:
          - Empty history → verdict="skipped" (no decision possible)
          - Empty / invalid expression → verdict="skipped"
          - Parse / numeric exception → verdict="skipped" with error captured
        """
        if not expression or not expression.strip():
            return OriginalityVerdict(
                verdict="skipped",
                min_distance=1.0,
                mean_distance=1.0,
                max_distance=1.0,
                nearest_neighbor_hash=None,
                history_size=len(self._history),
                threshold=self.threshold,
                mode=self.mode,
                reason="empty expression",
            )

        if not self._history:
            return OriginalityVerdict(
                verdict="skipped",
                min_distance=1.0,
                mean_distance=1.0,
                max_distance=1.0,
                nearest_neighbor_hash=None,
                history_size=0,
                threshold=self.threshold,
                mode=self.mode,
                reason="no history",
            )

        try:
            # Use the Phase 1 stable primitive — DO NOT touch
            # diversity_tracker.compute_ast_distance per G3 spec
            from backend.knowledge_extraction import ast_distance_from_expressions

            distances: List[float] = []
            for hist_expr in self._history:
                if not hist_expr:
                    continue
                d = ast_distance_from_expressions(
                    expression, hist_expr, self.max_depth,
                )
                distances.append(d)
        except Exception as e:
            logger.warning("[G3] distance computation failed (non-fatal): %s", e)
            return OriginalityVerdict(
                verdict="skipped",
                min_distance=1.0,
                mean_distance=1.0,
                max_distance=1.0,
                nearest_neighbor_hash=None,
                history_size=len(self._history),
                threshold=self.threshold,
                mode=self.mode,
                reason="distance computation error",
                error=str(e),
            )

        if not distances:
            return OriginalityVerdict(
                verdict="skipped",
                min_distance=1.0,
                mean_distance=1.0,
                max_distance=1.0,
                nearest_neighbor_hash=None,
                history_size=0,
                threshold=self.threshold,
                mode=self.mode,
                reason="no comparable history",
            )

        d_min = min(distances)
        d_mean = sum(distances) / len(distances)
        d_max = max(distances)
        nn_idx = min(range(len(distances)), key=lambda i: distances[i])
        nn_hash = _hash_expr(self._history[nn_idx])

        if d_min < self.threshold:
            verdict: Verdict = "blocked"
            reason = (
                f"min_distance={d_min:.4f} < τ={self.threshold:.4f} "
                f"(nearest_neighbor={nn_hash})"
            )
        else:
            verdict = "pass"
            reason = (
                f"min_distance={d_min:.4f} >= τ={self.threshold:.4f}"
            )

        return OriginalityVerdict(
            verdict=verdict,
            min_distance=float(d_min),
            mean_distance=float(d_mean),
            max_distance=float(d_max),
            nearest_neighbor_hash=nn_hash,
            history_size=len(distances),
            threshold=self.threshold,
            mode=self.mode,
            reason=reason,
        )


# ---------------------------------------------------------------------------
# Convenience: write verdict onto an AlphaCandidate-like object
# ---------------------------------------------------------------------------

#: Effects per mode — Phase A defaults to shadow only. Phase B/C is operator-
#: driven (flip ``AST_ORIGINALITY_MODE`` setting + observe stats).
_QUALITY_STATUS_BY_MODE = {
    "shadow": None,                              # no quality_status change
    "soft":   "PASS_PROVISIONAL",                # near-PASS bucket; still simulate
    "hard":   "FAIL",                            # reject before simulate path
}


def apply_to_alpha(alpha, verdict: OriginalityVerdict) -> bool:
    """Stamp G3 metrics + (mode-dependent) quality_status onto an alpha.

    Args:
        alpha: AlphaCandidate-like object with ``metrics`` dict and
            ``quality_status`` string.
        verdict: result of ``OriginalityChecker.check(...)``

    Returns:
        True if the alpha was modified (metrics + maybe quality_status),
        False if nothing changed (e.g. skipped verdict in shadow mode).
    """
    if verdict is None:
        return False
    # Always stash metrics so operators can observe verdict distribution
    metrics = dict(getattr(alpha, "metrics", {}) or {})
    metrics.update(verdict.to_metrics_dict())
    alpha.metrics = metrics

    if verdict.verdict != "blocked":
        # pass / skipped — no quality_status change in any mode
        return True

    new_status = _QUALITY_STATUS_BY_MODE.get(verdict.mode)
    if new_status is None:
        # shadow mode — log only, no quality_status mutation
        logger.warning(
            "[G3 shadow] would block alpha — %s | mode=%s expr_head=%r",
            verdict.reason,
            verdict.mode,
            (getattr(alpha, "expression", "") or "")[:80],
        )
        # Tag explicitly so /ops endpoint can count shadow-only blocks
        metrics["_g3_ast_originality_blocked"] = True
        alpha.metrics = metrics
        return True

    # soft / hard — mutate quality_status. Preserve original for audit.
    prev_status = getattr(alpha, "quality_status", None)
    metrics["_g3_ast_originality_blocked"] = True
    metrics["_g3_prev_quality_status"] = prev_status
    alpha.metrics = metrics
    alpha.quality_status = new_status
    logger.info(
        "[G3 %s] blocked alpha quality_status=%s -> %s | %s",
        verdict.mode,
        prev_status,
        new_status,
        verdict.reason,
    )
    return True


__all__ = [
    "OriginalityChecker",
    "OriginalityVerdict",
    "apply_to_alpha",
]
