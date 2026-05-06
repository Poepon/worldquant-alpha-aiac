"""
Integration module - connects new core architecture with existing workflow.

This module provides:
1. Factory functions to create pipeline components
2. Adapters to integrate with existing LangGraph workflow
3. Migration helpers for gradual adoption

Usage:
    # Create complete pipeline
    pipeline = create_alpha_pipeline(llm_service, brain_adapter, scen)
    
    # Run with new architecture
    trace = ExperimentTrace(dataset_id, region, universe)
    results = await pipeline.run_multiple(trace, num_experiments=3)
    
    # Or integrate with existing workflow
    state = MiningState(...)
    enhanced_state = await run_with_trace(state, pipeline)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING
import uuid

from backend.agents.core.experiment import (
    AlphaExperiment,
    ExperimentStatus,
    Hypothesis,
)
from backend.agents.core.feedback import HypothesisFeedback
from backend.agents.core.knowledge import QueriedKnowledge
from backend.agents.core.scenario import (
    AlphaMiningScenario,
    DatasetContext,
    OperatorContext,
)
from backend.agents.core.trace import ExperimentTrace
from backend.agents.core.pipeline import (
    AlphaMiningPipeline,
    LLMHypothesisGen,
    LLMHypothesis2Experiment,
    BRAINExperimentRunner,
    LLMExperiment2Feedback,
    PipelineResult,
)
from backend.agents.core.evolving_rag import AlphaRAGStrategy

if TYPE_CHECKING:
    from backend.agents.services.llm_service import LLMService
    from backend.adapters.brain_adapter import BrainAdapter
    from backend.agents.graph.state import MiningState


# =============================================================================
# Factory Functions
# =============================================================================

def create_scenario(
    region: str = "USA",
    universe: str = "TOP3000",
    dataset_id: str = "",
    fields: List[Dict] = None,
    operators: List[Dict] = None
) -> AlphaMiningScenario:
    """
    Create scenario from parameters.
    
    Args:
        region: Trading region (USA, CHN, etc.)
        universe: Stock universe (TOP3000, TOP500, etc.)
        dataset_id: Dataset identifier
        fields: Available data fields
        operators: Available operators
    """
    dataset_ctx = DatasetContext(
        dataset_id=dataset_id,
        fields=fields or []
    )
    
    operator_ctx = OperatorContext(
        operators=operators or []
    )
    
    return AlphaMiningScenario(
        region=region,
        universe=universe,
        dataset_context=dataset_ctx,
        operator_context=operator_ctx
    )


def create_alpha_pipeline(
    llm_service: 'LLMService',
    brain_adapter: 'BrainAdapter',
    scenario: AlphaMiningScenario
) -> AlphaMiningPipeline:
    """
    Create complete alpha mining pipeline.
    
    Args:
        llm_service: LLM service for generation
        brain_adapter: BRAIN platform adapter
        scenario: Task scenario
        
    Returns:
        Complete AlphaMiningPipeline
    """
    # Create components
    hypothesis_gen = LLMHypothesisGen(scenario, llm_service)
    h2e = LLMHypothesis2Experiment(scenario, llm_service)
    runner = BRAINExperimentRunner(brain_adapter, scenario)
    e2f = LLMExperiment2Feedback(scenario, llm_service)
    
    return AlphaMiningPipeline(
        hypothesis_gen=hypothesis_gen,
        h2e=h2e,
        runner=runner,
        e2f=e2f
    )


def create_trace(
    dataset_id: str,
    region: str = "USA",
    universe: str = "TOP3000",
    knowledge_base_path: Optional[str] = None
) -> ExperimentTrace:
    """
    Create experiment trace with optional knowledge base.
    
    Args:
        dataset_id: Dataset being mined
        region: Trading region
        universe: Stock universe
        knowledge_base_path: Path to persist knowledge
        
    Returns:
        ExperimentTrace ready for experiments
    """
    from backend.agents.core.knowledge import EvolvingKnowledge
    from pathlib import Path
    import pickle
    
    # Load or create knowledge base
    knowledge = None
    if knowledge_base_path:
        path = Path(knowledge_base_path)
        if path.exists():
            try:
                with open(path, "rb") as f:
                    knowledge = pickle.load(f)
            except Exception:
                pass
    
    if knowledge is None:
        knowledge = EvolvingKnowledge()
    
    return ExperimentTrace(
        dataset_id=dataset_id,
        region=region,
        universe=universe,
        knowledge_base=knowledge
    )


# =============================================================================
# State Adapters
# =============================================================================

def experiment_to_alpha_result(experiment: AlphaExperiment) -> Dict[str, Any]:
    """
    Convert AlphaExperiment to legacy AlphaResult format.
    
    This allows new experiments to be persisted by existing workflow.
    """
    return {
        "alpha_id": experiment.alpha_id,
        "expression": experiment.expression,
        "hypothesis": experiment.hypothesis.statement if experiment.hypothesis else "",
        "explanation": experiment.explanation,
        "metrics": experiment.metrics,
        "quality_status": experiment.quality_status,
        "is_simulated": experiment.status == ExperimentStatus.COMPLETED,
    }


def feedback_to_dict(feedback: HypothesisFeedback) -> Dict[str, Any]:
    """Convert HypothesisFeedback to dictionary."""
    return feedback.to_dict()


def state_to_trace(state: 'MiningState') -> ExperimentTrace:
    """
    Convert MiningState to ExperimentTrace.
    
    This allows existing state to be used with new pipeline.
    """
    trace = ExperimentTrace(
        dataset_id=state.dataset_id,
        region=state.region,
        universe=state.universe
    )
    
    # Import existing results if any
    for alpha in state.generated_alphas:
        experiment = AlphaExperiment(
            id=str(uuid.uuid4())[:8],
            expression=alpha.expression,
            alpha_id=alpha.alpha_id,
            metrics=alpha.metrics,
            quality_status=alpha.quality_status,
            status=ExperimentStatus.COMPLETED if alpha.is_simulated else ExperimentStatus.PENDING,
            dataset_id=state.dataset_id,
            region=state.region,
            universe=state.universe,
        )
        
        # Create basic feedback
        feedback = HypothesisFeedback(
            observations=f"Alpha completed with status {alpha.quality_status}",
            hypothesis_evaluation="",
            decision=alpha.quality_status == "PASS",
            reason=f"Quality status: {alpha.quality_status}",
        )
        
        trace.add_experiment(experiment, feedback)
    
    return trace


# =============================================================================
# Enhanced Workflow Integration
# =============================================================================

@dataclass
class EnhancedMiningResult:
    """Result from enhanced mining iteration."""
    experiments: List[AlphaExperiment]
    feedbacks: List[HypothesisFeedback]
    trace: ExperimentTrace
    knowledge_rules_added: int
    
    def to_legacy_format(self) -> Dict[str, Any]:
        """Convert to format expected by existing persistence."""
        return {
            "generated_alphas": [
                experiment_to_alpha_result(exp) for exp in self.experiments
            ],
            "failures": [
                {
                    "expression": exp.expression,
                    "error_type": exp.error_type,
                    "error_message": exp.error_message,
                }
                for exp in self.experiments
                if exp.status == ExperimentStatus.FAILED
            ],
            "trace_stats": self.trace.get_stats(),
        }


async def run_enhanced_mining(
    llm_service: 'LLMService',
    brain_adapter: 'BrainAdapter',
    dataset_id: str,
    fields: List[Dict],
    operators: List[Dict],
    region: str = "USA",
    universe: str = "TOP3000",
    num_experiments: int = 3,
    existing_trace: Optional[ExperimentTrace] = None
) -> EnhancedMiningResult:
    """
    Run enhanced mining with new architecture.

    Status (2026-05-06): DORMANT — designated as the Plan v5+ §C-Phase 3
    main-loop inversion entry point (hypothesis-as-driver instead of
    dataset-as-driver), but not wired into Celery / mining_tasks. Plan
    v5 Final §三轮精简 pushed Phase 3 to Q3 (2026-07-09); see
    docs/phase3_evaluation_2026-05-06.md for the launch gates.

    When Phase 3 is greenlit, this function becomes the production entry
    via mining_tasks.py routing on `task.config.hypothesis_centric_variant=3`.
    Until then it remains importable for ad-hoc experiments / R&D scripts
    only — not safe for Celery worker threads (no DB session injected).

    Args:
        llm_service: LLM service
        brain_adapter: BRAIN adapter
        dataset_id: Dataset to mine
        fields: Available fields
        operators: Available operators
        region: Trading region
        universe: Stock universe
        num_experiments: Number of experiments to run
        existing_trace: Optional existing trace to continue

    Returns:
        EnhancedMiningResult with experiments and feedback
    """
    # Create scenario
    scenario = create_scenario(
        region=region,
        universe=universe,
        dataset_id=dataset_id,
        fields=fields,
        operators=operators
    )
    
    # Create or use existing trace
    trace = existing_trace or create_trace(
        dataset_id=dataset_id,
        region=region,
        universe=universe
    )
    
    # Create pipeline
    pipeline = create_alpha_pipeline(llm_service, brain_adapter, scenario)
    
    # Run experiments
    results = await pipeline.run_multiple(trace, num_experiments=num_experiments)
    
    # Create RAG strategy for knowledge generation
    rag = AlphaRAGStrategy()
    new_rules = rag.generate_knowledge(trace)
    
    return EnhancedMiningResult(
        experiments=[r.experiment for r in results],
        feedbacks=[r.feedback for r in results],
        trace=trace,
        knowledge_rules_added=len(new_rules)
    )


# =============================================================================
# Gradual Migration Helpers
# =============================================================================

def enhance_existing_node_evaluate(
    alpha,  # Existing Alpha model
    sim_result: Dict[str, Any],
    hypothesis_dict: Dict[str, Any],
    trace: Optional[ExperimentTrace] = None
) -> HypothesisFeedback:
    """
    Enhanced evaluation that can be used in existing node_evaluate.
    
    This allows gradual adoption - existing workflow can use new feedback
    generation without full pipeline migration.
    
    Usage in node_evaluate:
        from backend.agents.core.integration import enhance_existing_node_evaluate
        
        feedback = enhance_existing_node_evaluate(
            alpha=alpha,
            sim_result=result,
            hypothesis_dict={"statement": alpha.hypothesis},
            trace=optional_trace
        )
    """
    from backend.agents.prompts.alignment import (
        quick_alignment_check,
        determine_attribution_heuristic,
    )
    from backend.agents.core.feedback import AttributionType
    
    # Quick alignment check
    is_aligned = True
    alignment_issues = []
    if hypothesis_dict.get("statement") and alpha.expression:
        is_aligned, alignment_issues = quick_alignment_check(
            hypothesis_dict,
            alpha.expression,
            []  # Fields - can be empty for basic check
        )
    
    # Determine attribution
    result_dict = {
        "sharpe": sim_result.get("sharpe"),
        "fitness": sim_result.get("fitness"),
        "passed": alpha.quality_status == "PASS",
    }
    attribution_str = determine_attribution_heuristic(
        result_dict,
        alignment_issues,
        getattr(alpha, "validation_error", None)
    )
    
    try:
        attribution = AttributionType(attribution_str)
    except ValueError:
        attribution = AttributionType.UNKNOWN
    
    # Create feedback
    return HypothesisFeedback(
        observations=f"Alpha simulated with quality_status={alpha.quality_status}",
        hypothesis_evaluation=f"Hypothesis alignment: {'aligned' if is_aligned else 'misaligned'}",
        hypothesis_supported=alpha.quality_status == "PASS",
        attribution=attribution,
        decision=alpha.quality_status == "PASS",
        reason=f"Sharpe: {sim_result.get('sharpe')}, Fitness: {sim_result.get('fitness')}",
        should_retry_implementation=attribution == AttributionType.IMPLEMENTATION,
        should_modify_hypothesis=attribution == AttributionType.HYPOTHESIS,
    )


# =============================================================================
# Usage Examples
# =============================================================================

"""
Example 1: Full pipeline usage
------------------------------

