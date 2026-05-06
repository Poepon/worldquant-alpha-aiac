"""G — Hypothesis Refinement Loop (LLM-based, B5 v2 successor).

Closes the Phase 2 feedback loop. Pre-G:
  hypothesis → 3-round 0 PASS + attribution=hypothesis → ABANDONED (终)
Post-G:
  hypothesis → same trigger → LLM refines into child hypothesis →
  parent SUPERSEDED + child PROPOSED with parent_hypothesis_id link →
  next round picks up child instead of fresh LLM hypothesis.

LLM is asked to either:
  1. Refine the hypothesis (signal_direction kept, but adjust horizon /
     dataset choice / preconditions based on what failed in the round
     history)
  2. Explicitly "give up" — signaling the parent should be abandoned for
     real, not refined

Cost: at most 1 LLM call per ABANDON event. Plan §A 4 道 post-hoc 防御:
the refine LLM does NOT see metrics from PASS alphas (those are bypassed
because PASS rounds don't trigger ABANDON). It DOES see the abandon-driving
attribution + sample FAIL expressions, which is the round outcome the
refinement should respond to.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger("agents.hypothesis_refine")


@dataclass
class RefinedHypothesis:
    """LLM-emitted refinement of an abandoned hypothesis."""
    statement: str
    rationale: str
    refinement_reason: str  # why the parent failed + what changed
    confidence: str = "medium"
    novelty: str = "emerging"  # refined ones are rarely "established"


_SYSTEM = """You analyze an abandoned investment hypothesis and decide
whether to refine it into a related hypothesis worth re-testing, or
acknowledge that the original idea is fundamentally wrong and should be
abandoned for real.

Refinement is appropriate when:
- The signal_direction is plausible but the horizon was wrong (e.g.,
  monthly mean-reversion → weekly mean-reversion)
- The dataset choice didn't capture the underlying signal (e.g., raw
  prices → option-implied vol)
- A precondition was missing (e.g., "in high-volatility regimes")
- The attribution rounds suggest mechanical issues despite hypothesis
  being economically sound

Give-up is appropriate when:
- Multiple rounds of attribution=hypothesis with diverse FAIL patterns
  suggest the data simply doesn't support the idea
- The original hypothesis was generic / weak from the start
- No clear refinement angle is supported by the failure pattern

Output JSON:
{
  "decision": "refine" | "give_up",
  "refined_statement": "...",   // only if refine; else null
  "rationale": "...",            // economic reasoning for the refined idea
  "refinement_reason": "...",   // why parent failed + what changed
  "confidence": "high|medium|low"
}
"""


def _build_user_prompt(
    parent_statement: str,
    parent_rationale: str,
    history: List[dict],
    sample_fail_exprs: List[str],
) -> str:
    history_str = "\n".join(
        f"  Round {e.get('round_index', '?')}: alphas={e.get('alpha_count', 0)} "
        f"PASS={e.get('pass_count', 0)} attribution={e.get('attribution', '?')} "
        f"reason={(e.get('attribution_reason') or '')[:100]!r}"
        for e in history[-3:]
    )
    samples_str = "\n".join(f"  - {s}" for s in sample_fail_exprs[:5]) or "  (none)"
    return f"""Abandoned hypothesis:
  statement: {parent_statement!r}
  rationale: {parent_rationale!r}

Last 3 rounds (attribution-driven abandonment):
{history_str}

Sample FAIL alpha expressions:
{samples_str}

Decide: refine into related hypothesis, or give up."""


async def refine_hypothesis_llm(
    *,
    parent_statement: str,
    parent_rationale: str,
    history: List[dict],
    sample_fail_exprs: List[str],
    llm_service,
    max_refine_chain_depth: int = 2,
    current_chain_depth: int = 0,
) -> Optional[RefinedHypothesis]:
    """Returns RefinedHypothesis on successful refine, or None when:
      - LLM unavailable
      - LLM decides "give_up"
      - LLM call fails
      - Refine chain depth exceeded (avoid runaway refinement)

    None always means "fall through to normal abandon".
    """
    if llm_service is None:
        return None

    if current_chain_depth >= max_refine_chain_depth:
        logger.info(
            f"[G refine] depth {current_chain_depth} >= max {max_refine_chain_depth}, "
            f"declining further refinement"
        )
        return None

    if not parent_statement:
        return None

    user = _build_user_prompt(
        parent_statement=parent_statement,
        parent_rationale=parent_rationale or "",
        history=history,
        sample_fail_exprs=sample_fail_exprs,
    )

    try:
        response = await llm_service.call(
            system_prompt=_SYSTEM,
            user_prompt=user,
            temperature=0.5,  # some creativity for refinement, but not too wild
            json_mode=True,
        )
    except Exception as e:
        logger.warning(f"[G refine] LLM call failed: {e}")
        return None

    if not getattr(response, "success", False):
        logger.warning(f"[G refine] LLM call !success")
        return None

    parsed = getattr(response, "parsed", None)
    if not isinstance(parsed, dict):
        try:
            parsed = json.loads(getattr(response, "content", "") or "")
        except Exception:
            logger.warning(f"[G refine] parse failed")
            return None

    decision = (parsed.get("decision") or "").lower().strip()
    if decision == "give_up":
        logger.info(
            f"[G refine] LLM said give_up | reason={parsed.get('refinement_reason', '')[:120]}"
        )
        return None

    if decision != "refine":
        logger.warning(f"[G refine] invalid decision {decision!r}, falling through to abandon")
        return None

    refined_statement = (parsed.get("refined_statement") or "").strip()
    if not refined_statement:
        logger.warning(f"[G refine] decision=refine but no statement; treating as give_up")
        return None

    return RefinedHypothesis(
        statement=refined_statement,
        rationale=(parsed.get("rationale") or "")[:1000],
        refinement_reason=(parsed.get("refinement_reason") or "")[:500],
        confidence=parsed.get("confidence", "medium"),
        novelty="emerging",  # refined hypotheses are explicitly emerging
    )


async def find_chain_depth(hypothesis_id: int, db_session) -> int:
    """Walks parent_hypothesis_id chain to count refinement depth.

    A fresh LLM-emitted hypothesis has depth 0 (no parent).
    A refined-from-X hypothesis has depth 1.
    A refined-from-(refined-from-X) has depth 2.

    Used to cap runaway refinement at max_refine_chain_depth.
    """
    from backend.models import Hypothesis

    depth = 0
    current_id = hypothesis_id
    seen = set()  # cycle guard
    while current_id is not None and current_id not in seen:
        seen.add(current_id)
        h = await db_session.get(Hypothesis, current_id)
        if h is None or h.parent_hypothesis_id is None:
            break
        current_id = h.parent_hypothesis_id
        depth += 1
        if depth > 10:  # absolute safety cap
            break
    return depth
