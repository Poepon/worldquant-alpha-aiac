"""Mining pipeline (producer-consumer) — keeps BRAIN sim slots saturated by
decoupling LLM generation from simulation.

Sub-phase 0 (2026-05-27): concurrent-safe plumbing only. The orchestration in
``runner.run_pipeline_session`` is pure (injectable produce/simulate/evaluate/
persist callables) so the queue/slot/persister mechanics are unit-tested in
isolation, with no DB, BRAIN, or LangGraph dependency. The real node-backed
wiring + ``_run_flat_iteration`` integration land in later sub-phases, gated by
``settings.ENABLE_SIM_PIPELINE`` (default OFF — existing round loop unchanged).

See docs/sim_pipeline_impl_plan_2026-05-27.md.
"""

from backend.agents.pipeline.types import Candidate, SimResult
from backend.agents.pipeline.runner import run_pipeline_session
from backend.agents.pipeline.consumer import build_consumer_stages

__all__ = [
    "Candidate",
    "SimResult",
    "run_pipeline_session",
    "build_consumer_stages",
]
