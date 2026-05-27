"""F2-4: G5 trajectory-crossover through the pipeline feedback channel.

G5 is fundamentally a DB-poll mechanism (the legacy round-end hook queries the
alphas table for the best PASS pair). In the pipeline it rides the feedback loop:

- **classifier** (persister-side, sync, DB-free): a persisted PASS result →
  a PASS_LANDED event (a "a PASS just landed, maybe a crossover is now possible"
  trigger). The PASS row is committed BEFORE the event (persist_every==1), so the
  producer's crossover query sees it.
- **handler** (producer-side, owns db+wf): queries the top PASS alphas for this
  task/region, picks the best eligible UNCROSSED pair, LLM-combines them
  (llm_crossover_alpha, 5 strategies), builds the offspring as AlphaCandidates,
  validates them (wf.run_validate), and pushes the valid ones to be simulated.

Termination/bounding (the runner's quiescence needs feedback fan-out bounded):
a crossover offspring that PASSes can itself trigger another PASS_LANDED →
crossover with the growing pool. At the low delay-0 PASS rate this converges, but
two hard bounds guarantee it regardless: (1) each parent pair is crossed at most
once (session-local dedupe set), (2) a hard cap on total crossovers per session
(G5_PIPELINE_MAX_CROSSOVERS).

Wired (by _run_flat_iteration_pipeline) only when ENABLE_G5_CROSSOVER is on.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from backend.agents.pipeline.producer import _sim_ready_payload
from backend.agents.pipeline.types import (
    FEEDBACK_PASS_LANDED,
    Candidate,
    FeedbackEvent,
    SimResult,
)

logger = logging.getLogger(__name__)


def _sattr(state: Any, name: str, default: Any) -> Any:
    if state is None:
        return default
    if isinstance(state, dict):
        return state.get(name, default)
    return getattr(state, name, default)


def _state_with_pending(state: Any, alphas: list):
    """Copy a parent state but swap in the offspring as pending_alphas and clear
    the generation-scoped fields, for the validate-only pass."""
    update = {
        "pending_alphas": list(alphas),
        "generated_alphas": [],
        "trace_steps": [],
        "current_alpha_index": 0,
    }
    if hasattr(state, "model_copy"):
        return state.model_copy(update=update)
    if isinstance(state, dict):
        merged = dict(state)
        merged.update(update)
        return merged
    return state


# --------------------------------------------------------------------------- #
# Classifier                                                                   #
# --------------------------------------------------------------------------- #
def build_g5_classifier() -> Callable[[SimResult], Optional[FeedbackEvent]]:
    """Persister-side classifier: a PASS / PASS_PROVISIONAL result → a
    PASS_LANDED event (trigger a crossover check). Returns None otherwise."""

    def classify(result: SimResult) -> Optional[FeedbackEvent]:
        if getattr(result, "verdict", None) in ("PASS", "PASS_PROVISIONAL"):
            return FeedbackEvent(kind=FEEDBACK_PASS_LANDED, result=result)
        return None

    return classify


# --------------------------------------------------------------------------- #
# Handler                                                                      #
# --------------------------------------------------------------------------- #
def build_g5_handler(
    *,
    run_id: Optional[int],
    config: Optional[dict] = None,
    top_k: int = 2,
    min_sharpe: float = 1.25,
    require_diff_pillar: bool = True,
    max_crossovers: int = 20,
) -> Callable[[FeedbackEvent, Callable[[Candidate], Awaitable[None]], Any, Any], Awaitable[None]]:
    """Producer-side crossover handler. Closure state: a dedupe set of crossed
    parent pairs + a hard crossover counter — together they bound feedback
    fan-out so the session reaches quiescence."""
    crossed_pairs: set = set()          # frozenset({a_id, b_id})
    counter = {"n": 0}

    async def handle(event: FeedbackEvent, push, db, wf) -> None:
        if event.kind != FEEDBACK_PASS_LANDED:
            return
        if counter["n"] >= max_crossovers or db is None:
            return  # hard termination backstop

        from sqlalchemy import desc as _desc, select as _select
        from backend.models import Alpha, Hypothesis

        st = getattr(event.result, "state", None)
        task_id = _sattr(st, "task_id", None)
        region = _sattr(st, "region", None)
        if task_id is None or region is None:
            return

        # Top PASS alphas for this task/region (mirrors the legacy round-end query).
        stmt = (
            _select(Alpha, Hypothesis)
            .outerjoin(Hypothesis, Alpha.hypothesis_id == Hypothesis.id)
            .where(
                Alpha.task_id == task_id,
                Alpha.region == region,
                Alpha.quality_status.in_(["PASS", "PASS_PROVISIONAL"]),
                Alpha.is_sharpe.isnot(None),
                Alpha.is_sharpe >= min_sharpe,
            )
            .order_by(_desc(Alpha.is_sharpe))
            .limit(20)
        )
        rows = (await db.execute(stmt)).all()
        if len(rows) < 2:
            return

        # Best eligible UNCROSSED pair: a = top sharpe, b = first distinct
        # hypothesis (+ pillar) not already crossed; fall back to any uncrossed b.
        alpha_a, hyp_a = rows[0]

        def _uncrossed(b):
            return frozenset({alpha_a.id, b.id}) not in crossed_pairs

        chosen = None
        for cand_alpha, cand_hyp in rows[1:]:
            if (cand_alpha.hypothesis_id is not None
                    and cand_alpha.hypothesis_id == alpha_a.hypothesis_id):
                continue
            if require_diff_pillar and hyp_a is not None and cand_hyp is not None:
                if hyp_a.pillar and cand_hyp.pillar and hyp_a.pillar == cand_hyp.pillar:
                    continue
            if _uncrossed(cand_alpha):
                chosen = (cand_alpha, cand_hyp)
                break
        if chosen is None:  # diversity-constrained pairs all crossed → any uncrossed
            for cand_alpha, cand_hyp in rows[1:]:
                if _uncrossed(cand_alpha):
                    chosen = (cand_alpha, cand_hyp)
                    break
        if chosen is None:
            return  # every pair with the top alpha already crossed
        alpha_b, hyp_b = chosen

        crossed_pairs.add(frozenset({alpha_a.id, alpha_b.id}))
        counter["n"] += 1

        from backend.agents.llm_crossover_alpha import llm_crossover_alpha

        def _m(alpha):
            return {"sharpe": alpha.is_sharpe, "fitness": alpha.is_fitness,
                    "turnover": alpha.is_turnover}

        offspring = await llm_crossover_alpha(
            alpha_a.expression or "",
            alpha_b.expression or "",
            region=region,
            llm_service=wf.llm_service,
            parent_a_metrics=_m(alpha_a),
            parent_b_metrics=_m(alpha_b),
            parent_a_pillar=getattr(hyp_a, "pillar", None) if hyp_a else None,
            parent_b_pillar=getattr(hyp_b, "pillar", None) if hyp_b else None,
            top_k=top_k,
        )
        await _write_g5_log(run_id, region, alpha_a, alpha_b, hyp_a, hyp_b,
                            offspring, getattr(wf, "llm_service", None))
        if not offspring:
            return

        # Build offspring AlphaCandidates (mirror node_code_gen's G5 prepend) and
        # validate them on a copy of the parent's state before re-simulating.
        from backend.agents.graph.state import AlphaCandidate

        cands = []
        for off in offspring:
            expr = (off.get("expression") or "").strip()
            if not expr:
                continue
            cands.append(AlphaCandidate(
                expression=expr,
                hypothesis=(f"G5 crossover: combine alpha {alpha_a.id} + "
                            f"{alpha_b.id} via {off.get('combination_strategy', '?')}"),
                explanation=(off.get("rationale") or "")[:200],
                parent_alpha_id=alpha_a.id,
                metrics={"_g5_crossover_parent_ids": [alpha_a.id, alpha_b.id],
                         "_g5_combination_strategy": off.get("combination_strategy", "unspecified")},
            ))
        if not cands:
            return

        validated = await wf.run_validate(_state_with_pending(st, cands), config=config)
        ds = _sattr(st, "dataset_id", None)
        for a in (_sattr(validated, "pending_alphas", None) or []):
            if not getattr(a, "is_valid", False):
                continue
            await push(Candidate(
                expression=getattr(a, "expression", "") or "",
                context={"dataset_id": ds, "g5_offspring": True},
                trace_records=[],
                payload=_sim_ready_payload(validated, a),
            ))

    return handle


async def _write_g5_log(run_id, region, alpha_a, alpha_b, hyp_a, hyp_b,
                        offspring, llm_model) -> None:
    """Record the crossover attempt (incl. empty offspring → LLM rejection rate)
    on its OWN session — never the producer's (F1)."""
    try:
        from backend.database import AsyncSessionLocal
        from backend.models import G5CrossoverLog

        async with AsyncSessionLocal() as log_db:
            log_db.add(G5CrossoverLog(
                task_id=getattr(alpha_a, "task_id", None),
                run_id=run_id,
                round_idx=None,
                region=region,
                parent_a_alpha_id=alpha_a.id,
                parent_b_alpha_id=alpha_b.id,
                parent_a_sharpe=alpha_a.is_sharpe,
                parent_b_sharpe=alpha_b.is_sharpe,
                parent_a_pillar=getattr(hyp_a, "pillar", None) if hyp_a else None,
                parent_b_pillar=getattr(hyp_b, "pillar", None) if hyp_b else None,
                offspring_count=len(offspring or []),
                offspring_expressions=offspring if offspring else None,
                llm_model=getattr(llm_model, "model", None),
                error_kind=None if offspring else "no_valid_offspring",
            ))
            await log_db.commit()
    except Exception:  # noqa: BLE001 — observability, never fatal
        logger.debug("[pipeline] G5 crossover-log write failed (skipped)", exc_info=True)
