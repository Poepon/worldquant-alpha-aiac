"""R5 Hypothesis-Alignment Dual-Bridge LLM Judge (Phase 2, plan v1.0, 2026-05-18).

Implements AlphaAgent Eq. 7:
    C(h, d, f) = α·c₁(h, d) + (1-α)·c₂(d, f), α=0.5

Where:
    c₁(h, d) — LLM judges hypothesis ↔ description alignment
    c₂(d, f) — LLM judges description ↔ expression alignment

Per plan §1.2:
- Runs inside R1a hook in evaluation.py (l.2546+), AFTER R1a heuristic computes
  attribution baseline
- R5 verdict (non-None) OVERRIDES R1a heuristic attribution per [V1.0-A2-3]
  conflict resolution lock
- R5 None (both PASS or low confidence) → R1a verdict preserved
- Original R1a verdict always stored in r5_agrees_r1a for analytics

Per plan §3.3 derivation rule:
- c1 strong FAIL + c2 strong FAIL → AttributionType.BOTH
- c1 strong FAIL only           → AttributionType.HYPOTHESIS
- c2 strong FAIL only           → AttributionType.IMPLEMENTATION
- both PASS or low confidence   → None (defer to R1a)

Per plan §1.4:
- Empty description → c₁ skipped, r5_c1_aligned NULL, r5_hook_error set
- LLM call failure → caught, r5_hook_error set, returns abstain payload
- Cost estimated from tokens_used per provider rate table

Per [[feedback_r1a_dedicated_log_table]]:
- R5 results land in r1a_attribution_log r5_* columns (REUSE not new table)
- Same write path as R1a (batch INSERT at end of round)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from backend.agents.prompts.r5_alignment import (
    R5_C1_SYSTEM,
    R5_C2_SYSTEM,
    build_r5_c1_prompt,
    build_r5_c2_prompt,
)
from backend.agents.services.llm_service import LLMService
from backend.config import settings

logger = logging.getLogger("agents.r5_judge")


# Provider rate table — per plan §4.5 [V1.0-S5]
# Hard-coded constant (provider rates change ~yearly, centralization beats env drift)
COST_PER_1K_INPUT = {
    "claude-haiku-4-5":          0.00100,
    "claude-haiku-4-5-20251001": 0.00100,
    "claude-opus-4-7":           0.01500,
    "deepseek-chat":             0.00027,
    "gpt-4":                     0.03000,
}
COST_PER_1K_OUTPUT = {
    "claude-haiku-4-5":          0.00500,
    "claude-haiku-4-5-20251001": 0.00500,
    "claude-opus-4-7":           0.07500,
    "deepseek-chat":             0.00110,
    "gpt-4":                     0.06000,
}


def _estimate_cost(model: str, tokens_used: int) -> float:
    """Per plan §4.5: estimate cost from tokens_used with 30% in / 70% out split."""
    if not tokens_used or tokens_used <= 0:
        return 0.0
    in_tok = tokens_used * 0.30
    out_tok = tokens_used * 0.70
    in_rate = COST_PER_1K_INPUT.get(model, 0.001)
    out_rate = COST_PER_1K_OUTPUT.get(model, 0.005)
    return (in_tok / 1000) * in_rate + (out_tok / 1000) * out_rate


def _parse_judge_response(content: str) -> Dict[str, Any]:
    """Parse strict-JSON judge output. Per plan §2.2 §3.2.

    Returns dict with keys: aligned (bool), confidence (float 0-1, clipped),
    reason (str, truncated to 500 chars). Invalid output → abstain payload
    (aligned=True, confidence=0.5) + error logged. Caller's responsibility
    to also write r5_hook_error.
    """
    try:
        parsed = json.loads(content) if isinstance(content, str) else content
        if not isinstance(parsed, dict):
            raise ValueError("response not a dict")
        aligned_raw = parsed.get("aligned")
        if isinstance(aligned_raw, str):
            aligned = aligned_raw.lower() in ("true", "yes", "1")
        else:
            aligned = bool(aligned_raw)
        confidence = float(parsed.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))
        reason = str(parsed.get("reason", ""))[:500]
        return {"aligned": aligned, "confidence": confidence, "reason": reason}
    except Exception as ex:
        logger.debug(f"[r5_judge] parse failure (returning abstain): {ex}")
        return {"aligned": True, "confidence": 0.5, "reason": f"<parse error: {str(ex)[:100]}>"}


async def _judge_once(
    llm_service: LLMService,
    system_prompt: str,
    user_prompt: str,
    node_key: str,
) -> tuple[Dict[str, Any], float, Optional[str]]:
    """Run one judge LLM call. Returns (parsed_dict, cost_usd, error_str_or_None)."""
    try:
        resp = await llm_service.call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            json_mode=True,
            max_tokens=512,
            node_key=node_key,
        )
        content = getattr(resp, "content", "") or ""
        tokens = int(getattr(resp, "tokens_used", 0) or 0)
        # PR3 per-function routing: the judge call routes by node_key (c1/c2),
        # so the model that ACTUALLY served the call is resp.model (PR2 sets it
        # to the effective model, incl. any runtime fallback) — NOT self.model,
        # which is only the construct-time default. Price off resp.model so the
        # cost reflects the model truly billed; self.model / R5_JUDGE_MODEL stay
        # as the routing-miss fallback label.
        model = getattr(resp, "model", None) or getattr(
            llm_service, "model", settings.R5_JUDGE_MODEL
        )
        cost = _estimate_cost(model, tokens)
        return _parse_judge_response(content), cost, None
    except Exception as ex:
        logger.warning(f"[r5_judge] LLM call failed ({node_key}): {ex}")
        return (
            {"aligned": True, "confidence": 0.5, "reason": ""},
            0.0,
            str(ex)[:200],
        )


def _derive_attribution(
    c1: Dict[str, Any],
    c2: Dict[str, Any],
    low_conf: float,
) -> Optional[str]:
    """Plan §3.3 derivation rule. Returns 'hypothesis'/'implementation'/'both' or None.

    None means R5 abstains; caller preserves R1a heuristic verdict.
    """
    c1_strong_fail = (not c1["aligned"]) and c1["confidence"] >= low_conf
    c2_strong_fail = (not c2["aligned"]) and c2["confidence"] >= low_conf
    if c1_strong_fail and c2_strong_fail:
        return "both"
    if c1_strong_fail:
        return "hypothesis"
    if c2_strong_fail:
        return "implementation"
    return None


def _composite_score(c1: Dict[str, Any], c2: Dict[str, Any]) -> float:
    """AlphaAgent Eq. 7 composite C(h,d,f) with α=0.5.

    Score per dimension = confidence when aligned, (1-confidence) when not.
    Aligned + high conf → high; misaligned + high conf → low; uncertain → 0.5.
    """
    c1_score = c1["confidence"] if c1["aligned"] else (1.0 - c1["confidence"])
    c2_score = c2["confidence"] if c2["aligned"] else (1.0 - c2["confidence"])
    return 0.5 * c1_score + 0.5 * c2_score


async def run_r5_judge(
    *,
    hypothesis_statement: str,
    description: str,
    expression: str,
    llm_service: LLMService,
    r1a_attribution: Optional[str] = None,
    operators_used: Optional[list] = None,
) -> Dict[str, Any]:
    """Run the dual-bridge R5 judge on one alpha. Plan §1.2 entry point.

    Args:
        hypothesis_statement: Alpha.hypothesis or hypothesis_dict["statement"]
        description: Alpha.logic_explanation
        expression: Alpha.expression (BRAIN DSL)
        llm_service: LLMService instance (caller-injected for cost reuse)
        r1a_attribution: R1a heuristic verdict (for r5_agrees_r1a computation)
        operators_used: Optional ops list to enrich c₂ prompt

    Returns:
        Dict with 10 r5_* keys ready to merge into r1a_attribution_log row.
        Caller (evaluation.py) applies override rule: if returned
        ``r5_attribution`` is non-None, overwrite ``_r1a_log["attribution"]``.
    """
    payload: Dict[str, Any] = {
        "r5_c1_aligned": None,
        "r5_c1_confidence": None,
        "r5_c1_reason": None,
        "r5_c2_aligned": None,
        "r5_c2_confidence": None,
        "r5_c2_reason": None,
        "r5_composite_score": None,
        "r5_agrees_r1a": None,
        "r5_hook_error": None,
        "r5_cost_usd": 0.0,
        # Internal-only; caller uses this to OVERWRITE _r1a_log["attribution"]
        "r5_attribution": None,
    }

    # [V1.0-A1-2] empty description → skip c₁
    desc_empty = not (description or "").strip()
    hyp_empty = not (hypothesis_statement or "").strip()
    expr_empty = not (expression or "").strip()

    errs = []

    # === c₁(h, d) ===
    if desc_empty or hyp_empty:
        errs.append(f"c1_skipped:{'desc' if desc_empty else 'hyp'}_empty")
        c1 = None
    else:
        c1_user = build_r5_c1_prompt(hypothesis_statement, description)
        c1_dict, c1_cost, c1_err = await _judge_once(
            llm_service, R5_C1_SYSTEM, c1_user, node_key="r5_alignment_c1",
        )
        payload["r5_cost_usd"] += c1_cost
        if c1_err:
            errs.append(f"c1_call:{c1_err[:80]}")
            c1 = None
        else:
            c1 = c1_dict
            payload["r5_c1_aligned"] = "true" if c1["aligned"] else "false"
            payload["r5_c1_confidence"] = c1["confidence"]
            payload["r5_c1_reason"] = c1["reason"]

    # === c₂(d, f) ===
    if desc_empty or expr_empty:
        errs.append(f"c2_skipped:{'desc' if desc_empty else 'expr'}_empty")
        c2 = None
    else:
        c2_user = build_r5_c2_prompt(description, expression, operators_used)
        c2_dict, c2_cost, c2_err = await _judge_once(
            llm_service, R5_C2_SYSTEM, c2_user, node_key="r5_alignment_c2",
        )
        payload["r5_cost_usd"] += c2_cost
        if c2_err:
            errs.append(f"c2_call:{c2_err[:80]}")
            c2 = None
        else:
            c2 = c2_dict
            payload["r5_c2_aligned"] = "true" if c2["aligned"] else "false"
            payload["r5_c2_confidence"] = c2["confidence"]
            payload["r5_c2_reason"] = c2["reason"]

    # === Composite + attribution derivation (only when BOTH c1+c2 succeeded) ===
    if c1 is not None and c2 is not None:
        payload["r5_composite_score"] = _composite_score(c1, c2)
        low_conf = float(getattr(settings, "R5_JUDGE_LOW_CONF", 0.55))
        r5_attribution = _derive_attribution(c1, c2, low_conf)
        payload["r5_attribution"] = r5_attribution
        if r5_attribution is not None and r1a_attribution is not None:
            payload["r5_agrees_r1a"] = "true" if r5_attribution == r1a_attribution else "false"

    if errs:
        payload["r5_hook_error"] = "; ".join(errs)[:200]

    # Round cost to 6 decimals for storage cleanliness
    payload["r5_cost_usd"] = round(payload["r5_cost_usd"], 6)

    return payload
