"""
LangGraph Mining Workflow
Orchestrates the complete alpha mining state graph
"""

from typing import Dict, List, Optional, Any, Annotated
from functools import partial
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from backend.agents.graph.state import MiningState, AlphaResult, FailureRecord
from backend.agents.graph.nodes import (
    node_rag_query,
    node_distill_context,
    node_hypothesis,
    node_code_gen,
    node_validate,
    node_self_correct,
    node_simulate,
    node_evaluate,
    node_save_results
)
from backend.agents.graph.edges import (
    route_after_validate,
)
from backend.agents.services import LLMService, RAGService, get_llm_service
from backend.adapters.brain_adapter import BrainAdapter
from backend.config import settings
from backend.models import MiningTask


class MiningWorkflow:
    """
    LangGraph-based mining workflow.
    
    Features:
    - Strongly typed state (Pydantic)
    - Conditional edges for self-correction loops
    - Full trace recording
    - Configurable checkpointing
    """
    
    def __init__(
        self,
        db: AsyncSession,
        brain: BrainAdapter = None,
        llm_service: LLMService = None,
        checkpointer: Optional[BaseCheckpointSaver] = None
    ):
        self.db = db
        self.brain = brain or BrainAdapter()
        self.llm_service = llm_service or get_llm_service()
        self.rag_service = RAGService(db)
        self.checkpointer = checkpointer
        
        # Pipeline sub-graphs: the pool consumer runs simulate then evaluate
        # per candidate (built once); the producer runs the generation split.
        self._sim_graph = self._build_simulate_graph()
        self._eval_graph = self._build_evaluate_graph()
        self._validate_graph = None  # validate → END (G5 offspring check)
        # Generation split at HYPOTHESIS (built lazily). Concatenated they ARE
        # the full generation graph, so the split carries no semantic drift.
        self._hyp_graph = None       # rag → distill → hypothesis → END (stage 1)
        self._codegen_graph = None   # code_gen → validate → [self_correct] (stage 2)

        logger.info("[MiningWorkflow] Initialized")
    
    def _build_simulate_graph(self) -> StateGraph:
        """Single-node graph (simulate→END) for the pipeline consumer.

        Reuses the identical node_simulate binding so per-candidate sims behave
        exactly like the in-round batch sim (DB dedup, slot claim, delay
        overrides). node_simulate manages its OWN ephemeral DB sessions, so
        concurrent consumers never share a connection.
        """
        g = StateGraph(MiningState)
        g.add_node("simulate", partial(node_simulate, brain=self.brain))
        g.set_entry_point("simulate")
        g.add_edge("simulate", END)
        return g

    def _build_evaluate_graph(self) -> StateGraph:
        """Single-node graph (evaluate→END) for the pipeline consumer.

        node_evaluate writes verdicts onto pending_alphas and (when trace_service
        is None, as the consumer passes) records trace in-memory only; its Q10/
        R1a/PR06 side-channels each open their own ephemeral session, so it is
        concurrent-safe. Persistence (save_results) is intentionally NOT here —
        the pipeline persister owns it.
        """
        g = StateGraph(MiningState)
        g.add_node("evaluate", partial(node_evaluate, brain=self.brain))
        g.set_entry_point("evaluate")
        g.add_edge("evaluate", END)
        return g

    async def run_simulate(self, state, config: Dict[str, Any] = None):
        """Run node_simulate on a (single-candidate) state; return final state."""
        return await self._sim_graph.compile().ainvoke(state, config=config)

    async def run_evaluate(self, state, config: Dict[str, Any] = None):
        """Run node_evaluate on a (post-sim) state; return final state."""
        return await self._eval_graph.compile().ainvoke(state, config=config)

    def _build_validate_graph(self) -> StateGraph:
        """validate → END for the F2-4 G5 offspring check.

        The crossover handler builds offspring AlphaCandidates directly (no LLM
        generation), puts them on a copied parent state, and runs validate so
        only syntactically/semantically valid offspring are re-simulated. Reuses
        the identical node_validate binding as the generation graph.
        """
        g = StateGraph(MiningState)
        g.add_node("validate", node_validate)
        g.set_entry_point("validate")
        g.add_edge("validate", END)
        return g

    async def run_validate(self, state, config: Dict[str, Any] = None):
        """Validate a state's pending_alphas (F2-4 G5); return final state with
        is_valid set on each. The caller re-simulates only the valid ones."""
        if self._validate_graph is None:
            self._validate_graph = self._build_validate_graph()
        return await self._validate_graph.compile().ainvoke(state, config=config)

    def _build_hyp_graph(self) -> StateGraph:
        """rag_query → distill_context → hypothesis → END (Sub-phase 3 stage 1).

        Same bindings as the generation graph's head, ending after hypothesis.
        node_rag_query uses self.rag_service (this workflow's DB session); the
        hypothesis hooks (G8 / Hypothesis INSERT) open their own ephemeral
        sessions — so a stage-1 hyp-producer owns one DB session, the F1 DB-owner.
        """
        g = StateGraph(MiningState)
        g.add_node("rag_query", partial(node_rag_query, rag_service=self.rag_service))
        g.add_node("distill_context", partial(node_distill_context, llm_service=self.llm_service))
        g.add_node("hypothesis", partial(node_hypothesis, llm_service=self.llm_service))
        g.set_entry_point("rag_query")
        g.add_edge("rag_query", "distill_context")
        g.add_edge("distill_context", "hypothesis")
        g.add_edge("hypothesis", END)
        return g

    def _build_codegen_graph(self) -> StateGraph:
        """code_gen → validate → [self_correct → validate]* → END (stage 2).

        Same bindings as the generation graph's tail (incl. the post-validate
        self_correct loop). node_code_gen / node_validate / node_self_correct are
        DB-free w.r.t. a shared session (code_gen's G8/bandit/G10 hooks each open
        their own ephemeral session; validate loads the operator registry at
        import) — so N stage-2 code-producers run concurrently DB-free, like the
        sim consumers.
        """
        g = StateGraph(MiningState)
        g.add_node("code_gen", partial(node_code_gen, llm_service=self.llm_service))
        g.add_node("validate", node_validate)
        g.add_node("self_correct", partial(node_self_correct, llm_service=self.llm_service))
        g.set_entry_point("code_gen")
        g.add_edge("code_gen", "validate")
        g.add_conditional_edges(
            "validate",
            route_after_validate,
            {"simulate": END, "self_correct": "self_correct"},
        )
        g.add_edge("self_correct", "validate")
        return g

    async def run_hypothesis(self, state, config: Dict[str, Any] = None):
        """Run stage 1 (rag→distill→hypothesis) → state carrying the hypothesis +
        RAG context, ready for code_gen. The hypothesis SOURCE seam (Sub-phase 3):
        a pluggable producer (e.g. paper-derived) could emit equivalent states."""
        if self._hyp_graph is None:
            self._hyp_graph = self._build_hyp_graph()
        return await self._hyp_graph.compile().ainvoke(state, config=config)

    async def run_codegen(self, state, config: Dict[str, Any] = None):
        """Run stage 2 (code_gen→validate→[self_correct]) on a post-hypothesis
        state → pending_alphas (validated candidates) for the sim consumers."""
        if self._codegen_graph is None:
            self._codegen_graph = self._build_codegen_graph()
        return await self._codegen_graph.compile().ainvoke(state, config=config)

