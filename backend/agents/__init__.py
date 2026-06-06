"""
AIAC 2.0 Agents Package
Contains Mining Agent, Feedback Agent, and LangGraph components
"""

# Phase 1c-delete: MiningAgent (FLAT/ONESHOT executor) retired. FeedbackAgent
# survives — consumed by the daily-feedback beat (tasks/feedback_tasks.py).
from backend.agents.feedback_agent import FeedbackAgent
from backend.agents.graph import (
    MiningWorkflow,
    MiningState,
    create_mining_graph
)

__all__ = [
    # Feedback agent (daily beat)
    "FeedbackAgent",
    # LangGraph
    "MiningWorkflow",
    "MiningState",
    "create_mining_graph",
]
