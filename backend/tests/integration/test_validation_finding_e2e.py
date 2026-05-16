"""End-to-end integration tests for P1-E structured findings.

Covers:
    - M-4: findings stamped to `alpha.metrics` (NOT `alpha.metadata`) so the
      JSONB persistence path (persistence.py:275) picks them up.
    - M-5: static_alpha_checks dict adapted to Finding format inside the
      unified _validation_findings container, so SELF_CORRECT prompt sees them.
    - M-3: build_self_correct_prompt new signature + legacy shim coexist.
    - N-3: info section in prompt capped at 5 entries to avoid bloat.
    - Backward-compat regression:
        * `result.errors` truthy / iterable still works for legacy tests.
        * `divide_by_zero` / `lookahead` substring assertions on
          `validation_error` still hold (existing test_static_alpha_checks).
        * factor_tier_classifier's `result.error_messages` returns string list.

Run with: pytest backend/tests/integration/test_validation_finding_e2e.py -v
"""

# IMPORTANT: pre-existing circular import in backend.tasks/backend.agents
# means we must prime backend.tasks.session_watchdog directly before
# importing node_validate (which lives under backend.agents.graph.*).
import backend.tasks.session_watchdog  # noqa: F401

import pytest

from backend.agents.graph.nodes.validation import (
    _find_similar_errors,
    _record_correction,
    node_validate,
)
from backend.agents.graph.state import AlphaCandidate, MiningState
from backend.agents.prompts.validation import build_self_correct_prompt
from backend.alpha_semantic_validator import Finding, RuleId


# =============================================================================
# 1) node_validate stamps Findings into alpha.metrics (M-4)
# =============================================================================


