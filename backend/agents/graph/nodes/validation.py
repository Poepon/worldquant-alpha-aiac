"""
Validation nodes for LangGraph workflow.

Redesigned based on RD-Agent principles:
- Learn from similar errors and their fixes
- Extract transferable knowledge from corrections
- No preconceived biases about error handling

Contains:
- node_validate: Batch validate alpha expressions
- node_self_correct: Attempt to fix invalid alphas with error pattern learning
"""

import time
from typing import Dict, List, Optional
from loguru import logger
from langchain_core.runnables import RunnableConfig

from backend.agents.graph.state import MiningState
from backend.agents.graph.nodes.base import record_trace, _debug_log
from backend.agents.services import LLMService
from backend.agents.prompts import SELF_CORRECT_SYSTEM, SELF_CORRECT_USER, build_self_correct_prompt
from backend.config import settings as _settings

from validator import ExpressionValidator
from backend.alpha_semantic_validator import (
    AlphaSemanticValidator,
    ExpressionDeduplicator,
    Finding,
    RuleId,
)
from backend.static_alpha_checks import run_static_suspicion_checks


# P1-E S-4: map structured rule_id → KB error_type category. Used by
# `_find_similar_errors` for high-precision rule-id-first lookup (KB rows
# without rule_id still fall back to the regex-based `_categorize_error`).
_RULE_ID_TO_CATEGORY: Dict[str, str] = {
    RuleId.EMPTY_EXPRESSION: "syntax",
    RuleId.UNKNOWN_OPERATOR: "operator_usage",
    RuleId.FIELD_NOT_FOUND: "field_name",
    RuleId.TYPE_MISMATCH_VECTOR_TS: "type_error",
    RuleId.LOW_COVERAGE_FIELD: "other",
    RuleId.RISK_DIVIDE_BY_VOLATILE_DENOM: "other",
    RuleId.RISK_HIGH_EXPONENT_SIGNED_POWER: "other",
    RuleId.RISK_SHORT_DECAY_WINDOW: "other",
    RuleId.RISK_EXTREME_WINSORIZATION: "other",
    RuleId.STATIC_LOOKAHEAD_BIAS: "other",
    RuleId.STATIC_DIVIDE_BY_ZERO: "other",
    RuleId.STATIC_OVERFIT_WINDOW: "other",
    RuleId.OTHER: "other",
}

# Static-check name → structured rule_id (M-5: dict→Finding bridge).
_STATIC_CHECK_TO_RULE_ID: Dict[str, str] = {
    "lookahead_bias": RuleId.STATIC_LOOKAHEAD_BIAS,
    "divide_by_zero": RuleId.STATIC_DIVIDE_BY_ZERO,
    "overfit_window": RuleId.STATIC_OVERFIT_WINDOW,
}

# Initialize Validators (Singleton-ish)
_VALIDATOR = ExpressionValidator()


# =============================================================================
# NODE: Validate
# =============================================================================

