"""Phase 3 R1b.1b: CoSTEER retry loop node (Code-Strategy-Test-Evaluate-Evolve-Refine).

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §3.

R1b.1 — implementation retry loop. When R1a / R5 attribution flags a FAIL
alpha as IMPLEMENTATION (or BOTH and HYPOTHESIS_MUTATE flag is off), this
node rewrites the expression via LLM while preserving hypothesis intent.

Module currently exposes ``node_code_gen_retry``; ``node_hypothesis_mutate``
and ``node_r1b_retry_router`` arrive in R1b.1c/R1b.2.

Loop termination per plan [V1.1-A1-1]: three redundant guards prevent
infinite cycles:
  1. ``state.r1b_retries_attempted_this_alpha < R1B_MAX_RETRIES_PER_ALPHA``
  2. ``state.r1b_mutations_attempted_this_cycle < R1B_MAX_MUTATIONS_PER_DATASET_CYCLE``
  3. ``state.r1b_token_cost_this_alpha < R1B_TOKEN_COST_CEILING_USD_PER_ALPHA``
Any guard fail → router routes to save_results. The node itself ALSO
self-checks (V-26.57 pattern) so direct-invoke tests work without router.

Soft-fail per plan §3.2: per-alpha try/except — single LLM failure does
NOT block the round; the failed alpha just doesn't get rewritten and
flows on as FAIL.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from backend.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cost estimation — mirrors R5 judge per-1K rate table (plan §6.1)
# ---------------------------------------------------------------------------

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

# R1b.1 review LOW 1 — warn-once per process for unknown models so accounting
# drift is visible if R1B_RETRY_MODEL is pointed at an exotic endpoint. Falls
# back to haiku-rate defaults silently otherwise (Pythonic dict.get default).
_R1B_COST_WARNED_MODELS: set[str] = set()


def _estimate_cost(model: str, tokens_used: int) -> float:
    """Per plan §6.1 — 30% input / 70% output split heuristic."""
    if not tokens_used or tokens_used <= 0:
        return 0.0
    if model not in COST_PER_1K_INPUT and model not in _R1B_COST_WARNED_MODELS:
        logger.warning(
            f"[R1b cost] unknown model {model!r}; using haiku-rate fallback "
            f"for accounting (actual cost may differ)"
        )
        _R1B_COST_WARNED_MODELS.add(model)
    in_tok = tokens_used * 0.30
    out_tok = tokens_used * 0.70
    in_rate = COST_PER_1K_INPUT.get(model, 0.001)
    out_rate = COST_PER_1K_OUTPUT.get(model, 0.005)
    return (in_tok / 1000) * in_rate + (out_tok / 1000) * out_rate


# ---------------------------------------------------------------------------
# Telemetry helper — batch INSERT into r1b_retry_log
# ---------------------------------------------------------------------------

async def _write_r1b_retry_log_rows(rows: List[Dict[str, Any]]) -> None:
    """Plan §6.4-style dedicated AsyncSession + soft-fail. Never raises."""
    if not rows:
        return
    try:
        from backend.database import AsyncSessionLocal
        from backend.models.r1b_retry import R1bRetryLog
    except Exception as ex:
        logger.debug(f"[r1b_loop] log writer deps unavailable ({ex})")
        return
    try:
        async with AsyncSessionLocal() as db:
            for r in rows:
                db.add(R1bRetryLog(**r))
            await db.commit()
        logger.info(f"[r1b_loop] wrote {len(rows)} retry_log rows")
    except Exception as ex:
        logger.warning(f"[r1b_loop] retry_log write failed (round unaffected): {ex}")


# ---------------------------------------------------------------------------
# node_code_gen_retry — R1b.1
# ---------------------------------------------------------------------------

async def node_code_gen_retry(
    state: Any,
    llm_service: Any,
    config: Any = None,
) -> Dict[str, Any]:
    """LangGraph node — rewrite FAIL+IMPLEMENTATION alphas via LLM.

    Returns a partial state dict (LangGraph reducer merges into MiningState).
    Updates ``state.pending_alphas`` with rewritten expressions in-place via
    Pydantic ``model_copy``; resets validate/simulate state so they re-flow.

    Self-guard (V-26.57 pattern, plan §3.2): if budget already exhausted,
    early-return without touching anything.
    """
    # Self-guard mirror of plan §3.2 — direct-invoke tests + extra defensive
    if getattr(state, "r1b_retries_attempted_this_alpha", 0) >= int(
        getattr(settings, "R1B_MAX_RETRIES_PER_ALPHA", 3)
    ):
        logger.debug("[r1b_loop] per-alpha retry budget exhausted; no-op")
        return {
            "r1b_retries_attempted_this_alpha": state.r1b_retries_attempted_this_alpha
        }
    if getattr(state, "r1b_token_cost_this_alpha", 0.0) >= float(
        getattr(settings, "R1B_TOKEN_COST_CEILING_USD_PER_ALPHA", 0.05)
    ):
        logger.warning(
            f"[r1b_loop] token ceiling hit "
            f"${state.r1b_token_cost_this_alpha:.4f}; no-op"
        )
        return {
            "r1b_token_cost_this_alpha": state.r1b_token_cost_this_alpha
        }

    pending = list(getattr(state, "pending_alphas", []) or [])
    target_indices: List[int] = []
    for i, a in enumerate(pending):
        if getattr(a, "quality_status", None) != "FAIL":
            continue
        attr = (getattr(a, "metrics", None) or {}).get("_r1a_attribution")
        if attr in ("implementation", "both"):
            target_indices.append(i)

    if not target_indices:
        logger.debug("[r1b_loop] no FAIL+IMPLEMENTATION alphas to retry")
        return {}

    from backend.agents.prompts.r1b_retry import build_r1b_retry_prompt

    updated_alphas = list(pending)
    cost_delta_usd = 0.0
    log_rows: List[Dict[str, Any]] = []
    model_name = getattr(llm_service, "model", None) or getattr(
        settings, "R1B_RETRY_MODEL", "claude-haiku-4-5-20251001"
    )
    region = getattr(state, "region", None)
    task_id = getattr(state, "task_id", None)
    round_idx = getattr(state, "round_idx", None) or getattr(state, "round_index", None)

    # Compute allowed_fields once — top 50 from state.fields (plan §3.2)
    allowed_fields = []
    for f in (getattr(state, "fields", None) or [])[:50]:
        fid = (f.get("id") if isinstance(f, dict) else None) or getattr(f, "id", None)
        if fid:
            allowed_fields.append(str(fid))

    # R1b.1 review LOW 2 — per-round soft cap. Estimate next-call cost via
    # max_tokens worst-case (512) so we don't overshoot. When state cumulative
    # + this estimate >= cap, skip retry (alpha stays as-is, NOT failed).
    per_round_cap = float(getattr(settings, "R1B_MAX_COST_USD_PER_ROUND", 5.00))
    round_cost_so_far = float(getattr(state, "r1b_cost_this_round", 0.0) or 0.0)
    estimated_next_call_cost = _estimate_cost(model_name, 512)

    for idx in target_indices:
        # Pre-call per-round cap check — soft-fall (skip LLM, alpha not failed)
        if round_cost_so_far + cost_delta_usd + estimated_next_call_cost >= per_round_cap:
            logger.info(
                f"[r1b_loop] per-round cost cap ${per_round_cap:.2f} would be "
                f"exceeded (so_far=${round_cost_so_far + cost_delta_usd:.4f} + "
                f"est=${estimated_next_call_cost:.4f}); skipping retry for "
                f"alpha idx={idx} (alpha left as FAIL)"
            )
            break

        original = pending[idx]
        original_metrics = dict(getattr(original, "metrics", None) or {})
        # Review MEDIUM #5 fix (2026-05-18): per-iteration call cost so
        # each log row records its OWN cost, not the loop-cumulative sum.
        # Was: every successive log row carried cost_delta_usd (running
        # total) → /ops/r1b/telemetry's SUM(llm_cost_usd) overcounted
        # by ~N²/2 for an N-retry round. cost_delta_usd is still the
        # accumulator for the per-alpha + per-round state ledger below.
        _call_cost = 0.0
        tokens = 0
        try:
            sys_p, user_p = build_r1b_retry_prompt(
                original_expression=getattr(original, "expression", "") or "",
                original_hypothesis=getattr(original, "hypothesis", "") or "",
                failure_metrics=original_metrics,
                r1a_evidence=original_metrics.get("_r1a_attribution_evidence") or [],
                r5_c2_reason=original_metrics.get("_r5_c2_reason") or "",
                allowed_fields=allowed_fields,
            )
            resp = await llm_service.call(
                system_prompt=sys_p, user_prompt=user_p,
                json_mode=True, max_tokens=512,
                node_key="r1b_retry",
            )
            tokens = int(getattr(resp, "tokens_used", 0) or 0)
            _call_cost = _estimate_cost(model_name, tokens)
            cost_delta_usd += _call_cost
        except Exception as ex:
            logger.warning(
                f"[r1b_loop] LLM call failed for alpha idx={idx}: {ex}"
            )
            log_rows.append(_make_log_row(
                original, idx, task_id, round_idx,
                model_name, original_metrics,
                new_expression=None, llm_changes_made=None,
                outcome="pending", loop_error=str(ex)[:200],
                llm_cost_usd=0.0, llm_tokens_used=0,
            ))
            continue

        parsed = getattr(resp, "parsed", None)
        if not getattr(resp, "success", False) or not isinstance(parsed, dict):
            log_rows.append(_make_log_row(
                original, idx, task_id, round_idx,
                model_name, original_metrics,
                new_expression=None, llm_changes_made=None,
                outcome="pending",
                loop_error="LLM returned non-success / non-dict",
                llm_cost_usd=_call_cost, llm_tokens_used=tokens,
            ))
            continue

        new_expr = str(parsed.get("fixed_expression") or "").strip()
        if not new_expr or new_expr == getattr(original, "expression", ""):
            # No-op retry — count it but don't replace
            log_rows.append(_make_log_row(
                original, idx, task_id, round_idx,
                model_name, original_metrics,
                new_expression=new_expr or None,
                llm_changes_made=str(parsed.get("changes_made", "")),
                outcome="pending",
                loop_error="LLM returned same/empty expression",
                llm_cost_usd=_call_cost, llm_tokens_used=tokens,
            ))
            continue

        # Apply retry — Pydantic model_copy + V-26.79 metrics rebind
        try:
            updated = original.model_copy()
        except Exception:
            # Defensive — non-Pydantic falls back to passing through
            updated = original
        # Preserve original_expression — never overwrite if already set
        if not getattr(updated, "original_expression", None):
            try:
                updated.original_expression = getattr(original, "expression", "")
            except Exception:
                pass
        try:
            updated.expression = new_expr
            updated.is_valid = None
            updated.validation_error = None
            updated.is_simulated = False
            updated.simulation_success = None
            updated.quality_status = "PENDING"
        except Exception as ex:
            logger.warning(f"[r1b_loop] failed to apply retry for idx={idx}: {ex}")
            continue
        # V-26.79 metrics rebind (mirror R1a hook)
        _m = dict(original_metrics)
        _m["_r1b_retry_chain"] = (_m.get("_r1b_retry_chain") or []) + [
            getattr(original, "expression", "")
        ]
        _m["_r1b_retry_reason"] = str(parsed.get("changes_made", ""))
        try:
            updated.metrics = _m
        except Exception:
            pass
        updated_alphas[idx] = updated

        log_rows.append(_make_log_row(
            original, idx, task_id, round_idx,
            model_name, original_metrics,
            new_expression=new_expr,
            llm_changes_made=str(parsed.get("changes_made", "")),
            outcome="pending",  # filled by post-BRAIN reconciliation hook
            loop_error=None,
            llm_cost_usd=_call_cost, llm_tokens_used=tokens,
        ))

    await _write_r1b_retry_log_rows(log_rows)

    return {
        "pending_alphas": updated_alphas,
        "r1b_retries_attempted_this_alpha": state.r1b_retries_attempted_this_alpha + 1,
        "r1b_token_cost_this_alpha": state.r1b_token_cost_this_alpha + cost_delta_usd,
        # R1b.1 review LOW 2 — per-round cumulative cost accumulator
        "r1b_cost_this_round": round_cost_so_far + cost_delta_usd,
    }


# ---------------------------------------------------------------------------
# Internal: build a R1bRetryLog row dict from an alpha + LLM outcome
# ---------------------------------------------------------------------------

def _make_log_row(
    original: Any,
    idx: int,
    task_id: Any,
    round_idx: Any,
    model_name: str,
    original_metrics: Dict[str, Any],
    *,
    new_expression: Any,
    llm_changes_made: Any,
    outcome: str,
    loop_error: Any,
    llm_cost_usd: float,
    llm_tokens_used: int,
) -> Dict[str, Any]:
    import hashlib
    expr = getattr(original, "expression", "") or ""
    return {
        "task_id": task_id,
        "round_idx": round_idx,
        "attempt_type": "retry_impl",
        "triggering_attribution": original_metrics.get("_r1a_attribution"),
        "triggering_attribution_source": (
            "r5_judge" if original_metrics.get("_r5_c2_reason") else "r1a_heuristic"
        ),
        "original_expression_hash": hashlib.sha256(
            expr.encode("utf-8")
        ).hexdigest()[:64],
        "original_alpha_id_brain": getattr(original, "alpha_id", None),
        "original_hypothesis_id": original_metrics.get("hypothesis_id"),
        "original_quality_status": getattr(original, "quality_status", None),
        "new_expression": new_expression,
        "new_hypothesis_statement": None,
        "new_hypothesis_id": None,
        "llm_changes_made": llm_changes_made,
        "outcome": outcome,
        "outcome_alpha_id_brain": None,
        "outcome_sharpe": None,
        "outcome_fitness": None,
        "llm_cost_usd": llm_cost_usd,
        "llm_tokens_used": llm_tokens_used,
        "llm_model": model_name,
        "loop_error": loop_error,
    }


# ---------------------------------------------------------------------------
# node_hypothesis_mutate — R1b.2
# ---------------------------------------------------------------------------

async def node_hypothesis_mutate(
    state: Any,
    llm_service: Any,
    config: Any = None,
) -> Dict[str, Any]:
    """LangGraph node — propose a revised hypothesis via LLM.

    Plan §4.2 — **dataset-cycle-scoped** (not per-alpha). One LLM call per
    unique original hypothesis; existing FAIL alphas of that hypothesis
    are dropped from the round (caller's hypothesis-propose node picks up
    ``state.r1b_pending_new_hypothesis`` in the next iteration).

    Per [V1.0-A2-3] — mutate dominates retry on BOTH attribution because
    rewriting the hypothesis usually changes the expression family entirely,
    making implementation retry stale.

    Self-guards (V-26.57 + plan §4.1):
      - per-cycle mutation budget (R1B_MAX_MUTATIONS_PER_DATASET_CYCLE)
      - per-alpha token cost ceiling (shared with retry node)

    Per-hypothesis try/except — single LLM failure does NOT block round.
    """
    # Self-guards
    if getattr(state, "r1b_mutations_attempted_this_cycle", 0) >= int(
        getattr(settings, "R1B_MAX_MUTATIONS_PER_DATASET_CYCLE", 2)
    ):
        logger.debug("[r1b_loop] per-cycle mutation budget exhausted; no-op")
        return {
            "r1b_mutations_attempted_this_cycle": state.r1b_mutations_attempted_this_cycle
        }
    if getattr(state, "r1b_token_cost_this_alpha", 0.0) >= float(
        getattr(settings, "R1B_TOKEN_COST_CEILING_USD_PER_ALPHA", 0.05)
    ):
        logger.warning(
            f"[r1b_loop mutate] token ceiling hit "
            f"${state.r1b_token_cost_this_alpha:.4f}; no-op"
        )
        return {
            "r1b_token_cost_this_alpha": state.r1b_token_cost_this_alpha
        }

    pending = list(getattr(state, "pending_alphas", []) or [])

    # Plan §4.2 dataset-cycle dedupe: group FAIL+HYPOTHESIS|BOTH alphas by
    # hypothesis statement so each unique hypothesis triggers ONE mutate.
    groups: Dict[str, List[int]] = {}
    for i, a in enumerate(pending):
        if getattr(a, "quality_status", None) != "FAIL":
            continue
        attr = (getattr(a, "metrics", None) or {}).get("_r1a_attribution")
        if attr not in ("hypothesis", "both"):
            continue
        hyp = (getattr(a, "hypothesis", "") or "").strip()
        if not hyp:
            continue
        groups.setdefault(hyp, []).append(i)

    if not groups:
        logger.debug("[r1b_loop mutate] no FAIL+HYPOTHESIS alphas to mutate")
        return {}

    from backend.agents.prompts.r1b_mutate import build_r1b_mutate_prompt

    cost_delta_usd = 0.0
    pending_new_hypothesis = None
    log_rows: List[Dict[str, Any]] = []
    model_name = getattr(llm_service, "model", None) or getattr(
        settings, "R1B_MUTATE_MODEL", "claude-haiku-4-5-20251001"
    )
    region = getattr(state, "region", None)
    task_id = getattr(state, "task_id", None)
    round_idx = getattr(state, "round_idx", None) or getattr(state, "round_index", None)
    pillar = getattr(state, "current_pillar", None) or ""

    # Plan §4.2 — v1.0 mutates ONE hypothesis per node invocation. If more
    # than one unique hypothesis failed, pick the one with the most failed
    # alphas (highest impact). Future v2 may batch.
    primary_hyp = max(groups.items(), key=lambda kv: len(kv[1]))[0]
    primary_indices = groups[primary_hyp]

    # R1b.2 review MEDIUM (2026-05-18): per-round R1B_MAX_MUTATIONS_PER_
    # DATASET_CYCLE caps within a round, but pending → inject → mutate
    # chains could spiral across rounds. Bail when parent Hypothesis row
    # already at depth >= R1B_MAX_MUTATION_DEPTH. Soft-fail on DB lookup
    # failure: proceed (don't block on observability glitches).
    _primary_first_alpha = pending[primary_indices[0]]
    _primary_metrics = dict(getattr(_primary_first_alpha, "metrics", None) or {})
    _parent_hyp_id = _primary_metrics.get("hypothesis_id")
    if _parent_hyp_id is not None:
        try:
            from backend.database import AsyncSessionLocal as _DepthSL
            from backend.models import Hypothesis as _DepthHyp
            from sqlalchemy import select as _depth_sel
            _max_depth = int(getattr(settings, "R1B_MAX_MUTATION_DEPTH", 3))
            async with _DepthSL() as _depth_db:
                _row = (await _depth_db.execute(
                    _depth_sel(_DepthHyp).where(_DepthHyp.id == _parent_hyp_id)
                )).scalar_one_or_none()
                _parent_depth = int(
                    getattr(_row, "r1b_mutation_depth", 0) or 0
                ) if _row is not None else 0
            if _parent_depth >= _max_depth:
                logger.info(
                    f"[r1b_loop mutate] mutation chain depth cap reached "
                    f"(parent_id={_parent_hyp_id} depth={_parent_depth} "
                    f">= R1B_MAX_MUTATION_DEPTH={_max_depth}); no-op"
                )
                return {
                    "r1b_mutations_attempted_this_cycle": (
                        getattr(state, "r1b_mutations_attempted_this_cycle", 0)
                    )
                }
        except Exception as _depth_ex:
            logger.debug(
                f"[r1b_loop mutate] depth-cap lookup soft-failed ({_depth_ex}); proceeding"
            )

    # Build outcome bullets from the primary group
    outcomes = []
    primary_alpha = pending[primary_indices[0]]
    primary_metrics = dict(getattr(primary_alpha, "metrics", None) or {})
    r5_c1_reason = primary_metrics.get("_r5_c1_reason") or ""
    for idx in primary_indices[:8]:
        a = pending[idx]
        m = getattr(a, "metrics", None) or {}
        outcomes.append({
            "expression": getattr(a, "expression", ""),
            "sharpe": m.get("sharpe"),
            "fitness": m.get("fitness"),
        })

    try:
        sys_p, user_p = build_r1b_mutate_prompt(
            original_hypothesis=primary_hyp,
            original_alpha_outcomes=outcomes,
            r5_c1_reason=r5_c1_reason,
            failure_tree_summary=primary_metrics.get("_r1b_failure_tree_summary") or "",
            region=region or "USA",
            dataset_id=getattr(state, "dataset_id", "") or "",
            pillar=pillar,
        )
        resp = await llm_service.call(
            system_prompt=sys_p, user_prompt=user_p,
            json_mode=True, max_tokens=768,
            node_key="r1b_mutate",
        )
        tokens = int(getattr(resp, "tokens_used", 0) or 0)
        cost_delta_usd += _estimate_cost(model_name, tokens)
    except Exception as ex:
        logger.warning(f"[r1b_loop mutate] LLM call failed: {ex}")
        log_rows.append(_make_mutate_log_row(
            primary_alpha, primary_indices[0], task_id, round_idx, model_name,
            primary_metrics,
            new_hypothesis_statement=None, llm_changes_made=None,
            outcome="pending", loop_error=str(ex)[:200],
            llm_cost_usd=0.0, llm_tokens_used=0,
        ))
        await _write_r1b_retry_log_rows(log_rows)
        return {
            "r1b_mutations_attempted_this_cycle": state.r1b_mutations_attempted_this_cycle + 1,
        }

    parsed = getattr(resp, "parsed", None)
    if not getattr(resp, "success", False) or not isinstance(parsed, dict):
        log_rows.append(_make_mutate_log_row(
            primary_alpha, primary_indices[0], task_id, round_idx, model_name,
            primary_metrics,
            new_hypothesis_statement=None, llm_changes_made=None,
            outcome="pending",
            loop_error="LLM returned non-success / non-dict",
            llm_cost_usd=cost_delta_usd, llm_tokens_used=tokens,
        ))
    else:
        new_hyp_block = parsed.get("new_hypothesis") or {}
        new_statement = str(new_hyp_block.get("statement", "")).strip()
        diff = str(parsed.get("diff_from_original", "")).strip()
        if new_statement and new_statement != primary_hyp:
            # R1b.2 review LOW (2026-05-18): defensive pillar-preservation
            # fallback — soft-fall if the LLM (despite the strict prompt)
            # produces a mutation that crosses pillars. Family caps + R10 +
            # Q10 diversity stats break if mid-cycle mutations drift pillar.
            new_key_fields = list(new_hyp_block.get("key_fields", []) or [])
            new_suggested_ops = list(new_hyp_block.get("suggested_operators", []) or [])
            new_expected_signal = str(new_hyp_block.get("expected_signal", ""))
            new_emitted_pillar = new_hyp_block.get("pillar")
            pillar_drift_detected = False
            if pillar:  # only enforce when we know the original pillar
                try:
                    from backend.pillar_classifier import infer_pillar
                    inferred = infer_pillar(
                        hypothesis_pillar=new_emitted_pillar,
                        key_fields=new_key_fields,
                        suggested_operators=new_suggested_ops,
                        expected_signal=new_expected_signal,
                    )
                    # "other" inference is ambiguous, not a confirmed drift —
                    # don't reject on it. Reject only on a confirmed different
                    # canonical pillar.
                    if inferred and inferred != "other" and inferred != pillar:
                        pillar_drift_detected = True
                        logger.warning(
                            f"[r1b_loop mutate] cross-pillar drift rejected: "
                            f"original pillar={pillar!r}, mutated inferred={inferred!r}"
                            f" (emitted pillar field={new_emitted_pillar!r})"
                        )
                except Exception as ex:
                    logger.debug(f"[r1b_loop mutate] pillar inference skipped: {ex}")

            if pillar_drift_detected:
                log_rows.append(_make_mutate_log_row(
                    primary_alpha, primary_indices[0], task_id, round_idx, model_name,
                    primary_metrics,
                    new_hypothesis_statement=new_statement,
                    llm_changes_made=diff,
                    outcome="pending",
                    loop_error=f"cross-pillar drift rejected (orig={pillar})",
                    llm_cost_usd=cost_delta_usd, llm_tokens_used=tokens,
                ))
                # Do NOT emit pending_new_hypothesis — original stays unchanged
            else:
                # Emit pending hypothesis for next-iteration propose node
                pending_new_hypothesis = {
                    "statement": new_statement,
                    "rationale": str(new_hyp_block.get("rationale", "")),
                    "expected_signal": new_expected_signal,
                    "key_fields": new_key_fields,
                    "suggested_operators": new_suggested_ops,
                    "parent_hypothesis_statement": primary_hyp,
                    "diff_from_original": diff,
                }
                log_rows.append(_make_mutate_log_row(
                    primary_alpha, primary_indices[0], task_id, round_idx, model_name,
                    primary_metrics,
                    new_hypothesis_statement=new_statement, llm_changes_made=diff,
                    outcome="pending", loop_error=None,
                    llm_cost_usd=cost_delta_usd, llm_tokens_used=tokens,
                ))
        else:
            log_rows.append(_make_mutate_log_row(
                primary_alpha, primary_indices[0], task_id, round_idx, model_name,
                primary_metrics,
                new_hypothesis_statement=new_statement or None,
                llm_changes_made=diff,
                outcome="pending",
                loop_error="LLM returned same/empty hypothesis statement",
                llm_cost_usd=cost_delta_usd, llm_tokens_used=tokens,
            ))

    await _write_r1b_retry_log_rows(log_rows)

    # Review HIGH #2 fix accumulator — populated by the R1b.3-v2 INSERT block
    # below. Surfaces in the returned reducer dict so LangGraph propagates
    # the new ids into the next node's state.
    _new_mutated_ids: List[int] = []

    # R1b.3-v2 (2026-05-18): persist the new mutated hypothesis as a
    # Hypothesis row with parent_hypothesis_id FK + r1b_mutation_depth so
    # the chain can be DB-walked next round + failure_tree skeletons span
    # past depth=1. Soft-fail: if INSERT fails the pending dict still flows
    # forward without a DB id (R1b.3c v1 behavior — _maybe_record_failure_tree
    # will fall back to the 2-node skeleton). Flag-gated by
    # ENABLE_R1B_HYPOTHESIS_MUTATE (same gate as the mutate node itself).
    if pending_new_hypothesis is not None:
        try:
            from backend.config import settings as _v2_settings
            if bool(getattr(_v2_settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False)):
                new_id = await _insert_mutated_hypothesis(
                    task_id=task_id,
                    parent_hypothesis_id=primary_metrics.get("hypothesis_id"),
                    pending=pending_new_hypothesis,
                    region=getattr(primary_alpha, "region", None) or getattr(state, "region", None),
                )
                if new_id is not None:
                    pending_new_hypothesis["hypothesis_id"] = new_id
                    # Review HIGH #2 fix (2026-05-18): track the new id in a
                    # local accumulator so we can append to the reducer-
                    # returned ``out`` dict below. Previously this mutated
                    # ``state.r1b_mutated_hypothesis_ids`` directly, which
                    # LangGraph drops on the next node hop (state snapshots
                    # only consume the dict returned by the node).
                    _new_mutated_ids.append(new_id)
        except Exception as _ins_ex:
            logger.warning(
                f"[r1b_loop mutate] R1b.3-v2 INSERT failed (round unaffected): {_ins_ex}"
            )

    # R1b.3c (2026-05-18): on successful mutate, persist failure_tree to KB
    # so R8 RAG L2 surfaces it next round. Plan §7.1 — fires only when the
    # mutate succeeded (pending_new_hypothesis non-None) AND the flag is
    # ON. R1b.3-v2: chain is now DB-walked from the parent_hypothesis_id
    # up to R1B_FAILURE_TREE_MAX_DEPTH, falling back to the 2-node skeleton
    # when no FK / depth=1.
    if pending_new_hypothesis is not None:
        await _maybe_record_failure_tree(
            primary_hyp=primary_hyp,
            pending=pending_new_hypothesis,
            log_rows=log_rows,
            primary_alpha=primary_alpha,
            primary_metrics=primary_metrics,
        )

    out: Dict[str, Any] = {
        "r1b_mutations_attempted_this_cycle": state.r1b_mutations_attempted_this_cycle + 1,
        "r1b_token_cost_this_alpha": state.r1b_token_cost_this_alpha + cost_delta_usd,
    }
    # Review HIGH #2 fix: surface the INSERTed Hypothesis id(s) through the
    # reducer so the next node sees them. Direct state mutation in the
    # INSERT block was dropped by LangGraph (state snapshot is per-node).
    if _new_mutated_ids:
        out["r1b_mutated_hypothesis_ids"] = list(
            state.r1b_mutated_hypothesis_ids or []
        ) + _new_mutated_ids
    if pending_new_hypothesis is not None:
        out["r1b_pending_new_hypothesis"] = pending_new_hypothesis
    return out


async def _maybe_record_failure_tree(
    *,
    primary_hyp: str,
    pending: Dict[str, Any],
    log_rows: List[Dict[str, Any]],
    primary_alpha: Any,
    primary_metrics: Dict[str, Any],
) -> None:
    """R1b.3c + R1b.3-v2 — soft-fail UPSERT failure_tree to KB via dedicated session.

    R1b.3c (2026-05-18): minimum 2-node chain {parent → new}.
    R1b.3-v2 (2026-05-18): when parent_id resolves to a real Hypothesis row,
    walk its parent_hypothesis_id chain via DB SELECT up to
    R1B_FAILURE_TREE_MAX_DEPTH (default 4) so the failure_tree skeleton
    captures the full mutation history. Falls back to the 2-node skeleton
    when no parent / no DB connectivity / chain walk fails.

    NEVER raises — DB / import errors logged + swallowed so the mutate
    node's main path is unaffected.
    """
    try:
        from backend.config import settings as _stg
    except Exception:
        return
    if not getattr(_stg, "ENABLE_R1B_FAILURE_TREE", False):
        return
    try:
        from backend.knowledge_extraction import record_failure_tree
        from backend.database import AsyncSessionLocal as _R1B_SessionLocal
    except Exception as ex:
        logger.debug(f"[r1b_loop mutate] failure_tree deps unavailable ({ex})")
        return
    parent_id = primary_metrics.get("hypothesis_id") if isinstance(primary_metrics, dict) else None

    max_depth = int(getattr(_stg, "R1B_FAILURE_TREE_MAX_DEPTH", 4) or 4)
    chain = await _build_parent_chain(
        parent_id=parent_id,
        parent_statement_fallback=primary_hyp,
        max_depth=max_depth,
    )
    # Append the new (uncommitted-id-or-just-INSERTed) tip — pending may
    # carry hypothesis_id if R1b.3-v2 INSERT succeeded earlier in the node.
    chain.append({
        "id": pending.get("hypothesis_id"),
        "statement": pending.get("statement", ""),
        "mutation_depth": (chain[-1]["mutation_depth"] + 1) if chain else 1,
        "diff_from_parent": pending.get("diff_from_original", ""),
    })

    try:
        async with _R1B_SessionLocal() as _r1b_db:
            ok = await record_failure_tree(
                hypothesis_chain=chain,
                retry_log_rows=log_rows,
                db=_r1b_db,
            )
        if ok:
            logger.info(
                f"[r1b_loop mutate] failure_tree persisted "
                f"(chain_depth={len(chain)} parent={primary_hyp[:60]!r} → "
                f"new={pending.get('statement', '')[:60]!r})"
            )
    except Exception as ex:
        logger.warning(
            f"[r1b_loop mutate] failure_tree persist failed "
            f"(round unaffected): {ex}"
        )


async def _build_parent_chain(
    *,
    parent_id: Any,
    parent_statement_fallback: str,
    max_depth: int,
) -> List[Dict[str, Any]]:
    """R1b.3-v2 (2026-05-18): walk Hypothesis.parent_hypothesis_id chain up
    to ``max_depth`` rows (inclusive of the parent itself), returning oldest
    ancestor first. Falls back to a single-node list using
    ``parent_statement_fallback`` when no parent_id / DB walk fails.

    NEVER raises — returns at minimum [{id: parent_id, statement: fallback,
    mutation_depth: 0}].
    """
    fallback = [{
        "id": parent_id,
        "statement": parent_statement_fallback,
        "mutation_depth": 0,
    }]
    if parent_id is None:
        return fallback
    try:
        from backend.database import AsyncSessionLocal as _SL
        from backend.models import Hypothesis
        from sqlalchemy import select as _sel
    except Exception:
        return fallback
    try:
        chain: List[Dict[str, Any]] = []
        # R1b.3-v2 review LOW 2 (2026-05-18): defense-in-depth cycle detection.
        # The ORM writer (_insert_mutated_hypothesis) only ever INSERTs new
        # rows so cycles cannot form via the current code path, but no DB
        # CHECK constraint prevents a future migration bug or manual SQL from
        # creating A→B→A loops. Without a `seen_ids` guard the walk would
        # terminate via max_depth but emit duplicate nodes (corrupting the
        # failure_tree KB skeleton).
        seen_ids: set = set()
        cur_id = parent_id
        async with _SL() as _db:
            for _ in range(max(1, max_depth)):
                if cur_id is None:
                    break
                if cur_id in seen_ids:
                    logger.warning(
                        f"[r1b_loop] cycle detected at id={cur_id}; "
                        f"breaking chain walk"
                    )
                    break
                seen_ids.add(cur_id)
                row = (
                    await _db.execute(
                        _sel(Hypothesis).where(Hypothesis.id == cur_id)
                    )
                ).scalar_one_or_none()
                if row is None:
                    break
                chain.append({
                    "id": row.id,
                    "statement": getattr(row, "statement", "") or "",
                    "mutation_depth": int(
                        getattr(row, "r1b_mutation_depth", 0) or 0
                    ),
                })
                cur_id = getattr(row, "parent_hypothesis_id", None)
        if not chain:
            return fallback
        # Reverse so oldest ancestor is first (lowest depth → highest).
        chain.reverse()
        return chain
    except Exception as ex:
        logger.debug(f"[r1b_loop mutate] parent-chain walk failed ({ex}); falling back")
        return fallback


async def _insert_mutated_hypothesis(
    *,
    task_id: Any,
    parent_hypothesis_id: Any,
    pending: Dict[str, Any],
    region: Any,
) -> Any:
    """R1b.3-v2 (2026-05-18): INSERT a new Hypothesis row for the mutated
    hypothesis with parent_hypothesis_id FK + r1b_mutation_depth bumped.

    Returns the new row id on success, None on any failure. Uses an
    independent AsyncSessionLocal so DB issues don't poison the calling
    LangGraph session's transaction state.

    NEVER raises — caller handles None gracefully.
    """
    try:
        from backend.database import AsyncSessionLocal as _SL
        from backend.models import Hypothesis
        from sqlalchemy import select as _sel
    except Exception as ex:
        logger.debug(f"[r1b_loop mutate] insert deps unavailable ({ex})")
        return None

    statement = str(pending.get("statement", "")).strip()
    if not statement:
        return None

    parent_depth = 0
    try:
        async with _SL() as _r1b_db:
            if parent_hypothesis_id is not None:
                parent = (
                    await _r1b_db.execute(
                        _sel(Hypothesis).where(Hypothesis.id == parent_hypothesis_id)
                    )
                ).scalar_one_or_none()
                if parent is not None:
                    parent_depth = int(
                        getattr(parent, "r1b_mutation_depth", 0) or 0
                    )

            new_row = Hypothesis(
                region=region or "USA",
                statement=statement,
                rationale=str(pending.get("rationale", "")),
                pillar=pending.get("pillar") or "unknown",
                key_fields=pending.get("key_fields") or [],
                suggested_operators=pending.get("suggested_operators") or [],
                dataset_pool=pending.get("selected_datasets") or [],
                parent_hypothesis_id=parent_hypothesis_id,
                r1b_mutation_depth=parent_depth + 1,
                kind="IMPROVEMENT_RULE",
                status="PROPOSED",
            )
            _r1b_db.add(new_row)
            await _r1b_db.commit()
            await _r1b_db.refresh(new_row)
            logger.info(
                f"[r1b_loop mutate] R1b.3-v2 INSERT hypothesis_id={new_row.id} "
                f"parent={parent_hypothesis_id} depth={new_row.r1b_mutation_depth}"
            )
            return new_row.id
    except Exception as ex:
        logger.warning(
            f"[r1b_loop mutate] R1b.3-v2 insert failed (round unaffected): {ex}"
        )
        return None


def _make_mutate_log_row(
    original: Any,
    idx: int,
    task_id: Any,
    round_idx: Any,
    model_name: str,
    original_metrics: Dict[str, Any],
    *,
    new_hypothesis_statement: Any,
    llm_changes_made: Any,
    outcome: str,
    loop_error: Any,
    llm_cost_usd: float,
    llm_tokens_used: int,
) -> Dict[str, Any]:
    import hashlib
    expr = getattr(original, "expression", "") or ""
    return {
        "task_id": task_id,
        "round_idx": round_idx,
        "attempt_type": "mutate_hyp",
        "triggering_attribution": original_metrics.get("_r1a_attribution"),
        "triggering_attribution_source": (
            "r5_judge" if original_metrics.get("_r5_c1_reason") else "r1a_heuristic"
        ),
        "original_expression_hash": hashlib.sha256(
            expr.encode("utf-8")
        ).hexdigest()[:64],
        "original_alpha_id_brain": getattr(original, "alpha_id", None),
        "original_hypothesis_id": original_metrics.get("hypothesis_id"),
        "original_quality_status": getattr(original, "quality_status", None),
        "new_expression": None,
        "new_hypothesis_statement": new_hypothesis_statement,
        "new_hypothesis_id": None,
        "llm_changes_made": llm_changes_made,
        "outcome": outcome,
        "outcome_alpha_id_brain": None,
        "outcome_sharpe": None,
        "outcome_fitness": None,
        "llm_cost_usd": llm_cost_usd,
        "llm_tokens_used": llm_tokens_used,
        "llm_model": model_name,
        "loop_error": loop_error,
    }


__all__ = [
    "node_code_gen_retry",
    "node_hypothesis_mutate",
    "_estimate_cost",
    "_write_r1b_retry_log_rows",
]
