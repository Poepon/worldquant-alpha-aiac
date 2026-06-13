"""
Models Module - Database entities

This module provides all SQLAlchemy models for the application.
Models are organized into separate files by domain but re-exported
here for backward compatibility.

Usage:
    from backend.models import Alpha, MiningTask, KnowledgeEntry
"""

# Enums
from backend.models.base import (
    MiningStatus,
    DatasetStrategy,
    TraceStepType,
    QualityStatus,
    HumanFeedback,
    KnowledgeEntryType,
    JobStatus,
    HypothesisStatus,
    HypothesisKind,
)

# Task models
from backend.models.task import (
    MiningTask,
    # ExperimentRun retired in Phase 1d
    TraceStep,
    MiningJob,
)

# Alpha models
from backend.models.alpha import (
    Alpha,
    AlphaFailure,
    AlphaPnl,
    AutoSubmitAudit,
)

# Status transition audit
from backend.models.transition import (
    AlphaStatusTransition,
    HypothesisStatusTransition,
)

# Hypothesis (Phase 2 B1) + per-round stats (V-27.92)
from backend.models.hypothesis import Hypothesis, HypothesisRoundStats

# Knowledge models
from backend.models.knowledge import (
    KnowledgeEntry,
    OperatorPreference,
    RLState,
    RLAction,
    BanditState,
    compute_pattern_hash,
)

# Metadata models
from backend.models.metadata import (
    DatasetMetadata,
    DatasetCellStats,
    DataField,
    DataFieldCellStats,
    Operator,
    OperatorBlacklist,
    Region,
    Universe,
    Neutralization,
    PyramidMultiplier,
    Template,
    TemplateVariable,
)

# Config models
from backend.models.config import (
    SystemConfig,
    BrainAuthToken,
    WQBCredential,
    LLMProvider,
    FeatureFlagOverride,
    FeatureFlagAudit,
)

# R1a attribution log (Phase 0 v1.6 fix — independent of alpha persistence)
from backend.models.r1a_attribution import R1aAttributionLog

# DirectionBandit off-policy log (Phase 1 R2/Q7 — independent of task.config)
from backend.models.direction_bandit_log import DirectionBanditLog

# AST distance log (Phase 1 R3/Q8 — independent of alpha persistence per
# R1a v1.6 lesson [[feedback_r1a_dedicated_log_table]])
from backend.models.ast_distance_log import AstDistanceLog

# Q10 pyqlib pre-screen log (Phase 3 Q10 PR1b — one row per prescreen_alpha
# call; dedicated table per same R1a lesson)
from backend.models.qlib_prescreen_log import QlibPrescreenLog

# R8 query-level telemetry (per-call layer_hits + cache_hit row, flag-gated)
from backend.models.r8_query_log import R8QueryLog

# G2 Phase A per-call LLM cost telemetry (flag-gated, batched flush at round
# boundary; dedicated per [[feedback_r1a_dedicated_log_table]])
from backend.models.llm_call_log import LLMCallLog

# G5 Phase A trajectory crossover log (per-LLM-call row + outcome alpha
# back-fill; dedicated per same rationale)
from backend.models.g5_crossover_log import G5CrossoverLog

# Pitfall-classifier per-call decision log (feedback_agent
# _classify_pitfall_error_type — drop vs stamp telemetry)
from backend.models.classifier_call_log import ClassifierCallLog

# Phase 4 Sprint 1 A2 — R14 task_stop_loss trigger event log (Millennium
# 5%/7.5% hard stop-loss pattern; dedicated table per
# [[feedback_r1a_dedicated_log_table]])
from backend.models.task_stop_loss_event import TaskStopLossEvent

# Phase 4 Sprint 2 B2 — R13 factor_lens OLS decomposition per PASS alpha
# (Two Sigma 18-factor lens / AQR autoencoder asset pricing pattern)
from backend.models.factor_lens_residual import FactorLensResidual

# Phase 4 Sprint 3 A5.1 G10 — distilled_logic_library (LLM weekly summary
# of common logic across past 7d PASS alphas grouped by pillar, region)
from backend.models.distilled_logic import DistilledLogic

# Phase 4 Tier E E1 — cognitive_layer_bandit_state (per-layer Beta-Bernoulli
# posterior for R8-v3 'bandit' select mode; weekly cron updates pass/fail)
from backend.models.cognitive_layer_bandit import CognitiveLayerBanditState

# Phase 16-A optimization closure (Stage A, 2026-05-28) — one row per cycle.
from backend.models.optimization import OptimizationRun

# Mining pipeline queues (four-pool decoupling Phase 0, 2026-06-05) — DB-persistent
# claim/lease queues for the resident HG / Simulate / Evaluate pools. INERT until
# Phase 1b wires the pools; see docs/four_pool_decoupling_plan_2026-06-05.md.
from backend.models.pipeline import (
    HypothesisIntent,
    CandidateQueue,
)

__all__ = [
    # Enums
    "MiningStatus",
    "DatasetStrategy",
    "TraceStepType",
    "QualityStatus",
    "HumanFeedback",
    "KnowledgeEntryType",
    "JobStatus",
    "HypothesisStatus",
    "HypothesisKind",
    # Task
    "MiningTask",
    "TraceStep",
    "MiningJob",
    # Alpha
    "Alpha",
    "AlphaFailure",
    "AlphaPnl",
    "AutoSubmitAudit",
    "AlphaStatusTransition",
    "HypothesisStatusTransition",
    # Hypothesis (Phase 2)
    "Hypothesis",
    "HypothesisRoundStats",
    # Knowledge
    "KnowledgeEntry",
    "OperatorPreference",
    "RLState",
    "RLAction",
    "BanditState",
    "compute_pattern_hash",
    # Metadata
    "DatasetMetadata",
    "DatasetCellStats",
    "DataField",
    "DataFieldCellStats",
    "Operator",
    "OperatorBlacklist",
    "Region",
    "Universe",
    "Neutralization",
    "PyramidMultiplier",
    "Template",
    "TemplateVariable",
    # Config
    "SystemConfig",
    "BrainAuthToken",
    "WQBCredential",
    "LLMProvider",
    "FeatureFlagOverride",
    "FeatureFlagAudit",
    # Optimization closure
    "OptimizationRun",
    # Mining pipeline queues (four-pool decoupling Phase 0)
    "HypothesisIntent",
    "CandidateQueue",
]