async def node_validate(state: MiningState, config: RunnableConfig = None) -> Dict:
    """
    Batch validate ALL pending alpha expressions.
    
    Enhanced with:
    - Semantic type validation (MATRIX/VECTOR constraints)
    - Deduplication gate (skip already-seen expressions)
    
    Input State:
        - pending_alphas
        - fields (with type info for semantic validation)
    
    Output Updates:
        - pending_alphas (with validation result)
        - trace_steps
    """
    start_time = time.time()
    node_name = "VALIDATE"
    
    trace_service = config.get("configurable", {}).get("trace_service") if config else None
    
    # Reset deduplicator for this batch.
    # V-26.59 (2026-05-13): similarity_threshold sourced from settings.
    batch_dedup = ExpressionDeduplicator(
        similarity_threshold=_settings.VALIDATE_DEDUP_SIMILARITY_THRESHOLD
    )
    
    updated_alphas = []
    valid_count = 0
    syntax_errors = []
    semantic_errors = []
    duplicate_count = 0
    type_warnings = []
    # V-P0: static suspicion checks moved pre-simulate. block = HARD look-ahead
    # invalidations routed to SELF_CORRECT; warn = SOFT divide/overfit annotations.
    static_block_count = 0
    static_warn_count = 0
    
    logger.info(f"[{node_name}] Starting batch validation | count={len(state.pending_alphas)}")
    
    # Build field list for validators
    allowed_fields = []
    for f in state.fields:
        if isinstance(f, dict):
            allowed_fields.append(f.get("id", f.get("name")))
        else:
            allowed_fields.append(str(f))
    
    _debug_log("D", "nodes.py:validate:fields", "Allowed fields for validation", {
        "allowed_fields": allowed_fields,
        "fields_count": len(allowed_fields),
        "expressions": [a.expression[:100] for a in state.pending_alphas]
    })
    
    # Initialize semantic validator with field type info.
    # Tier 2b (plan a-streamed-wren): feed the round's full operator catalog
    # (state.operators, ~66 ops from _get_operators) as the allowed set and
    # turn on reject_unknown_operators. The OperatorRegistry singleton is NOT
    # loaded in the Celery worker (only the FastAPI process loads it at
    # startup), so get_known_operators() is empty here — passing state.operators
    # is what makes hallucinated-operator rejection actually fire pre-sim. Falls
    # back to None (→ registry → footgun-safe skip) when state has no operators.
    _allowed_op_names = [
        o.get("name") for o in (state.operators or [])
        if isinstance(o, dict) and o.get("name")
    ]
    semantic_validator = AlphaSemanticValidator(
        fields=state.fields,
        operators=_allowed_op_names or None,
        strict_field_check=False,
        strict_type_check=True,
        reject_unknown_operators=True,
    )
    
    for alpha in state.pending_alphas:
        expression = alpha.expression
        is_valid = True
        error = None
        warnings = []
        # P1-E: structured findings collected for this alpha (semantic +
        # static-check adapter). Stamped to alpha.metrics at end of loop
        # (M-4: metrics, not metadata — persistence.py:275 only reads metrics).
        aggregated_findings: List[Finding] = []
        risk_bounds: Dict = {}

        if not expression or not expression.strip():
            is_valid = False
            error = "Empty expression"
        else:
            try:
                # Step 1: Deduplication check
                is_dup, dup_reason = batch_dedup.is_duplicate(expression)
                if is_dup:
                    is_valid = False
                    error = f"Duplicate: {dup_reason}"
                    duplicate_count += 1
                else:
                    batch_dedup.add(expression)
                    
                    # Step 2: Syntax validation
                    syntax_result = _VALIDATOR.check_expression(
                        expression, allowed_fields=allowed_fields
                    )
                    if not syntax_result.get("valid", False):
                        is_valid = False
                        err_list = syntax_result.get("errors", [])
                        error = "; ".join(err_list) if err_list else "Syntax error"
                        syntax_errors.append(error)
                    else:
                        # Step 3: Semantic validation (type constraints).
                        # P1-E: consume structured Finding list instead of
                        # str lists. sem_result.findings holds the canonical
                        # records; .errors/.warnings are derived views.
                        sem_result = semantic_validator.validate(expression)
                        aggregated_findings.extend(sem_result.findings)
                        risk_bounds = sem_result.risk_bounds or {}

                        hard_findings = [
                            f for f in sem_result.findings if f.severity == "hard"
                        ]
                        soft_findings = [
                            f for f in sem_result.findings
                            if f.severity in ("soft", "info")
                        ]

                        # Soft+info findings — surface as SELF_CORRECT context
                        # regardless of validity. Tag with rule_id so the LLM
                        # prompt and KB lookup can disambiguate.
                        if soft_findings:
                            warnings.extend(
                                f"[{f.rule_id}] {f.message}" for f in soft_findings
                            )
                            type_warnings.extend(
                                f"[{f.rule_id}] {f.message}"
                                for f in soft_findings[:2]
                            )

                        if hard_findings:
                            # V-15 (2026-05-03 spike 2.0): semantic hard
                            # findings invalidate so SELF_CORRECT can rewrite.
                            is_valid = False
                            error = "; ".join(
                                f.message for f in hard_findings[:2]
                            )
                            semantic_errors.extend(
                                f.message for f in hard_findings[:2]
                            )

                        # Step 4: static suspicion checks (V-P0 2026-05-15).
                        # P1-E M-5: adapt static dict→Finding so the unified
                        # _validation_findings container also sees these.
                        # Look-ahead bias / divide-by-zero / overfit-window are
                        # expression-only — moved here from node_evaluate so a
                        # bad expression never burns a BRAIN sim, with no
                        # sharpe>3 gate. HARD (look-ahead) invalidates → routes
                        # to SELF_CORRECT; SOFT (divide / overfit) annotate as
                        # warnings only and still simulate.
                        static_flags = run_static_suspicion_checks(expression)
                        if static_flags:
                            static_findings: List[Finding] = []
                            for sf in static_flags:
                                check_name = sf.get("check", "")
                                rule_id = _STATIC_CHECK_TO_RULE_ID.get(
                                    check_name, f"static_{check_name}",
                                )
                                static_findings.append(Finding(
                                    rule_id=rule_id,
                                    severity=sf.get("severity", "soft"),
                                    message=sf.get("evidence", check_name),
                                    category="risk",
                                    metadata={"check": check_name},
                                ))
                            aggregated_findings.extend(static_findings)

                            soft = [
                                (sf, f) for sf, f in zip(static_flags, static_findings)
                                if f.severity == "soft"
                            ]
                            hard = [
                                (sf, f) for sf, f in zip(static_flags, static_findings)
                                if f.severity == "hard"
                            ]
                            # SOFT — keep the legacy `[check] evidence` warning
                            # string so backward-compat assertions (e.g.
                            # `"divide_by_zero" in validation_error`) still hold.
                            for sf, _f in soft:
                                warnings.append(
                                    f"[{sf['check']}] {sf['evidence']}"
                                )
                            if soft:
                                static_warn_count += len(soft)
                            if hard:
                                hard_msg = "; ".join(
                                    f"{sf['check']}: {sf['evidence']}"
                                    for sf, _f in hard
                                )
                                static_block_count += len(hard)
                                if is_valid:
                                    is_valid = False
                                    error = hard_msg
                                    semantic_errors.append(hard_msg)
                                else:
                                    # already invalidated upstream — keep the
                                    # first error but surface look-ahead as
                                    # extra context for SELF_CORRECT.
                                    error = f"{error}; [also] {hard_msg}"

            except Exception as e:
                is_valid = False
                error = f"Validation Exception: {str(e)}"
        
        updated_alpha = alpha.model_copy()
        updated_alpha.is_valid = is_valid

        # Surface warnings as extra SELF_CORRECT context even when the
        # expression is already invalid — the primary error stays first.
        if warnings:
            warn_str = f"[WARNINGS] {'; '.join(warnings[:3])}"
            updated_alpha.validation_error = (
                f"{error} | {warn_str}" if error else warn_str
            )
        else:
            updated_alpha.validation_error = error

        # P1-E M-4: stamp structured findings to `alpha.metrics` (JSONB,
        # persisted by persistence.py:275). NOT `alpha.metadata` —
        # AlphaCandidate.metadata is never read by node_save_results, so
        # writing there would silently produce zero effect.
        # V-26.79 pattern: `updated_alpha = alpha.model_copy()` is shallow,
        # so `updated_alpha.metrics` is the SAME dict object as
        # `alpha.metrics`. Mutating it would write through to the
        # LangGraph input state, corrupting it for replay/interrupt-resume.
        # Detach with dict() before any mutation.
        if aggregated_findings or risk_bounds:
            updated_alpha.metrics = (
                dict(updated_alpha.metrics) if isinstance(updated_alpha.metrics, dict)
                else {}
            )
        if aggregated_findings:
            updated_alpha.metrics["_validation_findings"] = [
                f.to_dict() for f in aggregated_findings
            ]
        if risk_bounds:
            updated_alpha.metrics["_risk_bounds"] = risk_bounds
        
        if is_valid:
            valid_count += 1
        else:
            if error and "Duplicate" not in error:
                syntax_errors.append(f"{expression[:50]}... -> {error}")
        
        updated_alphas.append(updated_alpha)
    
    duration_ms = int((time.time() - start_time) * 1000)
    
    _debug_log("D", "nodes.py:validate:result", "Validation complete", {
        "total": len(updated_alphas),
        "valid": valid_count,
        "invalid": len(updated_alphas) - valid_count,
        "duplicates": duplicate_count,
        "syntax_error_count": len(syntax_errors),
        "duration_ms": duration_ms,
        "pass_rate": round(valid_count / max(1, len(updated_alphas)) * 100, 1)
    })
    
    logger.info(
        f"[{node_name}] Complete | valid={valid_count}/{len(updated_alphas)} "
        f"duplicates={duplicate_count} type_warnings={len(type_warnings)}"
    )
    
    if syntax_errors:
        logger.warning(f"[{node_name}] Syntax Errors: {syntax_errors[:3]}")
    if semantic_errors:
        logger.warning(f"[{node_name}] Semantic Warnings: {semantic_errors[:3]}")
    
    trace_update = await record_trace(
        state, trace_service, node_name,
        {"count": len(updated_alphas)},
        {
            "valid_count": valid_count,
            "invalid_count": len(updated_alphas) - valid_count,
            "duplicate_count": duplicate_count,
            "static_block_count": static_block_count,
            "static_warn_count": static_warn_count,
            "type_warnings": type_warnings[:5],
            "failures": [
                {"expression": a.expression[:100], "error": a.validation_error}
                for a in updated_alphas if not a.is_valid
            ][:10]
        },
        duration_ms,
        "SUCCESS"
    )
    
    return {
        "pending_alphas": updated_alphas,
        **trace_update
    }


