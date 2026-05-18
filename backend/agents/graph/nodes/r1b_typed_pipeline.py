"""Phase 3 R1b.4a: typed AlphaMiningPipeline dispatch helper.

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §8.

Activates the 3223-line DORMANT ``backend/agents/core/AlphaMiningPipeline``
as a production code path for tasks with ``hypothesis_centric_variant=3``.
This module is the THIN dispatcher — heavy lifting lives in the core
package. Per plan §8.1 the typed path is ADDITIVE OVERLAY:

  - default path = LangGraph cycle (R1b.1 + R1b.2) — unchanged
  - opt-in path = typed AlphaMiningPipeline.run_iteration loop

Coexists per [V1.0-A2-1] — chosen per-task at task creation via
``MiningTask.config['hypothesis_centric_variant']=3`` AND global flag
``ENABLE_R1B_TYPED_PIPELINE=True``.

Two budget guards per plan §8.4 (4 LLM calls/iter vs LangGraph ~5, so 3x
per-alpha ceiling for round = $0.15 default):
  - per-iteration cap from pipeline.run_iteration's own LLM counter
  - per-round token cost ceiling against
    ``R1B_TOKEN_COST_CEILING_USD_PER_ALPHA * 3``

R1b.4a (this file) ships the helper + tests only — wiring into
``mining_tasks._run_one_round_inline`` happens in R1b.4b/c.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def is_typed_pipeline_active(task: Any) -> bool:
    """True iff the task should be routed through the typed pipeline.

    Conditions per plan §8.2:
      1. ``settings.ENABLE_R1B_TYPED_PIPELINE`` is True (global gate)
      2. ``task.config['hypothesis_centric_variant'] == 3`` (per-task opt-in)

    Defensive: any attribute / settings error → False (graceful fall to
    legacy LangGraph path).
    """
    try:
        from backend.config import settings as _stg
    except Exception:
        return False
    if not bool(getattr(_stg, "ENABLE_R1B_TYPED_PIPELINE", False)):
        return False
    cfg = getattr(task, "config", None) or {}
    try:
        return int(cfg.get("hypothesis_centric_variant", 0)) == 3
    except Exception:
        return False


def _round_budget_usd() -> float:
    """3x per-alpha ceiling for the full typed round, per plan §8.4."""
    try:
        from backend.config import settings as _stg
        per_alpha = float(
            getattr(_stg, "R1B_TOKEN_COST_CEILING_USD_PER_ALPHA", 0.05)
        )
        return per_alpha * 3.0
    except Exception:
        return 0.15  # safe default


def _num_iter_per_round() -> int:
    """How many AlphaMiningPipeline.run_iteration calls per outer round.

    Plan §8.2 sketches 3 iterations; we read from a settings tunable so
    operators can dial down without code change.
    """
    try:
        from backend.config import settings as _stg
        return int(getattr(_stg, "R1B_TYPED_NUM_ITER_PER_ROUND", 3))
    except Exception:
        return 3


async def run_typed_round(
    *,
    task: Any,
    brain: Any,
    db: Any,
    region: str = "USA",
    universe: str = "TOP3000",
    dataset_id: str = "",
    fields: Optional[List[Dict]] = None,
    operators: Optional[List[Dict]] = None,
    trace: Any = None,
    num_iter: Optional[int] = None,
) -> Dict[str, Any]:
    """Run ONE outer-round worth of AlphaMiningPipeline iterations.

    Returns a partial result dict mirroring the legacy round result shape::

        {
          "all_alphas": [...],       # experiment_to_alpha_result list
          "trace_size": int,         # len(trace.hist) post-loop
          "num_iter_executed": int,  # how many iterations actually ran
          "abandoned": bool,         # True if feedback.should_abandon broke loop
          "cost_usd": float,         # accumulated across iterations
          "skipped_disabled": bool,  # True if flag/variant gated out
        }

    Soft-fail: any pipeline exception inside the loop is logged + the loop
    breaks gracefully (NEVER raises). Caller falls back to legacy LangGraph
    cycle as if the typed path were OFF.
    """
    result: Dict[str, Any] = {
        "all_alphas": [],
        "trace_size": 0,
        "num_iter_executed": 0,
        "abandoned": False,
        "cost_usd": 0.0,
        "skipped_disabled": False,
    }

    if not is_typed_pipeline_active(task):
        result["skipped_disabled"] = True
        return result

    try:
        from backend.agents.core.integration import (
            create_alpha_pipeline,
            create_scenario,
            create_trace,
            experiment_to_alpha_result,
        )
        from backend.agents.services import get_llm_service
    except Exception as ex:
        logger.warning(f"[r1b_typed] core/integration imports unavailable: {ex}")
        result["skipped_disabled"] = True
        return result

    try:
        scenario = create_scenario(
            region=region, universe=universe, dataset_id=dataset_id,
            fields=fields or [], operators=operators or [],
        )
        if trace is None:
            trace = create_trace(
                dataset_id=dataset_id, region=region, universe=universe,
            )
        llm_service = get_llm_service()
        pipeline = create_alpha_pipeline(llm_service, brain, scenario)
    except Exception as ex:
        logger.warning(f"[r1b_typed] pipeline construction failed: {ex}")
        return result

    iterations = int(num_iter if num_iter is not None else _num_iter_per_round())
    budget_ceiling = _round_budget_usd()

    for i in range(iterations):
        if result["cost_usd"] >= budget_ceiling:
            logger.warning(
                f"[r1b_typed] round budget exhausted "
                f"${result['cost_usd']:.4f}/${budget_ceiling:.4f}; break"
            )
            break
        try:
            iter_result = await pipeline.run_iteration(trace)
        except Exception as ex:
            logger.warning(f"[r1b_typed] run_iteration {i} raised: {ex}")
            break
        try:
            trace.add_experiment(iter_result.experiment, iter_result.feedback)
            result["all_alphas"].append(
                experiment_to_alpha_result(iter_result.experiment)
            )
            result["num_iter_executed"] += 1
            # Pipeline doesn't currently expose cost — estimate via 4-LLM-per-
            # iter heuristic at the model rate. R1b.4b can replace with real
            # tokens_used aggregation if pipeline exposes it.
            try:
                from backend.agents.graph.nodes.r1b_loop import _estimate_cost
                from backend.config import settings as _stg
                model = getattr(llm_service, "model", None) or getattr(
                    _stg, "R1B_RETRY_MODEL", "claude-haiku-4-5-20251001"
                )
                # Conservative 600-token-per-call * 4 calls/iter rough estimate
                # so the budget cap actually fires; precise accounting in R1b.4b.
                result["cost_usd"] += _estimate_cost(model, 600 * 4)
            except Exception:
                pass
            # Plan §8.2 — feedback.should_abandon breaks early
            if getattr(iter_result.feedback, "should_abandon", False):
                result["abandoned"] = True
                break
        except Exception as ex:
            logger.warning(f"[r1b_typed] post-iter bookkeeping failed: {ex}")
            break

    try:
        result["trace_size"] = len(getattr(trace, "hist", []) or [])
    except Exception:
        pass
    logger.info(
        f"[r1b_typed] round complete: iterations={result['num_iter_executed']} "
        f"alphas={len(result['all_alphas'])} cost=${result['cost_usd']:.4f} "
        f"abandoned={result['abandoned']}"
    )
    return result


__all__ = [
    "is_typed_pipeline_active",
    "run_typed_round",
]
