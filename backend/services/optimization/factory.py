"""Pipeline factory — build a fully-wired Stage A OptimizationService.

Both the 6h beat (``tasks/optimization_tasks._run_one``) and the manual
``POST /alphas/{id}/optimize`` path (``_run_manual``) construct the same
collaborator stack. Keeping the wiring in one place means a Stage A → B/C
generator/policy swap touches a single function instead of two call sites.

The factory takes an already-open ``db`` (AsyncSession) and ``brain``
(BrainAdapter) — both context-managed by the caller — and returns an
``OptimizationService`` ready for ``run_one_cycle``.

Imports are deferred inside the function (mirroring ``_run_one``'s pattern)
so importing this module stays cheap and test-importable even when the
optional BrainAdapter / Redis deps aren't available.

Source: ``docs/optimization_closure_plan_v1_2026-05-28.md`` §3/§4 (Layer 3
orchestrator signature frozen; only the injected impls swap A→C).
"""
from __future__ import annotations

from typing import Any


def build_optimization_service(db: Any, brain: Any) -> "Any":
    """Assemble the Stage A pipeline against an open db + BrainAdapter."""
    from backend.services.correlation_service import CorrelationService
    from backend.services.optimization.service import (
        NoOpKnowledgeFeedback,
        OptimizationService,
    )
    from backend.services.optimization.generators.settings_sweep import (
        SettingsSweepGenerator,
    )
    from backend.services.optimization.persister import Persister
    from backend.services.optimization.repository import (
        OptimizationRunRepositoryImpl,
    )
    from backend.services.optimization.simulator import BrainSimulator
    from backend.services.optimization.submit_policy import StageASubmitPolicy
    from backend.services.optimization.winner_selector import WinnerSelector

    from backend.config import settings as _cfg

    corr = CorrelationService(brain)
    repo = OptimizationRunRepositoryImpl(db)
    # 止血 (2026-06-03): wire the RobustnessFilter when the flag is ON (default).
    # Deflates sweep winners against multiple-testing + lone-peak overfitting
    # before they reach the submit-backlog.
    robustness = None
    if bool(getattr(_cfg, "OPT_ROBUSTNESS_FILTER", True)):
        from backend.services.optimization.robustness import RobustnessFilter
        robustness = RobustnessFilter()
    return OptimizationService(
        generator=SettingsSweepGenerator(
            max_variants=int(getattr(_cfg, "MAX_OPTIMIZATION_VARIANTS", 10)),
        ),
        simulator=BrainSimulator(brain),
        winner_selector=WinnerSelector(),
        persister=Persister(db, corr_service=corr, repository=repo),
        submit_policy=StageASubmitPolicy(),
        repository=repo,
        feedback=NoOpKnowledgeFeedback(),
        robustness=robustness,
    )