# =============================================================================
# NODE: Self-Correct
# =============================================================================

# V-26.17 (2026-05-13): error knowledge base for SELF_CORRECT learning.
# Pre-fix this was a module-level Python list — wiped on worker restart,
# unsharable across celery workers. Now backed by Redis via
# backend.tasks.redis_pool (cross-worker, survives worker restart, capped
# at 200 entries with LTRIM-oldest eviction).
#
# `_ERROR_KNOWLEDGE_BASE` is preserved as an in-memory fallback for two
# narrow cases:
#   1. Redis unreachable — error_kb_load returns [] and we fall back to
#      whatever this worker has accumulated this session.
#   2. Tests that import the symbol directly (kept for backward-compat).
_ERROR_KNOWLEDGE_BASE: List[Dict] = []


import re as _re_categorize

# V-26.55 (2026-05-13): match category keywords on word boundaries so
# tokens like "matrix_norm" or "vec_matrix" don't pull every error into
# the "type_error" bucket regardless of the actual failure mode. The KB
# pivots on category for "similar errors" retrieval, so mis-classification
# leaks irrelevant correction examples to SELF_CORRECT.
_CATEGORY_PATTERNS = (
    ("field_name",     _re_categorize.compile(r"\b(field|unknown)\b", _re_categorize.IGNORECASE)),
    ("syntax",         _re_categorize.compile(r"\b(syntax|parse)\b",  _re_categorize.IGNORECASE)),
    ("operator_usage", _re_categorize.compile(r"\b(operator|function)\b", _re_categorize.IGNORECASE)),
    ("type_error",     _re_categorize.compile(r"\b(type|matrix|vector)\b", _re_categorize.IGNORECASE)),
    ("duplicate",      _re_categorize.compile(r"\b(duplicate)\b", _re_categorize.IGNORECASE)),
)


