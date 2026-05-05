"""
LangGraph State Definitions
Strongly typed state using Pydantic for the mining workflow
"""

from typing import List, Dict, Optional, Any, Annotated
from pydantic import BaseModel, Field
from datetime import datetime
from operator import add


# =============================================================================
# DATA STRUCTURES
# =============================================================================

class AlphaCandidate(BaseModel):
    """A candidate alpha expression to be validated and simulated."""
    expression: str
    hypothesis: Optional[str] = None
    explanation: Optional[str] = None
    expected_sharpe: Optional[float] = None

    # Validation state
    is_valid: Optional[bool] = None
    validation_error: Optional[str] = None

    # Simulation state
    is_simulated: bool = False
    simulation_success: Optional[bool] = None
    alpha_id: Optional[str] = None
    metrics: Dict = Field(default_factory=dict)
    simulation_error: Optional[str] = None

    # Correction state
    correction_attempts: int = 0
    original_expression: Optional[str] = None  # If corrected

    # Evaluation state
    quality_status: str = "PENDING"  # PASS, FAIL, PENDING

    # Tier system: parent alphas.id when this candidate is a T2/T3 wrapping
    # of a prior-tier seed. None for T1 candidates and legacy AUTONOMOUS.
    parent_alpha_id: Optional[int] = None
    # Generation provenance: e.g. "group_neutralize_industry" / "rank" /
    # "trade_when_high_volume_entry". Used by post-task analytics to compute
    # wrapper-kind yield rates.
    wrapper_kind: Optional[str] = None

    # Additional metadata for tracking
    metadata: Dict = Field(default_factory=dict)


class AlphaResult(BaseModel):
    """Final result for a processed alpha."""
    expression: str
    hypothesis: Optional[str] = None
    explanation: Optional[str] = None
    alpha_id: Optional[str] = None
    metrics: Dict = Field(default_factory=dict)
    quality_status: str = "PENDING"  # PASS, REJECT, PENDING
    trace_step_id: Optional[int] = None
    # Tier system fields propagated to the Alpha DB row
    parent_alpha_id: Optional[int] = None
    wrapper_kind: Optional[str] = None
    # PR7 — set True by node_save_results when it has already INSERTed the
    # corresponding Alpha row (T2/T3 incremental persistence path).
    # workflow.run_with_persistence checks this and skips its own batch
    # write to avoid duplicating the row.
    persisted: bool = False
    db_id: Optional[int] = None  # alphas.id when persisted=True
    # Plan v5+ §Phase 2 B4: typed Hypothesis link. None when level<2 or
    # propose persistence failed; populated by node_save_results from
    # state.current_hypothesis_id at the moment the alpha was generated.
    hypothesis_id: Optional[int] = None


class FailureRecord(BaseModel):
    """Record of a failed alpha attempt."""
    expression: str
    error_type: str
    error_message: str
    trace_step_id: Optional[int] = None


class TraceStepData(BaseModel):
    """Trace step data for state accumulation."""
    step_type: str
    step_order: int
    input_data: Dict = Field(default_factory=dict)
    output_data: Dict = Field(default_factory=dict)
    duration_ms: int = 0
    status: str = "SUCCESS"
    error_message: Optional[str] = None


# =============================================================================
# MAIN STATE
# =============================================================================