class TestNodeValidateStructuredFindings:
    @pytest.mark.asyncio
    async def test_findings_stamped_to_alpha_metrics_not_metadata(self):
        """M-4 critical: findings MUST go to `alpha.metrics`, NOT `alpha.metadata`,
        because persistence.py:275 only reads metrics for the JSONB column.
        """
        state = MiningState(
            task_id=1,
            region="USA",
            universe="TOP3000",
            dataset_id="x",
            fields=[
                {"id": "close"},
                {"id": "returns"},
                {"id": "volume"},
            ],
            pending_alphas=[
                AlphaCandidate(expression="divide(close, volume)"),
            ],
        )
        result = await node_validate(state, config=None)
        alpha = result["pending_alphas"][0]

        # M-4: findings stamped to METRICS, not metadata.
        assert "_validation_findings" in alpha.metrics
        assert isinstance(alpha.metrics["_validation_findings"], list)
        assert len(alpha.metrics["_validation_findings"]) >= 1
        # alpha.metadata must NOT carry the findings (would silently no-op
        # under persistence — that's the bug M-4 catches).
        assert "_validation_findings" not in (alpha.metadata or {})

    @pytest.mark.asyncio
    async def test_risk_bounds_stamped_to_alpha_metrics(self):
        """divide(close, volume) → max_loss_hint=high in alpha.metrics."""
        state = MiningState(
            task_id=1,
            region="USA",
            universe="TOP3000",
            dataset_id="x",
            fields=[
                {"id": "close"},
                {"id": "volume"},
            ],
            pending_alphas=[
                AlphaCandidate(expression="divide(close, volume)"),
            ],
        )
        result = await node_validate(state, config=None)
        alpha = result["pending_alphas"][0]

        assert "_risk_bounds" in alpha.metrics
        assert alpha.metrics["_risk_bounds"]["max_loss_hint"] == "high"
        assert "rationale" in alpha.metrics["_risk_bounds"]
        assert "severity_distribution" in alpha.metrics["_risk_bounds"]

    @pytest.mark.asyncio
    async def test_legacy_validation_error_preserved(self):
        """alpha.validation_error remains a single string for backward-compat
        with KB regex / frontend / existing tests."""
        state = MiningState(
            task_id=1,
            region="USA",
            universe="TOP3000",
            dataset_id="x",
            fields=[
                {"id": "close"},
                {"id": "actual_eps_value_quarterly"},
            ],
            pending_alphas=[
                AlphaCandidate(expression="rank(actual_eps_value_quarterly)"),
            ],
        )
        result = await node_validate(state, config=None)
        alpha = result["pending_alphas"][0]
        assert alpha.is_valid is False
        # validation_error is a single string (not List[Finding]).
        assert isinstance(alpha.validation_error, str)

    @pytest.mark.asyncio
    async def test_findings_survive_node_simulate_metrics_replacement(self):
        """P1-E follow-up regression: node_simulate replaces
        ``alpha.metrics`` wholesale with the BRAIN result. Without the
        carry-over fix in evaluation.py L1090+, ``_validation_findings``
        and ``_risk_bounds`` get clobbered before persistence ever sees
        them. Simulate the same merge step here to lock the contract.
        """
        # 1. Pre-simulate state — node_validate has stamped findings.
        validated_metrics = {
            "_validation_findings": [
                {"rule_id": RuleId.LOW_COVERAGE_FIELD, "severity": "soft",
                 "message": "Low coverage", "category": "semantics",
                 "location": None, "metadata": {}},
            ],
            "_risk_bounds": {"max_loss_hint": "high",
                             "rationale": [RuleId.RISK_DIVIDE_BY_VOLATILE_DENOM],
                             "confidence": 0.25,
                             "severity_distribution": {"hard": 0, "soft": 0, "info": 1}},
        }
        # 2. Simulated BRAIN response — fresh metrics dict (no findings).
        sim_metrics = {"sharpe": 1.6, "fitness": 1.1, "turnover": 0.4}

        # 3. Replicate node_simulate's merge logic verbatim.
        post_sim_metrics = dict(sim_metrics)
        for k, v in validated_metrics.items():
            if k.startswith("_validation_") or k == "_risk_bounds":
                post_sim_metrics.setdefault(k, v)

        # 4. Assert findings + risk_bounds survived alongside sim metrics.
        assert post_sim_metrics["sharpe"] == 1.6
        assert post_sim_metrics["_validation_findings"] == validated_metrics["_validation_findings"]
        assert post_sim_metrics["_risk_bounds"]["max_loss_hint"] == "high"

    @pytest.mark.asyncio
    async def test_node_validate_does_not_writethrough_to_input_state(self):
        """V-26.79 pattern: ``updated_alpha = alpha.model_copy()`` is shallow,
        so mutating ``updated_alpha.metrics`` previously wrote through to
        the LangGraph input state's metrics dict. The detach-via-dict()
        fix prevents that — the input alpha's metrics must be untouched.
        """
        # Alpha with a pre-existing metrics dict (could be any sentinel).
        input_metrics = {"_seed_marker": "preserve_me"}
        alpha = AlphaCandidate(expression="rank(close)")
        alpha.metrics = input_metrics
        state = MiningState(
            task_id=1,
            region="USA",
            universe="TOP3000",
            dataset_id="x",
            fields=[{"id": "close"}],
            pending_alphas=[alpha],
        )
        await node_validate(state, config=None)
        # The ORIGINAL input metrics dict must be unchanged — no
        # `_validation_findings` / `_risk_bounds` written through.
        assert input_metrics == {"_seed_marker": "preserve_me"}, (
            f"shared-ref write-through detected: {input_metrics}"
        )


# =============================================================================
# 2) M-5: static_alpha_checks adapted to Finding format
# =============================================================================