def _categorize_error(error_message: str) -> str:
    """Categorize error type for knowledge matching.

    V-26.55: previously used substring `in` which collapsed any error
    that contained "matrix"/"vector" anywhere (including field names
    like `matrix_norm`) into "type_error". Now uses `\\b...\\b` regex
    so the keyword must be a whole token.
    """
    if not error_message:
        return "other"
    for label, pat in _CATEGORY_PATTERNS:
        if pat.search(error_message):
            return label
    return "other"


def _find_similar_errors(
    error_message: str,
    error_type: str,
    knowledge_base: List[Dict],
    max_results: int = 3,
    rule_id: Optional[str] = None,
) -> List[Dict]:
    """Find similar errors from knowledge base for learning.

    P1-E S-5: prefer rule_id exact match (high-precision lookup) for new
    Finding-aware KB rows. Falls back to error_category match (regex-based
    `_categorize_error`) for legacy rule_id-less entries, so existing KB
    rows keep working without backfill.
    """
    similar: List[Dict] = []
    seen_ids = set()

    # Phase 1: rule_id exact match (only matches new KB rows that carry rule_id).
    if rule_id:
        for entry in knowledge_base:
            if entry.get("rule_id") == rule_id:
                eid = id(entry)
                if eid in seen_ids:
                    continue
                similar.append(entry)
                seen_ids.add(eid)
                if len(similar) >= max_results:
                    return similar

    # Phase 2: regex-based category fallback (backward-compat with legacy KB).
    error_category = _categorize_error(error_message)
    for entry in knowledge_base:
        if id(entry) in seen_ids:
            continue
        if entry.get("error_category") == error_category:
            similar.append(entry)
            seen_ids.add(id(entry))
            if len(similar) >= max_results:
                break

    return similar