async def mine_alphas():
    from backend.agents.services import get_llm_service
    from backend.adapters.brain_adapter import BrainAdapter
    
    llm_service = get_llm_service()
    brain = BrainAdapter()
    
    async with brain:
        result = await run_enhanced_mining(
            llm_service=llm_service,
            brain_adapter=brain,
            dataset_id="fundamental6",
            fields=fields,
            operators=operators,
            num_experiments=5
        )
        
        print(f"Generated {len(result.experiments)} experiments")
        print(f"Added {result.knowledge_rules_added} knowledge rules")
        
        for exp, fb in zip(result.experiments, result.feedbacks):
            print(f"  - {exp.expression[:50]}... -> {fb.decision}")


Example 2: Gradual integration with existing workflow
-----------------------------------------------------

# In node_evaluate:
from backend.agents.core.integration import enhance_existing_node_evaluate

async def node_evaluate(state, brain, ...):
    # ... existing simulation code ...
    
    # Enhanced feedback generation
    feedback = enhance_existing_node_evaluate(
        alpha=alpha,
        sim_result=result,
        hypothesis_dict={"statement": alpha.hypothesis},
    )
    
    # Use feedback for knowledge filtering
    if feedback.should_record_to_knowledge_base():
        await rag_service.record_failure_pattern(...)


Example 3: Continue from existing trace
---------------------------------------

async def continue_mining(existing_trace_path: str):
    import pickle
    
    # Load existing trace
    with open(existing_trace_path, "rb") as f:
        trace = pickle.load(f)
    
    # Continue mining
    result = await run_enhanced_mining(
        ...,
        existing_trace=trace,
        num_experiments=3
    )
    
    # Save updated trace
    with open(existing_trace_path, "wb") as f:
        pickle.dump(result.trace, f)
"""
