"""B5 v2 — LLM-based attribution classifier for hypothesis lifecycle.

Plan v5+ §B5 originally specified `Experiment2Feedback.gen()` (RD-Agent
typed pipeline). For Phase 2 we shipped a heuristic v1 (early_stop.classify_attribution)
which gates abandon decisions on simple ratio thresholds:

  impl-fails ≥ 75% of FAIL → "implementation"
  qual-fails ≥ 75% of FAIL → "hypothesis"
  else                     → "both"

v2 upgrades to a lightweight LLM classifier that reads:
  - the hypothesis statement (what we're testing)
  - the round's alpha attempts (expression + outcome + metrics + error)
and judges attribution semantically. Falls back to v1 heuristic on LLM
failure so abandon path stays available even when DeepSeek is down.

Cost: ~$0.001 per round (DeepSeek), one call per round. Over 100 tasks ×
10 rounds = $1 total — negligible.

This file does NOT depend on RD-Agent core types (AlphaExperiment /
ExperimentTrace) — keeps the LangGraph mining path lean.
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional, Tuple

from backend.agents.graph.early_stop import classify_attribution as _classify_heuristic

logger = logging.getLogger("agents.attribution")


_SYSTEM = """You analyze a single round of alpha mining attempts to attribute
the failure (or success) to either the HYPOTHESIS itself or the IMPLEMENTATION.

Definitions:
- HYPOTHESIS attribution: the underlying economic/quantitative idea is wrong.
  Symptoms: alphas simulate cleanly but produce low/negative sharpe across
  multiple variants; signal direction is consistently inverted; the data
  contradicts the hypothesis statement.
- IMPLEMENTATION attribution: the LLM rendered bad code. Symptoms: syntax
  errors, semantic errors (undefined fields/operators), simulator-side
  failures dominate; expressions never reach quality evaluation.
- BOTH: mixed signals — some attempts ran but produced quality failures,
  others crashed.
- UNKNOWN: round had 0 alphas / clear PASS / cannot tell from this round alone.

Output JSON:
{
  "attribution": "hypothesis" | "implementation" | "both" | "unknown",
  "confidence": 0.0-1.0,
  "reasoning": "brief 1-sentence explanation"
}
"""


def _build_user_prompt(
    hypothesis_statement: str,
    alpha_count: int,
    pass_count: int,
    syntax_fail: int,
    simulate_fail: int,
    quality_fail: int,
    samples: List[str],
) -> str:
    samples_str = "\n".join(f"  - {s}" for s in samples[:5]) or "  (none)"
    return f"""Hypothesis being tested:
{hypothesis_statement!r}

Round outcome counts:
  total alphas: {alpha_count}
  PASS / PASS_PROVISIONAL: {pass_count}
  syntax fail: {syntax_fail}
  simulate fail: {simulate_fail}
  quality fail (low sharpe/fitness/turnover): {quality_fail}

Sample attempts (expression + outcome, first 5):
{samples_str}

Classify attribution per the rules above."""


def _build_samples(pending_alphas: List) -> List[str]:
    """Compress alpha candidates to single-line summaries the LLM can read."""
    out = []
    for a in pending_alphas[:5]:
        expr = (getattr(a, "expression", "") or "")[:120]
        if not getattr(a, "is_valid", True):
            err = getattr(a, "validation_error", "syntax error") or "syntax"
            out.append(f"{expr!r} → SYNTAX_FAIL ({err[:40]})")
        elif getattr(a, "is_simulated", False) and not getattr(a, "simulation_success", True):
            err = getattr(a, "simulation_error", "sim fail") or "sim"
            out.append(f"{expr!r} → SIMULATE_FAIL ({err[:40]})")
        else:
            qs = getattr(a, "quality_status", "?")
            metrics = getattr(a, "metrics", None) or {}
            sh = metrics.get("sharpe")
            ft = metrics.get("fitness")
            out.append(f"{expr!r} → {qs} (sharpe={sh}, fitness={ft})")
    return out


async def classify_attribution_llm(
    *,
    hypothesis_statement: Optional[str],
    pending_alphas: List,
    alpha_count: int,
    pass_count: int,
    syntax_fail_count: int,
    simulate_fail_count: int,
    quality_fail_count: int,
    llm_service,
) -> Tuple[str, Optional[str]]:
    """LLM-based attribution. Returns (attribution_str, reasoning).

    attribution_str ∈ {"hypothesis", "implementation", "both", "unknown"}

    Falls back to heuristic when:
      - llm_service is None
      - hypothesis_statement is empty (v1 doesn't need it; v2 prompt does)
      - LLM call fails (network/parse/timeout)
      - LLM returns invalid attribution string

    Failure mode is silent + safe: returns the heuristic answer + reasoning="(heuristic fallback)".
    """
    heuristic = _classify_heuristic(
        alpha_count=alpha_count,
        pass_count=pass_count,
        syntax_fail_count=syntax_fail_count,
        simulate_fail_count=simulate_fail_count,
        quality_fail_count=quality_fail_count,
    )

    if llm_service is None or not hypothesis_statement:
        return heuristic, None

    # Don't burn a LLM call on early-exit cases (UNKNOWN with no signal,
    # or PASS-dominated rounds where attribution doesn't drive any decision)
    if heuristic == "unknown":
        return heuristic, None

    samples = _build_samples(pending_alphas)
    user = _build_user_prompt(
        hypothesis_statement=hypothesis_statement,
        alpha_count=alpha_count,
        pass_count=pass_count,
        syntax_fail=syntax_fail_count,
        simulate_fail=simulate_fail_count,
        quality_fail=quality_fail_count,
        samples=samples,
    )

    try:
        response = await llm_service.call(
            system_prompt=_SYSTEM,
            user_prompt=user,
            temperature=0.2,  # mostly-deterministic classification
            json_mode=True,
        )
    except Exception as e:
        logger.warning(f"[B5 v2] LLM attribution call failed, fallback to heuristic: {e}")
        return heuristic, "(heuristic fallback: LLM error)"

    if not getattr(response, "success", False):
        logger.warning(f"[B5 v2] LLM attribution call !success, fallback to heuristic")
        return heuristic, "(heuristic fallback: LLM unsuccessful)"

    parsed = getattr(response, "parsed", None)
    if not isinstance(parsed, dict):
        # Try to parse content if json_mode didn't work
        content = getattr(response, "content", "") or ""
        try:
            parsed = json.loads(content)
        except Exception:
            logger.warning(f"[B5 v2] LLM attribution parse failed, fallback to heuristic")
            return heuristic, "(heuristic fallback: parse error)"

    attribution = (parsed.get("attribution") or "").lower().strip()
    reasoning = parsed.get("reasoning") or ""
    if attribution not in ("hypothesis", "implementation", "both", "unknown"):
        logger.warning(
            f"[B5 v2] LLM returned invalid attribution {attribution!r}, fallback to heuristic"
        )
        return heuristic, "(heuristic fallback: invalid attribution string)"

    if attribution != heuristic:
        logger.info(
            f"[B5 v2] LLM disagrees with heuristic: heuristic={heuristic!r} "
            f"llm={attribution!r} reasoning={reasoning[:80]!r}"
        )
    return attribution, reasoning
