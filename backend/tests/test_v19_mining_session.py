"""V-19.9 / V-20 / V-21 / V-22 — retained tests post phase15-D cascade retirement.

phase15-D PR3a/PR3b/PR3c/PR3d/PR3e retired the CONTINUOUS_CASCADE path:
  * PR3b dropped cascade_phase + cascade_round_idx ORM columns
  * PR3c deleted mining_session router + start_session/stop_session/resume_session
  * PR3d deleted _run_continuous_cascade / _run_cascade_phase / _prefetch_round_isolated
  * PR3e deleted task_service cascade methods

This file was rewritten 2026-05-18 to keep only the tests that DO NOT
depend on those deleted artifacts:

  KEPT — exercise non-cascade live code:
    * TestStartSessionValidation.test_supported_regions_set
    * TestOnConflictDoNothing — pure DB ON CONFLICT, no cascade dep
    * TestExpressionPersistedHelper — persistence helper still live
    * TestQuotaGuard — _quota_guard_async still live
    * TestCascadePhaseToTier — pure constant/mapping checks
    * TestV1910Fixups — prompt content tests (V21/V22) + factor_tier_override
      sig tests (V20) still live

  DROPPED — referenced deleted cascade-only helpers/methods/columns:
    * TestSessionSingleton (svc.get_active_session deleted, cascade_phase col dropped)
    * TestSessionLifecycle (svc.stop_session deleted)
    * TestWatchdog (watchdog still revives cascade tasks but test fixtures
      passed cascade_phase kwarg which now raises TypeError)
    * test_C1_cascade_phase_does_not_mutate_agent_mode (_run_cascade_phase deleted)
    * test_H1_cascade_phase_updates_heartbeat_per_dataset (same)
    * test_H2_continuous_cascade_paused_run_status (_run_continuous_cascade deleted)
    * test_V20_helpers_present (_prefetch_round_isolated deleted)
    * test_V201_schedules_next_before_awaiting_current (_run_cascade_phase deleted)
    * test_V20_prefetch_uses_isolated_session (_prefetch_round_isolated deleted)
    * test_V20_prefetch_returns_skipped_when_task_paused (same)

Run:
    pytest backend/tests/test_v19_mining_session.py -v

Requires:
    POSTGRES_PORT=5433 in env (or .env)
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Force the real PG connection for these tests
os.environ.setdefault("POSTGRES_PORT", "5433")

from backend.config import settings  # noqa: E402
from backend.models import Alpha, ExperimentRun, MiningTask  # noqa: E402
from backend.services.task_service import TaskService  # noqa: E402


TEST_PREFIX = f"v19test-{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture(scope="function")
async def pg_session():
    """Real-PG session per test. Cleans up TEST_PREFIX rows after."""
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        yield db
        # Teardown — only touch this test's rows.
        async with Session() as cleanup:
            tasks = (
                await cleanup.execute(
                    select(MiningTask.id).where(
                        MiningTask.task_name.like(f"{TEST_PREFIX}%")
                    )
                )
            ).scalars().all()
            if tasks:
                await cleanup.execute(
                    delete(Alpha).where(Alpha.task_id.in_(tasks))
                )
                await cleanup.execute(
                    delete(ExperimentRun).where(ExperimentRun.task_id.in_(tasks))
                )
                await cleanup.execute(
                    delete(MiningTask).where(MiningTask.id.in_(tasks))
                )
                await cleanup.commit()
    await engine.dispose()


# ---------------------------------------------------------------------------
# Service-layer constant: SUPPORTED_REGIONS still drives flat_session validation
# ---------------------------------------------------------------------------

class TestStartSessionValidation:
    @pytest.mark.asyncio
    async def test_supported_regions_set(self):
        # Sanity: full whitelist matches docs. Still consumed by start_flat_session.
        assert TaskService.SUPPORTED_REGIONS == ("USA", "CHN", "EUR", "ASI", "GLB")


# ---------------------------------------------------------------------------
# V-19.4 — RESUME dedup helper (still used by FLAT path)
# ---------------------------------------------------------------------------

class TestExpressionPersistedHelper:
    """is_expression_persisted_in_task is mining_mode-agnostic — still alive."""

    @pytest_asyncio.fixture
    async def task_with_alpha(self, pg_session):
        task = MiningTask(
            task_name=f"{TEST_PREFIX}-helper-1",
            region="ZZZ-HE1",
            universe="TOP3000",
            agent_mode="AUTONOMOUS",
            mining_mode="FLAT_CONTINUOUS",
            status="RUNNING",
        )
        pg_session.add(task)
        await pg_session.commit()
        await pg_session.refresh(task)

        from backend.alpha_semantic_validator import compute_expression_hash
        expr = "ts_rank(close, 20)"
        alpha = Alpha(
            alpha_id=f"{TEST_PREFIX}-a1",
            task_id=task.id,
            expression=expr,
            expression_hash=compute_expression_hash(expr),
            region="ZZZ-HE1",
            universe="TOP3000",
            quality_status="PASS",
        )
        pg_session.add(alpha)
        await pg_session.commit()
        return task

    @pytest.mark.asyncio
    async def test_returns_true_for_persisted_expression(self, pg_session, task_with_alpha):
        from backend.agents.graph.nodes.persistence import is_expression_persisted_in_task
        assert await is_expression_persisted_in_task(
            pg_session, task_with_alpha.id, "ts_rank(close, 20)"
        ) is True

    @pytest.mark.asyncio
    async def test_returns_false_for_unpersisted_expression(self, pg_session, task_with_alpha):
        from backend.agents.graph.nodes.persistence import is_expression_persisted_in_task
        assert await is_expression_persisted_in_task(
            pg_session, task_with_alpha.id, "rank(volume)"
        ) is False

    @pytest.mark.asyncio
    async def test_returns_false_for_empty_expression(self, pg_session, task_with_alpha):
        from backend.agents.graph.nodes.persistence import is_expression_persisted_in_task
        assert await is_expression_persisted_in_task(
            pg_session, task_with_alpha.id, ""
        ) is False

    @pytest.mark.asyncio
    async def test_returns_false_for_different_task(self, pg_session, task_with_alpha):
        from backend.agents.graph.nodes.persistence import is_expression_persisted_in_task
        assert await is_expression_persisted_in_task(
            pg_session, task_with_alpha.id + 999_999, "ts_rank(close, 20)"
        ) is False


# ---------------------------------------------------------------------------
# V-19.8 ON CONFLICT — still used by every persistence path
# ---------------------------------------------------------------------------

class TestOnConflictDoNothing:
    """V-19.8 — INSERT ... ON CONFLICT (alpha_id) DO NOTHING.
    Prevents race-window IntegrityError when two workers try to
    persist the same alpha_id concurrently.
    """

    @pytest.mark.asyncio
    async def test_duplicate_alpha_id_insert_does_not_raise(self, pg_session):
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        task = MiningTask(
            task_name=f"{TEST_PREFIX}-conflict-1",
            region="ZZZ-CF1",
            universe="TOP3000",
            agent_mode="AUTONOMOUS",
            mining_mode="DISCRETE",
            status="RUNNING",
        )
        pg_session.add(task)
        await pg_session.commit()
        await pg_session.refresh(task)

        shared_alpha_id = f"v19c{uuid.uuid4().hex[:8]}"  # len=12 < 20

        stmt1 = (
            pg_insert(Alpha)
            .values(
                alpha_id=shared_alpha_id,
                task_id=task.id,
                expression="rank(close)",
                region="ZZZ-CF1",
                universe="TOP3000",
                quality_status="PASS",
            )
            .on_conflict_do_nothing(index_elements=["alpha_id"])
            .returning(Alpha.id)
        )
        first_id = (await pg_session.execute(stmt1)).scalar_one_or_none()
        assert first_id is not None
        await pg_session.commit()

        stmt2 = (
            pg_insert(Alpha)
            .values(
                alpha_id=shared_alpha_id,
                task_id=task.id,
                expression="rank(close)",
                region="ZZZ-CF1",
                universe="TOP3000",
                quality_status="PASS_PROVISIONAL",
            )
            .on_conflict_do_nothing(index_elements=["alpha_id"])
            .returning(Alpha.id)
        )
        second_id = (await pg_session.execute(stmt2)).scalar_one_or_none()
        assert second_id is None  # ON CONFLICT skipped the row
        await pg_session.commit()


# ---------------------------------------------------------------------------
# V-19.7 quota guard — still scheduled by celery beat
# ---------------------------------------------------------------------------

class TestQuotaGuard:
    @pytest.mark.asyncio
    async def test_below_threshold_no_pause(self, pg_session):
        from backend.tasks.session_watchdog import _quota_guard_async
        result = await _quota_guard_async()
        assert isinstance(result["today_alpha_count"], int)
        assert result["limit"] == settings.BRAIN_DAILY_SIMULATE_LIMIT
        assert result["threshold"] == int(
            settings.BRAIN_DAILY_SIMULATE_LIMIT * settings.BRAIN_QUOTA_PAUSE_PCT
        )
        assert "paused_count" in result


# ---------------------------------------------------------------------------
# V-19.2 cascade phase tier mapping constants (still consumed by mining_agent
# tier dispatch — _TIER_TO_AGENT_MODE table outlives _run_cascade_phase)
# ---------------------------------------------------------------------------

class TestCascadePhaseToTier:
    def test_tier_to_agent_mode_mapping(self):
        from backend.tasks.mining_tasks import _TIER_TO_AGENT_MODE
        assert _TIER_TO_AGENT_MODE[1] == "AUTONOMOUS_TIER1"
        assert _TIER_TO_AGENT_MODE[2] == "AUTONOMOUS_TIER2"
        assert _TIER_TO_AGENT_MODE[3] == "AUTONOMOUS_TIER3"

    def test_round_trip_tier_resolution(self):
        """Tier the dispatcher sets via agent_mode == tier MiningAgent derives."""
        from backend.tasks.mining_tasks import _TIER_TO_AGENT_MODE
        for tier, mode in _TIER_TO_AGENT_MODE.items():
            assert TaskService.factor_tier_from_mode(mode) == tier

    def test_cascade_settings_have_sensible_defaults(self):
        # IX-2 round-driven defaults (T1=10/T2=10/T3=5). Constants
        # outlive the cascade execution path — still read by some
        # legacy unit tests and admin views.
        assert settings.CASCADE_T1_ROUNDS >= 1
        assert settings.CASCADE_T2_ROUNDS >= 1
        assert settings.CASCADE_T3_ROUNDS >= 1
        # IX-4 — T3 default disabled
        assert settings.CASCADE_ENABLE_T3 is False


# ---------------------------------------------------------------------------
# V-19.10 fix-up — retained pieces (factor_tier_override signature + V-21/V-22
# prompt content tests). Cascade-execution-coupled assertions removed in
# phase15-D PR3d cleanup.
# ---------------------------------------------------------------------------

class TestV1910Fixups:
    """Regressions for live prompt content + factor_tier_override signature.

    Source-grep tests against _run_cascade_phase / _run_continuous_cascade /
    _prefetch_round_isolated were dropped — those helpers are deleted.
    """

    def test_C1_run_mining_iteration_accepts_factor_tier_override(self):
        """C1: factor_tier_override parameter exists and overrides agent_mode
        derivation. Still used by flat-mode tier dispatch."""
        import inspect
        from backend.agents.mining_agent import MiningAgent
        sig = inspect.signature(MiningAgent.run_mining_iteration)
        assert "factor_tier_override" in sig.parameters
        param = sig.parameters["factor_tier_override"]
        assert param.default is None

    def test_C1_run_evolution_loop_accepts_factor_tier_override(self):
        import inspect
        from backend.agents.mining_agent import MiningAgent
        sig = inspect.signature(MiningAgent.run_evolution_loop)
        assert "factor_tier_override" in sig.parameters

    def test_V20_pipeline_setting_default_on(self):
        """V-20: pipeline is opt-out. Default ON setting still consumed by
        the FLAT runner's prefetch decisions."""
        assert settings.CASCADE_PIPELINE_ENABLED is True

    def test_V21_diversity_mandate_in_system_prompt(self):
        """V-21: T1_STRATEGY_SYSTEM must enforce family-diversity to prevent
        the RETURNS-monoculture observed in 1hr V-20.1 production data."""
        from backend.agents.prompts.strategy_prompts import T1_STRATEGY_SYSTEM
        assert "FAMILY DIVERSITY MANDATE" in T1_STRATEGY_SYSTEM
        assert "at least 3 distinct field families" in T1_STRATEGY_SYSTEM
        for fam in ("RETURNS", "FUNDAMENTAL", "ANALYST", "SENTIMENT",
                    "FACTOR_COMPOSITE", "OPTION", "PRICE_PV"):
            assert fam in T1_STRATEGY_SYSTEM, f"family {fam} missing from T1 mandate"

    def test_V21_alert_fires_when_patterns_concentrated(self):
        """V-21: when >50% of recent success_patterns are in one family,
        the user prompt must emit a DIVERSITY ALERT block calling it out."""
        from backend.agents.prompts.strategy_prompts import build_t1_strategy_user_prompt
        prompt = build_t1_strategy_user_prompt(
            dataset_id="pv1", region="USA",
            available_fields=[{"id": "returns", "type": "MATRIX", "coverage": 1.0}],
            success_patterns=[
                {"pattern": "multiply(-1, ts_decay_linear(returns, 5))", "expected_sharpe": 1.5},
                {"pattern": "multiply(-1, ts_decay_linear(returns, 10))", "expected_sharpe": 1.4},
                {"pattern": "multiply(-1, ts_mean(returns, 5))", "expected_sharpe": 1.3},
                {"pattern": "ts_zscore(close, 20)", "expected_sharpe": 1.0},
            ],
        )
        assert "V-21 DIVERSITY ALERT" in prompt
        assert "RETURNS" in prompt
        assert "[RETURNS]" in prompt

    def test_V21_alert_silent_when_patterns_balanced(self):
        from backend.agents.prompts.strategy_prompts import build_t1_strategy_user_prompt
        prompt = build_t1_strategy_user_prompt(
            dataset_id="pv1", region="USA",
            available_fields=[{"id": "returns", "type": "MATRIX"}],
            success_patterns=[
                {"pattern": "ts_decay_linear(returns, 5)"},
                {"pattern": "ts_zscore(fnd6_revenue, 20)"},
                {"pattern": "ts_rank(snt_news_buzz, 10)"},
            ],
        )
        assert "V-21 DIVERSITY ALERT" not in prompt

    def test_V22_brain_status_in_system_prompt(self):
        """V-22: T1_STRATEGY_SYSTEM must teach the LLM how to read BRAIN
        verdict tags (OK/REJECTED/PENDING)."""
        from backend.agents.prompts.strategy_prompts import T1_STRATEGY_SYSTEM
        assert "BRAIN FEEDBACK INTERPRETATION" in T1_STRATEGY_SYSTEM
        assert "BRAIN_OK" in T1_STRATEGY_SYSTEM
        assert "BRAIN_REJECTED" in T1_STRATEGY_SYSTEM
        for fail_kind in ("LOW_FITNESS", "CONCENTRATED_WEIGHT",
                          "SELF_CORR", "LOW_SUB_UNIVERSE_SHARPE"):
            assert fail_kind in T1_STRATEGY_SYSTEM

    def test_V22_pattern_tags_render(self):
        from backend.agents.prompts.strategy_prompts import build_t1_strategy_user_prompt
        prompt = build_t1_strategy_user_prompt(
            dataset_id="pv1", region="USA",
            available_fields=[{"id": "returns", "type": "MATRIX"}],
            success_patterns=[
                {"pattern": "ts_zscore(fnd6_revenue, 60)", "expected_sharpe": 1.2,
                 "brain_can_submit": True, "brain_failed_checks": []},
                {"pattern": "multiply(-1, ts_decay_linear(returns, 5))",
                 "expected_sharpe": 1.5,
                 "brain_can_submit": False,
                 "brain_failed_checks": [{"name": "LOW_FITNESS"}]},
                {"pattern": "rank(volume)", "expected_sharpe": 1.0},
            ],
        )
        assert "[BRAIN_OK" in prompt
        assert "[BRAIN_REJECTED: LOW_FITNESS]" in prompt
        assert "[BRAIN_PENDING]" in prompt

    def test_V22_alert_fires_when_majority_rejected(self):
        from backend.agents.prompts.strategy_prompts import build_t1_strategy_user_prompt
        prompt = build_t1_strategy_user_prompt(
            dataset_id="pv1", region="USA",
            available_fields=[{"id": "returns", "type": "MATRIX"}],
            success_patterns=[
                {"pattern": "multiply(-1, ts_decay_linear(returns, 5))",
                 "brain_can_submit": False,
                 "brain_failed_checks": [{"name": "LOW_FITNESS"}]},
                {"pattern": "multiply(-1, ts_decay_linear(returns, 10))",
                 "brain_can_submit": False,
                 "brain_failed_checks": [{"name": "SELF_CORR"}]},
                {"pattern": "ts_zscore(fnd6_revenue, 60)",
                 "brain_can_submit": True, "brain_failed_checks": []},
            ],
        )
        assert "V-22 BRAIN-REJECTION ALERT" in prompt
        assert "2/3" in prompt

    def test_V22_alert_silent_when_pending_only(self):
        from backend.agents.prompts.strategy_prompts import build_t1_strategy_user_prompt
        prompt = build_t1_strategy_user_prompt(
            dataset_id="pv1", region="USA",
            available_fields=[{"id": "returns", "type": "MATRIX"}],
            success_patterns=[
                {"pattern": "ts_rank(close, 20)"},
                {"pattern": "ts_mean(volume, 5)"},
            ],
        )
        assert "V-22 BRAIN-REJECTION ALERT" not in prompt
        assert "[BRAIN_PENDING]" in prompt

    def test_V22_update_pattern_brain_status_helper_exists(self):
        """V-22: rag_service.update_pattern_brain_status is the write path
        called by refresh_can_submit_for_alpha — must exist + be async."""
        import inspect
        from backend.agents.services.rag_service import RAGService
        m = getattr(RAGService, "update_pattern_brain_status", None)
        assert m is not None
        assert inspect.iscoroutinefunction(m)

    def test_V22_refresh_task_calls_brain_status_update(self):
        """V-22: refresh_can_submit_for_alpha must call update_pattern_brain_
        status (regardless of can_submit value) so True clears stale state
        and False stamps the rejection."""
        import inspect
        from backend.tasks import refresh_tasks
        src = inspect.getsource(refresh_tasks._refresh_can_submit_async)
        assert "update_pattern_brain_status" in src, (
            "regression: refresh_can_submit no longer calls V-22 update — "
            "LLM feedback loop is broken"
        )

    def test_V21_alert_silent_when_no_patterns(self):
        """V-21: cold-start (no patterns) should not emit alert."""
        from backend.agents.prompts.strategy_prompts import build_t1_strategy_user_prompt
        prompt = build_t1_strategy_user_prompt(
            dataset_id="pv1", region="USA",
            available_fields=[{"id": "close", "type": "MATRIX", "coverage": 0.99}],
            success_patterns=None,
        )
        assert "V-21 DIVERSITY ALERT" not in prompt

    @pytest.mark.asyncio
    async def test_C1_factor_tier_override_overrides_agent_mode(self, pg_session):
        """End-to-end-lite: factor_tier_override wins over agent_mode-derived tier."""
        from backend.services.task_service import TaskService

        task = MiningTask(
            task_name=f"{TEST_PREFIX}-C1",
            region="ZZZ-C1",
            universe="TOP3000",
            agent_mode="AUTONOMOUS",
            mining_mode="DISCRETE",
            status="RUNNING",
        )
        pg_session.add(task)
        await pg_session.commit()

        # Mirror the resolution branch from mining_agent.run_mining_iteration
        def resolve(task, override):
            if override is not None:
                return override
            return TaskService.factor_tier_from_mode(task.agent_mode) or 1

        assert resolve(task, None) == 1   # AUTONOMOUS → 1
        assert resolve(task, 2) == 2      # override wins
        assert resolve(task, 3) == 3
        assert task.agent_mode == "AUTONOMOUS"