def _record_correction(
    original_expression: str,
    fixed_expression: str,
    error_message: str,
    error_type: str,
    fix_description: str,
    rule_id: Optional[str] = None,
) -> None:
    """Record a successful correction for future learning.

    V-26.17: writes through to the Redis-backed cross-worker store and
    also appends to the in-memory list for the same-process / Redis-down
    fallback case.

    P1-E S-5: optional `rule_id` lets `_find_similar_errors` perform
    exact-match lookup on new entries (legacy entries lack the field and
    fall through to category-based regex matching).
    """
    global _ERROR_KNOWLEDGE_BASE
    entry = {
        "failed_expression": original_expression,
        "fixed_expression": fixed_expression,
        "error": error_message,
        "error_category": _categorize_error(error_message),
        "fix_description": fix_description,
    }
    if rule_id:
        entry["rule_id"] = rule_id
    try:
        from backend.tasks.redis_pool import error_kb_record
        error_kb_record(entry)
    except Exception as exc:
        # Module import failure shouldn't break the in-flight node — log
        # and keep going on the in-memory fallback below.
        logger.warning(f"[validate] V-26.17 redis kb record failed: {exc}")
    _ERROR_KNOWLEDGE_BASE.append(entry)
    if len(_ERROR_KNOWLEDGE_BASE) > 100:
        _ERROR_KNOWLEDGE_BASE = _ERROR_KNOWLEDGE_BASE[-50:]


def _load_correction_kb(max_entries: int = 100) -> List[Dict]:
    """V-26.17: return the active correction examples, preferring the
    Redis cross-worker store. Falls back to the in-memory list when
    Redis is empty or unreachable."""
    try:
        from backend.tasks.redis_pool import error_kb_load
        redis_kb = error_kb_load(max_entries=max_entries)
        if redis_kb:
            return redis_kb
    except Exception as exc:
        logger.warning(f"[validate] V-26.17 redis kb load failed: {exc}")
    return list(_ERROR_KNOWLEDGE_BASE)


