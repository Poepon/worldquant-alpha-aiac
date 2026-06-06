"""Mining pipeline shared primitives.

Phase 1c-delete retired the FLAT producer-consumer orchestration
(runner / producer / consumer / feedback_g5 / feedback_r1b / client_refresh)
together with ``_run_flat_iteration``. The two survivors are reused by the
HG/S/E pool (``backend/pool/workers.py``):

- ``types`` — the Candidate / SimResult DTOs that cross the pool's DB queue
- ``persister`` — ``build_persister`` (the E-pool persist callable)
"""

from backend.agents.pipeline.types import Candidate, SimResult
from backend.agents.pipeline.persister import build_persister

__all__ = [
    "Candidate",
    "SimResult",
    "build_persister",
]
