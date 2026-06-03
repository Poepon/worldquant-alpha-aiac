"""Optimization-closure protocols + value objects.

These are the only types that cross the optimization service boundary. The
plan locks Layer 3 (OptimizationService.run_one_cycle) signature for the full
A→B→C arc — Layer 2 generators / Layer 4 triggers swap behind these protocols.

Source: ``docs/optimization_closure_plan_v1_2026-05-28.md`` §4.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Protocol


# ---------------------------------------------------------------------------
# Value objects (dataclasses, not Pydantic — these never cross HTTP boundary)
# ---------------------------------------------------------------------------


@dataclass
class Variant:
    """One concrete (expression, settings) candidate to be simulated.

    ``tag`` is the human-readable axis label (e.g. ``"decay=4|window=60|neut=INDUSTRY"``)
    — surfaced in telemetry + audit trails.

    ``generator_name`` matches the generator's ``name`` attribute
    (``"settings_sweep"`` / ``"expression_rewrite"`` / ``"ga"``). Used by the
    SQL conversion-rate query in ``/ops/optimization/cycles`` to slice per
    generator.

    ``generation`` is GA-specific (parent → child depth). Always 0 for
    settings_sweep + expression_rewrite.
    """

    expression: str
    settings: Dict[str, Any]
    tag: str
    generator_name: str
    generation: int = 0


@dataclass
class VariantSimResult:
    """The packed result of simulating one variant against BRAIN.

    ``checks_passed`` is the AND of all BRAIN gate checks (sharpe + fitness +
    turnover + sub-univ + concentration + self-corr); WinnerSelector reads
    only this + the four metric scalars (sharpe/fitness/turnover/subuniv).

    ``self_corr`` is filled by Persister (post-simulate, before insert) — kept
    here in the result struct so the Persister doesn't have to re-query;
    None when the corr cache is cold or BRAIN didn't return SELF check.
    """

    variant: Variant
    sim_response: Dict[str, Any]
    sharpe: Optional[float]
    fitness: Optional[float]
    turnover: Optional[float]
    margin: Optional[float]
    subuniv: Optional[float]
    brain_alpha_id: Optional[str]
    checks_passed: bool
    self_corr: Optional[float] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class VariantGenerator(Protocol):
    """Layer 2: produce concrete variants from a parent alpha.

    Stage A ships ``SettingsSweepGenerator``; Stage B adds
    ``ExpressionRewriteGenerator`` and ``CompositeGenerator``; Stage C adds
    ``GeneticOptimizerGenerator``. All share this signature.
    """

    name: str

    async def generate(self, alpha: Any) -> List[Variant]:
        """``alpha`` is a ``backend.models.Alpha`` row."""
        ...


class Simulator(Protocol):
    """Layer 3 primitive: run a batch of variants on BRAIN, return packed results.

    ``budget`` is the upper bound on BRAIN sim spend for this batch. The
    Simulator MUST record every sim it spends (even when no cap is active
    in Stage A) to the SimBudget counter so Stage B's allocator has
    historical data to calibrate against.
    """

    async def run_batch(
        self, variants: List[Variant], budget: int
    ) -> List[VariantSimResult]:
        ...


class WinnerSelector(Protocol):
    """Layer 3 primitive: filter sim results to the ones that clear the band.

    ``delay`` must be the parent alpha's actual delay — drives the band
    lookup via ``settings.eval_thresholds(delay)`` (delay-0 is stricter
    than delay-1, see commit ``b8a9560``).
    """

    def pick(
        self, results: List[VariantSimResult], delay: int
    ) -> List[VariantSimResult]:
        ...


class Persister(Protocol):
    """Layer 3 primitive: persist winners as ``alphas`` rows.

    Each winner becomes a new Alpha row with parent_alpha_id pointing at
    the candidate that spawned it + ``parent_alpha_family_id`` derived
    via :func:`backend.services.optimization.family_id.derive_parent_alpha_family_id`
    + ``optimization_run_id`` linking to the open cycle.

    Returns the list of inserted alpha PKs in input order. None entries
    represent ``ON CONFLICT DO NOTHING`` skips (alpha_id collision) — kept
    in-position so SubmitPolicy can map back to its decisions cleanly.
    """

    async def save(
        self,
        winners: List[VariantSimResult],
        parent_alpha_id: int,
        opt_run_id: int,
    ) -> List[Optional[int]]:
        ...


SubmitAction = Literal["submit", "queue", "skip"]
"""SubmitPolicy decision per persisted winner.

  - ``"submit"`` — the policy MUST itself call ``alpha_service.submit_alpha``
    (caller does NOT re-submit). Stage A NEVER returns this (NEVER auto-submit
    global constraint).
  - ``"queue"`` — keep the alpha row, can_submit=True, surfaces in
    ``ops/submit-backlog`` for human review.
  - ``"skip"`` — keep the alpha row but mark can_submit=False (winners that
    aren't actually safe to submit, e.g. self_corr too high). Stage A
    doesn't produce these either — all winners are queued.
"""


class SubmitPolicy(Protocol):
    """Layer 3 primitive: decide what to do with each persisted winner.

    The decision list MUST be the same length as ``persisted_pks`` and
    correspond 1:1 by index. The policy is responsible for executing any
    ``"submit"`` decisions itself.
    """

    async def decide(
        self, persisted_pks: List[Optional[int]]
    ) -> List[SubmitAction]:
        ...


class OptimizationRunRepository(Protocol):
    """Layer 1 primitive: the cycle row-level lifecycle.

    Called by OptimizationService at cycle boundaries; Persister / SubmitPolicy
    call ``record_persist`` / ``record_submit`` mid-cycle. Used as the
    single source of truth for the GO/STOP transition gate
    (``n_winners / n_variants`` over 14d).
    """

    async def open_cycle(
        self,
        parent_alpha_id: int,
        generator_name: str,
        trigger_source: str,
        sim_budget_granted: int,
    ) -> int:
        """Insert one row with cycle_started_at=now(); return opt_run_id."""
        ...

    async def record_persist(
        self,
        opt_run_id: int,
        n_variants: int,
        n_winners: int,
        sim_spent: int,
    ) -> None:
        ...

    async def record_submit(
        self, opt_run_id: int, n_submitted: int
    ) -> None:
        ...

    async def finish_cycle(
        self,
        opt_run_id: int,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Stamp cycle_finished_at; non-None error marks abnormal exit.

        ``metadata`` (optional) is merged into ``optimization_runs.cycle_metadata``
        — used to record the RobustnessFilter's per-cycle rejections + SR0 so the
        monitor can show how many "winners" were deflated as overfit noise.
        """
        ...


class KnowledgeFeedback(Protocol):
    """Layer 3 callback: notify the knowledge base of a winner.

    Stage A + B ship a no-op implementation. Stage C wires RAG L1 pattern
    write here so winners feed back into future hypothesis generation.
    """

    async def on_winner(self, alpha: Any) -> None:
        ...