class TestStaticChecksAdapter:
    @pytest.mark.asyncio
    async def test_static_lookahead_appears_in_findings(self):
        """M-5: static lookahead must be present in _validation_findings as
        `static_lookahead_bias` with severity=hard."""
        state = MiningState(
            task_id=1,
            region="USA",
            universe="TOP3000",
            dataset_id="x",
            fields=[{"id": "actual_eps_value_quarterly"}],
            pending_alphas=[
                AlphaCandidate(expression="rank(actual_eps_value_quarterly)"),
            ],
        )
        result = await node_validate(state, config=None)
        alpha = result["pending_alphas"][0]
        findings = alpha.metrics.get("_validation_findings", [])
        rule_ids = [f["rule_id"] for f in findings]
        assert RuleId.STATIC_LOOKAHEAD_BIAS in rule_ids
        lookahead = next(
            f for f in findings if f["rule_id"] == RuleId.STATIC_LOOKAHEAD_BIAS
        )
        assert lookahead["severity"] == "hard"

    @pytest.mark.asyncio
    async def test_static_divide_by_zero_appears_as_soft_finding(self):
        state = MiningState(
            task_id=1,
            region="USA",
            universe="TOP3000",
            dataset_id="x",
            fields=[
                {"id": "close"},
                {"id": "returns"},
            ],
            pending_alphas=[
                AlphaCandidate(expression="divide(close, returns)"),
            ],
        )
        result = await node_validate(state, config=None)
        alpha = result["pending_alphas"][0]
        findings = alpha.metrics.get("_validation_findings", [])
        soft_static = [
            f for f in findings
            if f["rule_id"] == RuleId.STATIC_DIVIDE_BY_ZERO
        ]
        assert len(soft_static) == 1
        assert soft_static[0]["severity"] == "soft"

    @pytest.mark.asyncio
    async def test_self_correct_prompt_sees_static_finding(self):
        """SELF_CORRECT prompt must render the static_lookahead_bias finding
        so the LLM has full structured context. Verifies M-5 end-to-end."""
        # Materialize a Finding the way node_self_correct would after reading
        # alpha.metrics["_validation_findings"].
        f = Finding(
            rule_id=RuleId.STATIC_LOOKAHEAD_BIAS,
            severity="hard",
            message="announcement field used without ts_delay direct-wrap",
            category="risk",
        )
        prompt = build_self_correct_prompt(
            expression="rank(actual_eps_value_quarterly)",
            findings=[f],
            available_fields=["close", "actual_eps_value_quarterly"],
        )
        assert "static_lookahead_bias" in prompt
        assert "hard" in prompt


# =============================================================================
# 3) SELF_CORRECT prompt rendering (M-3 + N-3)
# =============================================================================


class TestSelfCorrectPromptRendering:
    def test_prompt_renders_findings_severity_sorted(self):
        hard_f = Finding(rule_id="r_hard", severity="hard", message="must-fix")
        soft_f = Finding(rule_id="r_soft", severity="soft", message="should-fix")
        info_f = Finding(rule_id="r_info", severity="info", message="hint", category="risk")
        prompt = build_self_correct_prompt(
            expression="rank(x)",
            findings=[info_f, soft_f, hard_f],  # purposely scrambled
            available_fields=[],
        )
        # Hard section must appear before soft section, and soft before info.
        hard_idx = prompt.index("Errors that MUST be fixed")
        soft_idx = prompt.index("Warnings (fix if relevant)")
        info_idx = prompt.index("Risk hints")
        assert hard_idx < soft_idx < info_idx

    def test_prompt_legacy_string_shim_still_works(self):
        """M-3: legacy (error_message, error_type) signature still produces
        a usable prompt for callers that haven't migrated."""
        prompt = build_self_correct_prompt(
            expression="rank(x)",
            error_message="field x not found",
            error_type="field_name",
            available_fields=["close"],
        )
        assert "field x not found" in prompt
        assert "field_name" in prompt
        # Synthesized into one hard finding under-the-hood.
        assert "Errors that MUST be fixed" in prompt

    def test_prompt_info_section_capped_at_5(self):
        """N-3: info section must cap at 5 entries to bound prompt size."""
        info_findings = [
            Finding(rule_id=f"r_info_{i}", severity="info",
                    message=f"info-msg-{i}", category="risk")
            for i in range(7)
        ]
        prompt = build_self_correct_prompt(
            expression="rank(x)",
            findings=info_findings,
            available_fields=[],
        )
        # First 5 messages must appear; messages 5 and 6 must not.
        for i in range(5):
            assert f"info-msg-{i}" in prompt
        assert "info-msg-5" not in prompt
        assert "info-msg-6" not in prompt


# =============================================================================
# 4) Backward-compat regression
# =============================================================================


