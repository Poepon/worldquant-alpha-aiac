"""
Prompt Templates for Alpha Mining - Backward Compatibility Module

This module re-exports all prompt templates from the modular prompts/ package.
For new code, prefer importing directly from backend.agents.prompts.

Module Organization:
- prompts/base.py: Data classes (PromptContext) and helper functions
- prompts/generation.py: Alpha generation prompts
- prompts/hypothesis.py: Hypothesis and distillation prompts
- prompts/validation.py: Self-correction and optimization prompts
- prompts/analysis.py: Round and failure analysis prompts
- prompts/legacy.py: Legacy templates for backward compatibility
"""

# Re-export everything from the prompts package for backward compatibility
from backend.agents.prompts import (
    # Base
    PromptContext,
    build_fields_context,
    build_operators_context,
    build_patterns_context,
    build_strategy_constraints,
    # Generation
    ALPHA_GENERATION_SYSTEM,
    build_alpha_generation_prompt,
    # Hypothesis
    HYPOTHESIS_SYSTEM,
    DISTILL_SYSTEM,
    build_hypothesis_prompt,
    build_distill_prompt,
    # Validation
    SELF_CORRECT_SYSTEM,
    build_self_correct_prompt,
    # Analysis
    ROUND_ANALYSIS_SYSTEM,
    FAILURE_ANALYSIS_SYSTEM,
    build_round_analysis_prompt,
    FAILURE_ANALYSIS_USER,
    # Legacy
    DISTILL_USER,
    HYPOTHESIS_USER,
    ALPHA_GENERATION_USER,
    SELF_CORRECT_USER,
    ROUND_ANALYSIS_USER,
)

__all__ = [
    # Base
    "PromptContext",
    "build_fields_context",
    "build_operators_context",
    "build_patterns_context",
    "build_strategy_constraints",
    # Generation
    "ALPHA_GENERATION_SYSTEM",
    "build_alpha_generation_prompt",
    # Hypothesis
    "HYPOTHESIS_SYSTEM",
    "DISTILL_SYSTEM",
    "build_hypothesis_prompt",
    "build_distill_prompt",
    # Validation
    "SELF_CORRECT_SYSTEM",
    "build_self_correct_prompt",
    # Analysis
    "ROUND_ANALYSIS_SYSTEM",
    "FAILURE_ANALYSIS_SYSTEM",
    "build_round_analysis_prompt",
    "FAILURE_ANALYSIS_USER",
    # Legacy
    "DISTILL_USER",
    "HYPOTHESIS_USER",
    "ALPHA_GENERATION_USER",
    "SELF_CORRECT_USER",
    "ROUND_ANALYSIS_USER",
]
