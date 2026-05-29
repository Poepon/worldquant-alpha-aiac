"""Mining pipeline (producer-consumer) — keeps BRAIN sim slots saturated by
decoupling LLM generation from simulation.

The orchestration in ``runner.run_pipeline_session`` is pure (injectable
produce/simulate/evaluate/persist callables) so the queue/slot/persister
mechanics are unit-tested in isolation, with no DB, BRAIN, or LangGraph
dependency. ``_run_flat_iteration`` (mining_tasks.py) is the node-backed
integration — the sole FLAT path since the serial round loop was retired
(2026-05-29).

See docs/sim_pipeline_impl_plan_2026-05-27.md.
"""

from backend.agents.pipeline.types import Candidate, SimResult
from backend.agents.pipeline.runner import run_pipeline_session
from backend.agents.pipeline.consumer import build_consumer_stages
from backend.agents.pipeline.persister import build_persister
from backend.agents.pipeline.producer import build_producer, run_flat_pipeline_session

__all__ = [
    "Candidate",
    "SimResult",
    "run_pipeline_session",
    "build_consumer_stages",
    "build_persister",
    "build_producer",
    "run_flat_pipeline_session",
]