async def node_self_correct(
    state: MiningState,
    llm_service: LLMService,
    config: RunnableConfig = None
) -> Dict:
    """
    Batch attempt to fix ALL invalid alphas with error pattern learning.
    
    Redesigned based on RD-Agent principles:
    - Learn from similar errors and their successful fixes
    - Extract transferable knowledge ("If this error, then this fix")
    - Multiple fix approaches without prescriptive bias
    
    Input State:
        - pending_alphas
        - retry_count
    
    Output Updates:
        - pending_alphas (updated)
        - retry_count
        - knowledge_extracted (new corrections for future learning)
    """
    start_time = time.time()
    node_name = "SELF_CORRECT"
    
    trace_service = config.get("configurable", {}).get("trace_service") if config else None
    
    # V-26.57 (2026-05-13): defense-in-depth retry cap. The LangGraph
    # router (edges.py:route_after_validate) is the primary guard against
    # infinite SELF_CORRECT loops, but a router bug / graph rewrite would
    # silently let this node spin. Self-check here so the node owns its
    # own bound. Also lets unit tests invoke node_self_correct directly
    # without wiring the full graph.
    if state.retry_count >= state.max_retries:
        logger.warning(
            f"[{node_name}] V-26.57 retry_count={state.retry_count} >= "
            f"max_retries={state.max_retries}; refusing to attempt further fixes"
        )
        return {"retry_count": state.retry_count}

    # Identify invalid alphas
    invalid_indices = [
        i for i, a in enumerate(state.pending_alphas)
        if not a.is_valid
    ]

    if not invalid_indices:
        logger.info(f"[{node_name}] No alphas need correction")
        return {"retry_count": state.retry_count + 1}

    logger.info(f"[{node_name}] Starting batch fix | count={len(invalid_indices)} pass={state.retry_count + 1}/{state.max_retries}")
    
    # Build allowed fields list
    allowed_fields = []
    for f in state.fields[:50]:
        fid = f.get('id', f.get('name', ''))
        if fid:
            allowed_fields.append(fid)
    
    updated_alphas = list(state.pending_alphas)
    fixed_count = 0
    corrections_made = []
    knowledge_extracted = []
    
    # V-26.17: load shared cross-worker correction KB (Redis-backed). Falls
    # back to the in-memory list if Redis is unreachable.
    correction_kb = _load_correction_kb(max_entries=100)

    for idx in invalid_indices:
        current = state.pending_alphas[idx]
        error_message = current.validation_error or "Unknown error"
        error_type = _categorize_error(error_message)

        # P1-E: reconstruct structured Findings from alpha.metrics for the
        # SELF_CORRECT prompt + rule-id-aware KB lookup. Falls back to
        # synthetic Finding from (error_message, error_type) if the alpha
        # came from a legacy pre-P1-E path that didn't stamp findings.
        stamped = (current.metrics or {}).get("_validation_findings") or []
        findings_for_prompt: List[Finding] = []
        for entry in stamped:
            f = Finding.from_dict(entry)
            if f is not None:
                findings_for_prompt.append(f)

        primary_rule_id: Optional[str] = None
        if findings_for_prompt:
            # Use the first hard finding's rule_id for KB lookup (high-precision
            # match against rule_id-aware KB rows).
            hard = [f for f in findings_for_prompt if f.severity == "hard"]
            primary_rule_id = (hard[0].rule_id if hard else findings_for_prompt[0].rule_id)

        # Find similar errors for learning — prefer rule_id exact match.
        similar_errors = _find_similar_errors(
            error_message, error_type, correction_kb, rule_id=primary_rule_id,
        )

        if similar_errors:
            logger.debug(f"[{node_name}] Found {len(similar_errors)} similar errors for learning")

        # Use enhanced prompt builder with structured Findings. Backward-compat
        # `error_message`/`error_type` keep flowing for legacy alpha rows.
        prompt = build_self_correct_prompt(
            expression=current.expression,
            findings=findings_for_prompt if findings_for_prompt else None,
            error_message=error_message,
            error_type=error_type,
            available_fields=allowed_fields,
            similar_errors=similar_errors if similar_errors else None,
        )
        
        try:
            # V-26.59 (2026-05-13): temperature sourced from settings.
            response = await llm_service.call(
                system_prompt=SELF_CORRECT_SYSTEM,
                user_prompt=prompt,
                temperature=_settings.SELF_CORRECT_TEMPERATURE,
                json_mode=True,
                node_key="self_correct",
            )
            
            updated_alpha = current.model_copy()
            updated_alpha.correction_attempts += 1
            if not updated_alpha.original_expression:
                updated_alpha.original_expression = current.expression
            
            if response.success and response.parsed:
                parsed = response.parsed
                
                # Handle both old format (fixed_expression) and new format (fix.fixed_expression)
                fix_data = parsed.get("fix", {})
                fixed = fix_data.get("fixed_expression") if isinstance(fix_data, dict) else None
                if not fixed:
                    fixed = parsed.get("fixed_expression")
                
                if fixed:
                    # Get fix description
                    changes_made = fix_data.get("changes_made", "") if isinstance(fix_data, dict) else ""
                    if not changes_made:
                        changes_made = parsed.get("changes_made", "")

                    # === Phase 2 R7 semi-acceptance (2026-05-18) ===
                    # rd_agent §6 R7 Co-STEER `should_use_new_evo`:
                    # quick re-validate the LLM fix before overwriting; only
                    # accept when new is VALID OR has strictly fewer hard
                    # findings than original. Default OFF preserves legacy
                    # "always overwrite + retry loop" behavior.
                    _r7_accept = True
                    _r7_reason = "legacy_always_accept"
                    if getattr(_settings, "ENABLE_SELF_CORRECT_SEMI_ACCEPT", False):
                        try:
                            from backend.alpha_semantic_validator import validate_alpha_semantically  # noqa: E402
                            _new_val = validate_alpha_semantically(
                                expression=fixed,
                                fields=state.fields[:50],
                                operators=None,
                                strict=False,
                            )
                            _new_err_count = len(_new_val.get("errors", []) or [])
                            # M2 review fix (2026-05-18): track whether the
                            # `or 1` fallback fired. Legacy alphas (no
                            # _validation_findings cache) or alphas with only
                            # non-hard findings reported orig=0 originally,
                            # the `or 1` bumped it to 1 nominal, and the
                            # strict `<` comparator then rejected any new
                            # candidate with ≥1 error — i.e. legacy alphas
                            # could never be auto-fixed. Relax the comparator
                            # to `<=` only when the fallback fired so a fix
                            # producing 1 hard error is still accepted.
                            _findings_list = [
                                f for f in (current.metrics or {}).get("_validation_findings", [])
                                if isinstance(f, dict) and f.get("severity") == "hard"
                            ]
                            _orig_err_count = len(_findings_list)
                            _orig_count_fallback = (_orig_err_count == 0)
                            if _orig_count_fallback:
                                _orig_err_count = 1  # nominal; original failed validation so ≥1
                            if _new_val.get("valid"):
                                _r7_reason = f"new_valid (orig_errs={_orig_err_count}{', fallback' if _orig_count_fallback else ''})"
                            elif _orig_count_fallback and _new_err_count <= _orig_err_count:
                                # Relaxed comparator: 1 hard error in fix OK
                                # when original count is unknown / inferred.
                                _r7_reason = f"relaxed_fewer_or_eq_errs ({_new_err_count}<={_orig_err_count}, fallback)"
                            elif (not _orig_count_fallback) and _new_err_count < _orig_err_count:
                                _r7_reason = f"strict_fewer_errs ({_new_err_count}<{_orig_err_count})"
                            else:
                                _r7_accept = False
                                _cmp = "<=" if _orig_count_fallback else "<"
                                _r7_reason = f"rejected (new={_new_err_count}, orig={_orig_err_count}, cmp={_cmp}{', fallback' if _orig_count_fallback else ''})"
                        except Exception as _r7_e:  # noqa: BLE001
                            logger.debug(f"[{node_name}] R7 re-validate failed (non-fatal, defaulting accept): {_r7_e}")
                            _r7_reason = f"r7_check_error:{str(_r7_e)[:60]}"

                    if not _r7_accept:
                        # R7 reject: keep original expression, mark + don't reset is_valid
                        _new_metrics = dict(updated_alpha.metrics) if isinstance(updated_alpha.metrics, dict) else {}
                        _new_metrics["_r7_self_correct_rejected"] = True
                        _new_metrics["_r7_self_correct_reason"] = _r7_reason
                        _new_metrics["_r7_rejected_candidate"] = fixed[:200]
                        updated_alpha.metrics = _new_metrics
                        logger.info(
                            f"[{node_name}] R7 reject overwrite idx={idx}: {_r7_reason}"
                        )
                    else:
                        # Accept: legacy path
                        corrections_made.append({
                            "original": current.expression,
                            "fixed": fixed,
                            "error": error_message,
                            "changes": changes_made
                        })

                        updated_alpha.expression = fixed
                        updated_alpha.is_valid = None
                        updated_alpha.validation_error = None
                        fixed_count += 1

                        # Record for future learning (P1-E S-5: stamp rule_id
                        # so the new entry is rule-id-lookup-eligible).
                        # Phase 2 R7 (2026-05-18): moved inside accept branch
                        # so rejected fixes don't pollute correction KB.
                        _record_correction(
                            original_expression=current.expression,
                            fixed_expression=fixed,
                            error_message=error_message,
                            error_type=error_type,
                            fix_description=changes_made,
                            rule_id=primary_rule_id,
                        )

                        # Extract transferable knowledge — also only on accept
                        knowledge = parsed.get("knowledge_extracted")
                        if knowledge:
                            knowledge_extracted.append(knowledge)
            
            updated_alphas[idx] = updated_alpha
            
        except Exception as e:
            logger.error(f"[{node_name}] Fix failed for index {idx}: {e}")
    
    duration_ms = int((time.time() - start_time) * 1000)
    
    logger.info(f"[{node_name}] Complete | fixed_attempts={fixed_count}/{len(invalid_indices)}")
    
    if knowledge_extracted:
        logger.info(f"[{node_name}] Extracted {len(knowledge_extracted)} knowledge rules")
        for rule in knowledge_extracted[:3]:
            logger.debug(f"[{node_name}] Rule: {rule}")
    
    trace_update = await record_trace(
        state, trace_service, node_name,
        {
            "fix_targets": len(invalid_indices),
            "similar_errors_found": sum(1 for _ in _ERROR_KNOWLEDGE_BASE)
        },
        {
            "fixed_count": fixed_count,
            "corrections": corrections_made,
            "knowledge_extracted": knowledge_extracted
        },
        duration_ms,
        "SUCCESS"
    )
    
    return {
        "pending_alphas": updated_alphas,
        "retry_count": state.retry_count + 1,
        **trace_update
    }
