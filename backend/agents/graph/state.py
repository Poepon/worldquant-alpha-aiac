"""
LangGraph State Definitions
Strongly typed state using Pydantic for the mining workflow
"""

from typing import List, Dict, Optional, Any
from pydantic import BaseModel, Field
from datetime import datetime


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
    # V-25.B (2026-05-13): typed Hypothesis link for FAIL alphas. Mirrors
    # AlphaResult.hypothesis_id (set by node_save_results from
    # state.current_hypothesis_id). Lets B5/B6 attribution span PASS + FAIL
    # via the same hypothesis_id key; previously FAIL alphas were
    # attribution-orphaned (alpha_failures had no hypothesis_id column).
    hypothesis_id: Optional[int] = None


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

    # G8 Phase A follow-up (2026-05-19): hypothesis IDs that node_hypothesis
    # surfaced to the LLM via the cross-task forest reference block. Stamped
    # into alpha.metrics["_g8_forest_referenced_ids"] by _incremental_save_alphas
    # for reverse attribution analytics ("which alphas were generated under
    # what forest prompt context"). Empty when ENABLE_HYPOTHESIS_FOREST_REUSE
    # is OFF or no rows qualified — stamp key omitted in that case.
    g8_forest_referenced_ids: List[int] = Field(default_factory=list)

    # Plan v5+ §Phase 2 B5/B6: per-hypothesis round history. Key = hypothesis_id.
    # Each entry: {round_index, alpha_count, pass_count, fail_count,
    #              syntax_fail_count, simulate_fail_count, attribution,
    #              best_sharpe}
    # V-27.92: DEMOTED to a display cache. The authoritative input for the
    # B6 abandon decision is now the hypothesis_round_stats DB table —
    # in-memory history is lost on worker restart / Celery task-boundary
    # switch and not shared across the V-20.1 prefetch round's isolated
    # session, which silently disabled abandonment. This field still feeds
    # the HYPOTHESIS_FEEDBACK trace step and the should_abandon_hypothesis_
    # from_memory() flag-off fallback path.
    # Persists across rounds within a single workflow.run() invocation; reset
    # to empty when a new task starts.
    hypothesis_round_history: Dict[int, List[Dict]] = Field(default_factory=dict)
    
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
    # Layer 1 Anti-collapse — dedup signal blacklist (2026-05-11)
    # -------------------------------------------------------------------------
    # recent_dedup_skeletons: skeletons that the pre-simulate dedup gate
    # already rejected this workflow run (DB duplicate OR portfolio self-corr
    # match). Strategy_select reads this and renders a "DO NOT REGENERATE"
    # block in the LLM prompt so the LLM stops sampling the same narrow
    # neighborhood. Accumulates across rounds within one workflow.run();
    # capped at 50 to bound prompt size. evaluation.py appends; strategy
    # nodes (t1_nodes / tier_seed) read.
    recent_dedup_skeletons: List[str] = Field(default_factory=list)
    
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
    # BRAIN Consultant mode snapshot (P3-Brain, 2026-05-16)
    # -------------------------------------------------------------------------
    # 从 MiningTask.config["brain_role_snapshot"] 透传 — task 启动时冻结当下
    # settings.effective_*,后续 round 内读快照而非 settings,保证 Consultant
    # 切换不影响 running task(数据一致性:Sharpe/testPeriod;endpoint 选择
    # 类如 multi-sim/PROD-corr 仍读全局 flag,方向 C — 见 plan §14)。
    # 全 Optional + default=None,兼容 30+ 现有测试构造点(不传也不破)+
    # 对未来启用 LangGraph checkpoint 反序列化友好(workflow.py:131 checkpointer
    # 目前未启用)。这些字段只在 task 启动时写一次,后续 round 只读 getattr。
    brain_consultant_mode_at_start: Optional[bool] = None
    effective_default_test_period: Optional[str] = None
    effective_sharpe_submit_min: Optional[float] = None
    effective_region_universes_at_start: Optional[Dict[str, str]] = None

    # ===== Phase 3 R1b CoSTEER loop counters (2026-05-18) =====
    # Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §5.1
    # All Optional / default-safe per phase15-C serialization-compat (mirror
    # brain_consultant_mode_at_start pattern above).
    # Reset boundary: per LangGraph invocation (one round of mining_tasks
    # _run_one_round_inline). Across rounds these reset to defaults.
    # Cross-round persistence handled via MiningTask.config in persistence
    # node when budget actually fired (R1b.1c wiring).
    r1b_retries_attempted_this_alpha: int = 0
    r1b_mutations_attempted_this_cycle: int = 0   # R1b.2 sub-phase uses this
    r1b_token_cost_this_alpha: float = 0.0
    # R1b.1 review LOW 2 (2026-05-18): cumulative R1b LLM cost across all
    # alphas in current round. Soft cap = settings.R1B_MAX_COST_USD_PER_ROUND
    # (default $5). Retry node checks BEFORE the LLM call; on hit it skips
    # the call + logs info (alpha not failed). Resets per LangGraph
    # invocation along with the other R1b counters above.
    r1b_cost_this_round: float = 0.0
    r1b_loop_attribution_evidence: List[Dict] = Field(default_factory=list)
    r1b_mutated_hypothesis_ids: List[int] = Field(default_factory=list)
    r1b_pending_new_hypothesis: Optional[Dict] = None

    # R1b.2-v2 (2026-05-18): consumed-side mirror of r1b_pending_new_hypothesis.
    # Populated by workflow.run from configurable when _run_one_round_inline's
    # consume_pending_hypothesis returned non-None. node_hypothesis checks this
    # at entry — if set AND ENABLE_R1B_HYPOTHESIS_MUTATE flag ON, skips the
    # exploration LLM call and uses the mutated hypothesis directly so the
    # CoSTEER loop directive flows into next round's alpha generation.
    r1b_consumed_pending_hypothesis: Optional[Dict] = None

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