class MiningState(BaseModel):
    """
    Main state for the mining workflow graph.
    
    Designed for:
    - Strong typing with Pydantic
    - Immutable updates (return new state)
    - Full traceability
    """
    
    # -------------------------------------------------------------------------
    # Task Context (immutable after init)
    # -------------------------------------------------------------------------
    task_id: int
    region: str = "USA"
    universe: str = "TOP3000"
    dataset_id: str = ""
    
    # Context data
    fields: List[Dict] = Field(default_factory=list)
    operators: List[Dict] = Field(default_factory=list)
    num_alphas_target: int = 3
    
    # -------------------------------------------------------------------------
    # RAG Results
    # -------------------------------------------------------------------------
    patterns: List[Dict] = Field(default_factory=list)
    pitfalls: List[Dict] = Field(default_factory=list)
    dataset_description: str = ""
    dataset_category: str = ""
    
    # Distillation Results
    distilled_concepts: List[str] = Field(default_factory=list)
    focused_fields: List[Dict] = Field(default_factory=list)

    hypotheses: List[Dict] = Field(default_factory=list)

    # -------------------------------------------------------------------------
    # Plan v5+ §Phase 1 cross-dataset hypothesis (HGE Level 1+)
    # -------------------------------------------------------------------------
    # available_dataset_pool: dataset_ids the LLM may choose from when forming
    #   a hypothesis. mining_tasks._get_datasets_to_mine populates this with
    #   [anchor, top-K complementary] before each evolution_loop call. Empty
    #   list = legacy single-anchor behavior (pre-Phase 1).
    # current_hypothesis_datasets: dataset_ids the LLM picked for THIS round's
    #   hypothesis. node_code_gen unions their field pools when generating
    #   candidate expressions. When empty, code_gen falls back to state.fields
    #   (anchor-only, legacy path).
    available_dataset_pool: List[str] = Field(default_factory=list)
    current_hypothesis_datasets: List[str] = Field(default_factory=list)
    # current_hypothesis_fields: union of fields across current_hypothesis_datasets
    # populated by node_hypothesis when Phase 1 active. Downstream nodes
    # (t1_strategy_select / code_gen) prefer this over state.fields when
    # non-empty, so the LLM strategy / code-gen sees the union pool.
    current_hypothesis_fields: List[Dict] = Field(default_factory=list)

    # -------------------------------------------------------------------------
    # Plan v5+ §Phase 2 typed hypothesis (HGE Level 2+)
    # -------------------------------------------------------------------------
    # current_hypothesis_id: PK of the Hypothesis row INSERT-ed by
    #   node_hypothesis_propose (Phase 2 B3). Downstream Alpha rows carry
    #   this in their hypothesis_id FK so cross-round accumulation +
    #   lifecycle / KB attribution become possible. None = legacy / Phase 1
    #   path didn't persist a typed Hypothesis.
    # current_hypothesis_ids: full list of hypothesis IDs proposed THIS round
    #   (B3 may persist 1-3 hypotheses per round, one per LLM-emitted item).
    #   alphas downstream link to current_hypothesis_id (the primary), but
    #   B5 feedback iterates the list to update lifecycle on all of them.
    current_hypothesis_id: Optional[int] = None
    current_hypothesis_ids: List[int] = Field(default_factory=list)
    
    # -------------------------------------------------------------------------
    # Alpha Processing Queue
    # -------------------------------------------------------------------------
    pending_alphas: List[AlphaCandidate] = Field(default_factory=list)
    current_alpha: Optional[AlphaCandidate] = None
    current_alpha_index: int = 0
    
    # -------------------------------------------------------------------------
    # Self-Correction Loop Control
    # -------------------------------------------------------------------------
    retry_count: int = 0
    max_retries: int = 3
    
    # -------------------------------------------------------------------------
    # Outputs (accumulated)
    # -------------------------------------------------------------------------
    generated_alphas: List[AlphaResult] = Field(default_factory=list)
    failures: List[FailureRecord] = Field(default_factory=list)
    
    # -------------------------------------------------------------------------
    # Trace (accumulated)
    # -------------------------------------------------------------------------
    step_order: int = 0
    trace_steps: List[TraceStepData] = Field(default_factory=list)
    
    # -------------------------------------------------------------------------
    # Round-level history (W1: round-level early-stop / median pruner)
    # -------------------------------------------------------------------------
    # Each entry: {round_index, pass_rate, mean_score, best_sharpe,
    #              pass_count, optimize_count, fail_count, alphas_count}
    round_history: List[Dict] = Field(default_factory=list)
    current_round: int = 0
    early_stopped: bool = False
    early_stop_reason: Optional[str] = None
    multi_fidelity_enabled: bool = False  # W4 nice-to-have

    # -------------------------------------------------------------------------
    # Tier system (T1/T2/T3 factor library) — populated by router from agent_mode
    # -------------------------------------------------------------------------
    # factor_tier: 1 = T1 (LLM-guided field/op selection),
    #              2 = T2 (wrap T1 PASS seeds with cross-sectional / smoothing),
    #              3 = T3 (wrap T2 PASS seeds with trade_when filters).
    # Default 1 keeps the legacy AUTONOMOUS path classified as T1 when
    # ENABLE_FACTOR_TIERING is on.
    factor_tier: int = 1
    # current_strategy: Pydantic model serialized via .model_dump(); T1 stores
    # T1Strategy, T2/T3 store T{2,3}Strategy. Consumers reconstitute the typed
    # object before passing to expand_*_strategy.
    current_strategy: Optional[Dict] = None
    # T2/T3 only: seeds loaded once at task start by node_tier_seed_load.
    # Each entry: {alpha_id, expression, region, dataset_id, metrics, snapshot_at}
    tier_seeds: List[Dict] = Field(default_factory=list)
    current_seed_index: int = 0
    current_seed: Optional[Dict] = None

    # -------------------------------------------------------------------------
    # Control Flags
    # -------------------------------------------------------------------------
    should_stop: bool = False
    error: Optional[str] = None
    
    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------
    
    def increment_step(self) -> int:
        """Get next step order (for external use, state is immutable)."""
        return self.step_order + 1
    
    def has_more_alphas(self) -> bool:
        """Check if there are more alphas to process."""
        return self.current_alpha_index < len(self.pending_alphas)
    
    def get_current_alpha(self) -> Optional[AlphaCandidate]:
        """Get current alpha from queue."""
        if self.current_alpha_index < len(self.pending_alphas):
            return self.pending_alphas[self.current_alpha_index]
        return None
    
    class Config:
        """Pydantic config."""
        validate_assignment = True


# =============================================================================
# STATE UPDATE HELPERS
# =============================================================================

def merge_state(state: MiningState, updates: Dict) -> Dict:
    """
    Create a partial state update dict.
    Used in node functions to return updates.
    """
    return updates


def add_trace_step(
    state: MiningState,
    step_type: str,
    input_data: Dict = None,
    output_data: Dict = None,
    duration_ms: int = 0,
    status: str = "SUCCESS",
    error_message: str = None
) -> Dict:
    """
    Create a trace step and return state update.
    """
    new_step = TraceStepData(
        step_type=step_type,
        step_order=state.step_order + 1,
        input_data=input_data or {},
        output_data=output_data or {},
        duration_ms=duration_ms,
        status=status,
        error_message=error_message
    )
    
    return {
        "step_order": state.step_order + 1,
        "trace_steps": state.trace_steps + [new_step]
    }