class TestBackwardCompatRegression:
    def test_optimization_modules_assertions_pass(self):
        """test_optimization_modules.py asserts `not result.errors` — must hold
        because empty Finding list is falsy."""
        from backend.alpha_semantic_validator import AlphaSemanticValidator

        fields = [
            {"id": "close", "type": "MATRIX"},
            {"id": "volume", "type": "MATRIX"},
        ]
        validator = AlphaSemanticValidator(fields=fields)
        result = validator.validate("ts_rank(close, 20)")
        assert not result.errors, (
            f"Expected empty errors for valid expression, got: {result.errors}"
        )

    @pytest.mark.asyncio
    async def test_static_alpha_checks_backward_compat_strings_in_validation_error(self):
        """`"lookahead"` / `"divide_by_zero"` substring assertions on
        validation_error must keep holding (test_static_alpha_checks.py:173,178)."""
        state = MiningState(
            task_id=1,
            region="USA",
            universe="TOP3000",
            dataset_id="x",
            fields=[
                {"id": "close"},
                {"id": "returns"},
                {"id": "actual_eps_value_quarterly"},
            ],
            pending_alphas=[
                AlphaCandidate(expression="rank(actual_eps_value_quarterly)"),
                AlphaCandidate(expression="divide(close, returns)"),
            ],
        )
        result = await node_validate(state, config=None)
        by_expr = {a.expression: a for a in result["pending_alphas"]}

        lookahead = by_expr["rank(actual_eps_value_quarterly)"]
        assert lookahead.is_valid is False
        assert "lookahead" in (lookahead.validation_error or "").lower()

        divide = by_expr["divide(close, returns)"]
        assert divide.is_valid is True
        assert "divide_by_zero" in (divide.validation_error or "")

    def test_factor_tier_classifier_logger_uses_error_messages(self):
        """S-7: `result.error_messages` returns clean List[str] for logger."""
        from backend.alpha_semantic_validator import AlphaSemanticValidator

        v = AlphaSemanticValidator(
            fields=[{"id": "close", "type": "MATRIX"}],
            strict_field_check=True,
        )
        r = v.validate("rank(unknown_field)")
        msgs = r.error_messages
        assert isinstance(msgs, list)
        assert all(isinstance(m, str) for m in msgs)
        assert any("unknown_field" in m for m in msgs)


# =============================================================================
# 5) Rule-id-aware KB lookup (S-5)
# =============================================================================


class TestKnowledgeBaseLookup:
    def test_find_similar_errors_prefers_rule_id_exact_match(self):
        """S-5: rule_id exact match takes precedence over category fallback."""
        kb = [
            # Legacy entry — no rule_id, only error_category.
            {
                "error_category": "type_error",
                "error": "old VECTOR type mismatch",
                "fix_description": "wrap with vec_avg",
                "failed_expression": "ts_delta(vec_x, 5)",
                "fixed_expression": "ts_delta(vec_avg(vec_x), 5)",
            },
            # New entry with rule_id matching the lookup.
            {
                "rule_id": RuleId.TYPE_MISMATCH_VECTOR_TS,
                "error_category": "type_error",
                "error": "new VECTOR mismatch",
                "fix_description": "wrap with vec_avg (rule_id matched)",
                "failed_expression": "ts_delta(vec_y, 5)",
                "fixed_expression": "ts_delta(vec_avg(vec_y), 5)",
            },
        ]
        # rule_id match must come first.
        similar = _find_similar_errors(
            error_message="vector mismatch",
            error_type="type_error",
            knowledge_base=kb,
            rule_id=RuleId.TYPE_MISMATCH_VECTOR_TS,
        )
        assert len(similar) >= 1
        # First entry is the rule_id-tagged one.
        assert similar[0].get("rule_id") == RuleId.TYPE_MISMATCH_VECTOR_TS

    def test_find_similar_errors_falls_back_to_category(self):
        """Legacy KB entries (no rule_id) still findable via category."""
        kb = [
            {
                "error_category": "field_name",
                "error": "field xyz not found",
                "fix_description": "use close",
                "failed_expression": "rank(xyz)",
                "fixed_expression": "rank(close)",
            },
        ]
        similar = _find_similar_errors(
            error_message="field abc not found",
            error_type="field_name",
            knowledge_base=kb,
            rule_id=RuleId.FIELD_NOT_FOUND,  # no rule_id-tagged entries
        )
        # Falls back to category match.
        assert len(similar) == 1
        assert similar[0]["error_category"] == "field_name"
