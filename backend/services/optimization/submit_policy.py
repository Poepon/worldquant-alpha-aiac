"""SubmitPolicy implementations.

Stage A ships ``StageASubmitPolicy`` only — its single job is to enforce
the NEVER auto-submit hard global constraint by returning ``"queue"`` for
every persisted winner. Stage B is the upgrade that adds self-corr-aware
auto-submit; it is conditionally enabled after Stage A's 14d GO/STOP gate.

Source: ``docs/optimization_closure_plan_v1_2026-05-28.md`` §6.
"""
from __future__ import annotations

from typing import List, Optional

from backend.services.optimization.protocols import SubmitAction


class StageASubmitPolicy:
    """The "no auto-submit" policy. Hard invariant — tested explicitly so a
    Stage B regression cannot accidentally weaken this gate.

    Returns ``["queue"] * len(persisted_pks)`` regardless of winner quality.
    The ``None`` entries (ON CONFLICT skips from Persister) are also queued —
    they refer to existing alpha rows that are still backlog candidates.
    """

    async def decide(
        self, persisted_pks: List[Optional[int]]
    ) -> List[SubmitAction]:
        return ["queue"] * len(persisted_pks)
