"""Optimization closure (Stage A).

Plan: ``docs/optimization_closure_plan_v1_2026-05-28.md``.

Stage A scope:
  - SettingsSweepGenerator (10 variants per candidate)
  - StageASubmitPolicy (always "queue" — NEVER auto-submit)
  - 6h beat schedule (gated by ``ENABLE_OPTIMIZATION_LOOP``)
  - ``/ops/optimization/cycles`` telemetry

Public exports kept minimal — callers go through OptimizationService.
"""

from backend.services.optimization.protocols import (
    Variant,
    VariantSimResult,
    VariantGenerator,
    Simulator,
    WinnerSelector,
    Persister,
    SubmitPolicy,
    OptimizationRunRepository,
    KnowledgeFeedback,
)
from backend.services.optimization.service import (
    NoOpKnowledgeFeedback,
    OptimizationService,
)
# Factory keeps all its (heavy) collaborator imports deferred inside the
# function, so re-exporting it here adds no import-time cost.
from backend.services.optimization.factory import build_optimization_service

__all__ = [
    "Variant",
    "VariantSimResult",
    "VariantGenerator",
    "Simulator",
    "WinnerSelector",
    "Persister",
    "SubmitPolicy",
    "OptimizationRunRepository",
    "KnowledgeFeedback",
    "NoOpKnowledgeFeedback",
    "OptimizationService",
    "build_optimization_service",
]
