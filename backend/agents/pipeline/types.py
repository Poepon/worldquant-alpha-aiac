"""Pipeline payloads passed between producer → consumer → persister.

These are plain in-memory objects (no ORM, no DB session). The persister is the
only stage that touches the database — it reads ``trace_records`` and the
metrics/verdict off ``SimResult`` and writes them through its own single-owner
session. Keeping these DB-free is what lets N consumers run concurrently
without sharing an asyncpg connection (see the F1 finding in the design doc).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Candidate:
    """A validated alpha candidate emitted by the producer, ready to simulate.

    ``context`` carries the MiningState slice the consumer/persister need
    (region, universe, dataset_id, sim settings, delay, hypothesis_id, bandit
    arm, …). ``trace_records`` are the buffered generation-stage trace steps
    (RAG/HYPOTHESIS/CODE_GEN/VALIDATE) that the persister will flush — the
    producer does NOT write them inline (it has no shared session to write to).
    """

    expression: str
    context: Dict[str, Any] = field(default_factory=dict)
    trace_records: List[Dict[str, Any]] = field(default_factory=list)
    # Opaque handle to the producer's richer object (e.g. the AlphaCandidate /
    # MiningState slice) when the real wiring needs more than ``context``.
    payload: Any = None


@dataclass
class SimResult:
    """Outcome of simulating + evaluating one ``Candidate`` (consumer output).

    DB-free: the consumer fills ``metrics``/``verdict`` from BRAIN + the pure
    evaluate compute, appends the SIMULATE/EVALUATE ``trace_records``, and hands
    this to the persister. ``error`` set (and ``ok`` False) when the sim failed
    or no slot was acquired — the persister records the failure, not an alpha.
    """

    candidate: Candidate
    ok: bool = False
    metrics: Dict[str, Any] = field(default_factory=dict)
    verdict: Optional[str] = None
    trace_records: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    # Post-evaluate MiningState (carries the evaluated pending_alphas + context
    # the persister needs for _incremental_save_alphas). None on a failure path.
    state: Any = None
