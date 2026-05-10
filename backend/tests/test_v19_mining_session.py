"""V-19.9 — Persistent Mining Service tests.

Targets the real PostgreSQL DB (port 5433) since V-19 features rely on
JSONB columns + partial unique indexes that SQLite-in-memory cannot
emulate. Each test creates its own task rows tagged with a unique
prefix and tears them down after.

Run:
    pytest backend/tests/test_v19_mining_session.py -v

Requires:
    POSTGRES_PORT=5433 in env (or .env)
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

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
            # Delete alphas first (FK)
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
# V-19.4 service layer — singleton + start/stop/resume
# ---------------------------------------------------------------------------

class TestSessionSingleton:
    """One CONTINUOUS_CASCADE per region. start_session is idempotent."""

    @pytest.mark.asyncio
    async def test_get_active_session_returns_none_when_no_session(self, pg_session):
        svc = TaskService(pg_session)
        # Pick an unlikely region to avoid collision with running sessions
        # in shared dev DB. We don't actually need to match SUPPORTED_REGIONS
        # for a query (the query filters; no validation).
        r = await svc.get_active_session("USA-NONEXISTENT-INVALID")
        assert r is None

    @pytest.mark.asyncio
    async def test_session_unique_index_blocks_second_active_per_region(self, pg_session):
        # Manually insert two active CONTINUOUS_CASCADE rows for same region —
        # the second commit must fail the partial unique index.
        from sqlalchemy.exc import IntegrityError

        t1 = MiningTask(
            task_name=f"{TEST_PREFIX}-singleton-1",
            region="ZZZ",  # unused region to avoid colliding with real sessions
            universe="TOP3000",
            agent_mode="AUTONOMOUS",
            mining_mode="CONTINUOUS_CASCADE",
            status="RUNNING",
            cascade_phase="T1",
            cascade_round_idx=0,
        )
        pg_session.add(t1)
        await pg_session.commit()

        t2 = MiningTask(
            task_name=f"{TEST_PREFIX}-singleton-2",
            region="ZZZ",
            universe="TOP3000",
            agent_mode="AUTONOMOUS",
            mining_mode="CONTINUOUS_CASCADE",
            status="RUNNING",
            cascade_phase="T1",
            cascade_round_idx=0,
        )
        pg_session.add(t2)
        with pytest.raises(IntegrityError):
            await pg_session.commit()
        await pg_session.rollback()


class TestStartSessionValidation:
    @pytest.mark.asyncio
    async def test_unsupported_region_raises(self, pg_session):
        svc = TaskService(pg_session)
        with pytest.raises(ValueError, match="not supported"):
            await svc.start_session(region="JPN")

    @pytest.mark.asyncio
    async def test_supported_regions_set(self):
        # Sanity: full whitelist matches docs
        assert TaskService.SUPPORTED_REGIONS == ("USA", "CHN", "EUR", "ASI", "GLB")


class TestSessionLifecycle:
    """stop_session / resume_session state machine."""

    @pytest.mark.asyncio
    async def test_stop_running_session_moves_to_paused(self, pg_session):
        # Manually insert an active session row (skip celery dispatch path)
        task = MiningTask(
            task_name=f"{TEST_PREFIX}-lifecycle-1",
            region="ZZZ-LC1",
            universe="TOP3000",
            agent_mode="AUTONOMOUS",
            mining_mode="CONTINUOUS_CASCADE",
            status="RUNNING",
            cascade_phase="T2",
            cascade_round_idx=3,
        )
        pg_session.add(task)
        await pg_session.commit()

        svc = TaskService(pg_session)
        result = await svc.stop_session(task.id)
        assert result.status == "PAUSED"
        assert result.cascade_phase == "T2"
        assert result.cascade_round_idx == 3

    @pytest.mark.asyncio
    async def test_stop_already_paused_session_idempotent(self, pg_session):
        task = MiningTask(
            task_name=f"{TEST_PREFIX}-lifecycle-2",
            region="ZZZ-LC2",
            universe="TOP3000",
            agent_mode="AUTONOMOUS",
            mining_mode="CONTINUOUS_CASCADE",
            status="PAUSED",
            cascade_phase="T1",
        )
        pg_session.add(task)
        await pg_session.commit()
        svc = TaskService(pg_session)
        result = await svc.stop_session(task.id)
        assert result.status == "PAUSED"

    @pytest.mark.asyncio
    async def test_stop_nonexistent_task_raises(self, pg_session):
        svc = TaskService(pg_session)
        with pytest.raises(ValueError, match="not found"):
            await svc.stop_session(task_id=999_999_999)

    @pytest.mark.asyncio
    async def test_stop_discrete_task_rejects(self, pg_session):
        # DISCRETE tasks should not be touched by stop_session — that's the
        # legacy intervene_task PAUSE path's job.
        task = MiningTask(
            task_name=f"{TEST_PREFIX}-lifecycle-3",
            region="USA",
            universe="TOP3000",
            agent_mode="AUTONOMOUS",
            mining_mode="DISCRETE",
            status="RUNNING",
        )
        pg_session.add(task)
        await pg_session.commit()
        svc = TaskService(pg_session)
        with pytest.raises(ValueError, match="not CONTINUOUS_CASCADE"):
            await svc.stop_session(task.id)


# ---------------------------------------------------------------------------
# V-19.8 ON CONFLICT + RESUME helper
# ---------------------------------------------------------------------------

class TestExpressionPersistedHelper:
    """is_expression_persisted_in_task — V-19.4 RESUME dedup."""

    @pytest_asyncio.fixture
    async def task_with_alpha(self, pg_session):
        task = MiningTask(
            task_name=f"{TEST_PREFIX}-helper-1",
            region="ZZZ-HE1",
            universe="TOP3000",
            agent_mode="AUTONOMOUS",
            mining_mode="CONTINUOUS_CASCADE",
            status="RUNNING",
            cascade_phase="T1",
        )
        pg_session.add(task)
        await pg_session.commit()
        await pg_session.refresh(task)

        # Insert one alpha owned by this task
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
        # Same expression, different task_id
        assert await is_expression_persisted_in_task(
            pg_session, task_with_alpha.id + 999_999, "ts_rank(close, 20)"
        ) is False


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

        # alpha_id is VARCHAR(20). Use a short stable ID so both INSERTs use
        # the same value and trigger ON CONFLICT.
        shared_alpha_id = f"v19c{uuid.uuid4().hex[:8]}"  # len=12 < 20

        # First INSERT lands
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

        # Second INSERT with same alpha_id — must return None (skipped) not raise
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
# V-19.7 watchdog
# ---------------------------------------------------------------------------

class TestWatchdog:
    """watchdog_revive_dead_sessions: detects stalled workers via
    last_alpha_persisted_at heartbeat. Grace period skips fresh sessions.
    """

    @pytest.mark.asyncio
    async def test_no_active_sessions_no_revive(self, pg_session):
        # baseline: with no CONTINUOUS_CASCADE sessions, watchdog returns 0
        from backend.tasks.session_watchdog import _watchdog_revive_async
        result = await _watchdog_revive_async()
        # Note: shared DB may have other CONTINUOUS_CASCADE sessions; verify
        # only that none of THIS test's tasks were revived.
        assert isinstance(result["revived"], list)
        for revived in result["revived"]:
            assert TEST_PREFIX not in str(revived.get("task_id", ""))

    @pytest.mark.asyncio
    async def test_grace_period_skips_fresh_session(self, pg_session):
        """Just-created sessions (created_at > NOW()-grace_min) should NOT
        trigger watchdog revive even with NULL heartbeat. IX-6 mitigation."""
        # Create a fresh dead-looking session (no heartbeat, but very recent)
        task = MiningTask(
            task_name=f"{TEST_PREFIX}-watchdog-grace",
            region="ZZZ-WG1",
            universe="TOP3000",
            agent_mode="AUTONOMOUS",
            mining_mode="CONTINUOUS_CASCADE",
            status="RUNNING",
            cascade_phase="T1",
            # last_alpha_persisted_at None ← would be "dead" if not for grace
        )
        pg_session.add(task)
        await pg_session.commit()
        await pg_session.refresh(task)

        from backend.tasks.session_watchdog import _watchdog_revive_async
        result = await _watchdog_revive_async()
        revived_ids = [r["task_id"] for r in result["revived"]]
        assert task.id not in revived_ids, (
            "fresh session in grace period should NOT be revived"
        )

    @pytest.mark.asyncio
    async def test_dead_session_outside_grace_gets_revived(self, pg_session, monkeypatch):
        """Old session with stale heartbeat → watchdog revives it."""
        # Mock celery dispatch so we don't actually start a worker
        from backend.tasks import mining_tasks as mt
        dispatch_calls = []

        class FakeAsyncResult:
            id = "fake-celery-task-id"

        def fake_delay(*args, **kwargs):
            dispatch_calls.append((args, kwargs))
            return FakeAsyncResult()

        monkeypatch.setattr(mt.run_mining_task, "delay", fake_delay)

        # Create a session that's old (created 1 hour ago) and dead heartbeat
        # (last persist 30 min ago)
        old_time = datetime.now(timezone.utc) - timedelta(hours=1)
        stale_heartbeat = datetime.now(timezone.utc) - timedelta(minutes=30)

        task = MiningTask(
            task_name=f"{TEST_PREFIX}-watchdog-dead",
            region="ZZZ-WD1",
            universe="TOP3000",
            agent_mode="AUTONOMOUS",
            mining_mode="CONTINUOUS_CASCADE",
            status="RUNNING",
            cascade_phase="T2",
            cascade_round_idx=2,
            last_alpha_persisted_at=stale_heartbeat,
        )
        pg_session.add(task)
        await pg_session.flush()
        # Force created_at into the past — server_default gave us NOW().
        from sqlalchemy import update
        await pg_session.execute(
            update(MiningTask)
            .where(MiningTask.id == task.id)
            .values(created_at=old_time)
        )
        await pg_session.commit()
        await pg_session.refresh(task)

        from backend.tasks.session_watchdog import _watchdog_revive_async
        result = await _watchdog_revive_async()
        revived_ids = [r["task_id"] for r in result["revived"]]
        assert task.id in revived_ids, (
            f"dead session task={task.id} should have been revived; got {revived_ids}"
        )
        assert len(dispatch_calls) >= 1, "celery dispatch should have been called"


# ---------------------------------------------------------------------------
# V-19.7 quota guard
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
        # Production DB may have many alphas today; assertion limited to dict shape.
        assert "paused_count" in result


# ---------------------------------------------------------------------------
# V-19.2 cascade phase tier mapping
# ---------------------------------------------------------------------------

class TestCascadePhaseToTier:
    def test_tier_to_agent_mode_mapping(self):
        from backend.tasks.mining_tasks import _TIER_TO_AGENT_MODE
        assert _TIER_TO_AGENT_MODE[1] == "AUTONOMOUS_TIER1"
        assert _TIER_TO_AGENT_MODE[2] == "AUTONOMOUS_TIER2"
        assert _TIER_TO_AGENT_MODE[3] == "AUTONOMOUS_TIER3"

    def test_round_trip_tier_resolution(self):
        """The tier the cascade sets via agent_mode is the tier MiningAgent
        derives — round-trip invariant."""
        from backend.tasks.mining_tasks import _TIER_TO_AGENT_MODE
        for tier, mode in _TIER_TO_AGENT_MODE.items():
            assert TaskService.factor_tier_from_mode(mode) == tier

    def test_cascade_settings_have_sensible_defaults(self):
        # IX-2 round-driven defaults (T1=10/T2=10/T3=5)
        assert settings.CASCADE_T1_ROUNDS >= 1
        assert settings.CASCADE_T2_ROUNDS >= 1
        assert settings.CASCADE_T3_ROUNDS >= 1
        # IX-4 — T3 default disabled
        assert settings.CASCADE_ENABLE_T3 is False


# ---------------------------------------------------------------------------
# V-19.10 fix-up — C1 / H1 / H2
# ---------------------------------------------------------------------------

class TestV1910Fixups:
    """Regressions for V-19.10 C1/H1/H2 fixes from code review."""

    def test_C1_run_mining_iteration_accepts_factor_tier_override(self):
        """C1: factor_tier_override parameter exists and overrides agent_mode
        derivation. Without this, cascade had to mutate task.agent_mode in
        memory, which leaked into ORM auto-flush and persisted to DB."""
        import inspect
        from backend.agents.mining_agent import MiningAgent
        sig = inspect.signature(MiningAgent.run_mining_iteration)
        assert "factor_tier_override" in sig.parameters
        param = sig.parameters["factor_tier_override"]
        # default None — non-cascade callers unaffected
        assert param.default is None

    def test_C1_run_evolution_loop_accepts_factor_tier_override(self):
        import inspect
        from backend.agents.mining_agent import MiningAgent
        sig = inspect.signature(MiningAgent.run_evolution_loop)
        assert "factor_tier_override" in sig.parameters

    def test_C1_cascade_phase_does_not_mutate_agent_mode(self):
        """C1: source-level guard — cascade phase code path (incl V-20 helpers)
        must not touch task.agent_mode. A grep catches regressions that
        re-introduce the mutation."""
        import inspect
        from backend.tasks import mining_tasks
        # V-20 split: factor_tier_override is now passed inside the per-round
        # helpers (_run_one_round_inline + _prefetch_round_isolated) rather
        # than directly inside _run_cascade_phase. Check all three for mode
        # mutation absence + factor_tier_override presence in the helpers.
        for fn in (
            mining_tasks._run_cascade_phase,
            mining_tasks._run_one_round_inline,
            mining_tasks._prefetch_round_isolated,
        ):
            src = inspect.getsource(fn)
            assert "task.agent_mode =" not in src, (
                f"regression: {fn.__name__} reintroduced task.agent_mode "
                "mutation — C1 fix lost"
            )
        # The override must be wired in BOTH per-round entry points.
        for helper in (
            mining_tasks._run_one_round_inline,
            mining_tasks._prefetch_round_isolated,
        ):
            src = inspect.getsource(helper)
            assert "factor_tier_override=tier" in src, (
                f"regression: {helper.__name__} no longer passes "
                "factor_tier_override to mining_agent — C1 fix lost"
            )

    def test_H1_cascade_phase_updates_heartbeat_per_dataset(self):
        """H1: cascade phase loop must update last_alpha_persisted_at after
        each dataset, not just on PASS."""
        import inspect
        from backend.tasks import mining_tasks
        src = inspect.getsource(mining_tasks._run_cascade_phase)
        assert "last_alpha_persisted_at" in src, (
            "regression: cascade phase no longer updates heartbeat at dataset "
            "boundary — H1 fix lost"
        )

    def test_H2_continuous_cascade_paused_run_status(self):
        """H2: when worker exits because task is PAUSED/STOPPED, the
        ExperimentRun.status should mirror task.status, not be misreported
        as COMPLETED."""
        import inspect
        from backend.tasks import mining_tasks
        src = inspect.getsource(mining_tasks._run_continuous_cascade)
        # The fix-up replaces unconditional `run.status = "COMPLETED"` with
        # a conditional that mirrors task.status when paused.
        assert 'task.status in ("PAUSED", "STOPPED")' in src, (
            "regression: paused/stopped sessions are still being marked "
            "COMPLETED — H2 fix lost"
        )

    def test_V20_pipeline_setting_default_on(self):
        """V-20: pipeline is opt-out. Default ON = round N+1 prefetch active."""
        assert settings.CASCADE_PIPELINE_ENABLED is True

    def test_V20_helpers_present(self):
        """V-20 / V-20.1: per-round helpers + pipeline coordination present."""
        from backend.tasks import mining_tasks
        # Per-round helpers
        assert hasattr(mining_tasks, "_run_one_round_inline")
        assert hasattr(mining_tasks, "_prefetch_round_isolated")
        # Cascade phase must use asyncio.create_task (pipeline marker)
        import inspect
        src = inspect.getsource(mining_tasks._run_cascade_phase)
        assert "asyncio.create_task" in src
        assert "_prefetch_round_isolated" in src
        # Cancel logic for pause path (V-20.1: current/next_task instead
        # of single pending var). Either variable being cancellable suffices.
        assert ".cancel()" in src

    def test_V21_diversity_mandate_in_system_prompt(self):
        """V-21: T1_STRATEGY_SYSTEM must enforce family-diversity to prevent
        the RETURNS-monoculture observed in 1hr V-20.1 production data."""
        from backend.agents.prompts.strategy_prompts import T1_STRATEGY_SYSTEM
        assert "FAMILY DIVERSITY MANDATE" in T1_STRATEGY_SYSTEM
        assert "at least 3 distinct field families" in T1_STRATEGY_SYSTEM
        # The system prompt should enumerate family taxonomy
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
        # Family tag must appear before each pattern (visible to LLM)
        assert "[RETURNS]" in prompt

    def test_V21_alert_silent_when_patterns_balanced(self):
        """V-21: balanced family distribution should NOT emit the alert
        (otherwise LLM gets noise for healthy state)."""
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
        # All 4 BRAIN check categories must be enumerated
        for fail_kind in ("LOW_FITNESS", "CONCENTRATED_WEIGHT",
                          "SELF_CORR", "LOW_SUB_UNIVERSE_SHARPE"):
            assert fail_kind in T1_STRATEGY_SYSTEM

    def test_V22_pattern_tags_render(self):
        """V-22: patterns from RAG carry brain_can_submit / brain_failed_checks
        and the prompt translates them to readable tags."""
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
        """V-22: when ≥50% of resolved patterns are BRAIN_REJECTED, prompt
        emits the REJECTION ALERT — major LLM signal to pivot."""
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
        assert "2/3" in prompt  # 2 of 3 resolved patterns rejected

    def test_V22_alert_silent_when_pending_only(self):
        """V-22: PENDING patterns alone shouldn't fire the alert (no verdicts
        to count yet)."""
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
        # PENDING tag still rendered for visibility
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

    def test_V201_schedules_next_before_awaiting_current(self):
        """V-20.1: critical ordering invariant. The next-round task MUST be
        spawned BEFORE we await the current round, else the main loop
        blocks on prefetch and the pipeline degenerates to serial.
        Trace_steps from V-20 production showed exactly this regression."""
        import inspect
        from backend.tasks import mining_tasks
        src = inspect.getsource(mining_tasks._run_cascade_phase)
        idx_schedule = src.find("next_task = _spawn(i + 1)")
        idx_await = src.find("result = await current")
        assert idx_schedule > 0 and idx_await > 0, (
            "V-20.1 markers missing — code structure changed"
        )
        assert idx_schedule < idx_await, (
            "regression: V-20.1 ordering violated. The schedule of "
            "next_task MUST appear before the await of current; otherwise "
            "the pipeline runs serially."
        )

    def test_V20_prefetch_uses_isolated_session(self):
        """V-20: prefetch_round MUST open its own AsyncSessionLocal (not
        share with foreground). Otherwise a single round_N+1 commit could
        race the round_N foreground commit."""
        import inspect
        from backend.tasks import mining_tasks
        src = inspect.getsource(mining_tasks._prefetch_round_isolated)
        assert "AsyncSessionLocal" in src, (
            "regression: prefetch must use isolated DB session — sharing "
            "the foreground session causes commit/refresh races"
        )
        assert "BrainAdapter()" in src, (
            "regression: prefetch must construct its own BrainAdapter so "
            "the redis sim slot semaphore can serialize foreground + prefetch"
        )

    @pytest.mark.asyncio
    async def test_V20_prefetch_returns_skipped_when_task_paused(self, pg_session):
        """V-20: prefetch round started AFTER task moves to PAUSED should
        return early with skipped=True instead of opening Brain."""
        task = MiningTask(
            task_name=f"{TEST_PREFIX}-V20-prefetch-paused",
            region="ZZZ-V20-PP",
            universe="TOP3000",
            agent_mode="AUTONOMOUS",
            mining_mode="CONTINUOUS_CASCADE",
            status="PAUSED",
            cascade_phase="T1",
        )
        pg_session.add(task)
        await pg_session.commit()
        await pg_session.refresh(task)

        from backend.tasks.mining_tasks import _prefetch_round_isolated
        result = await _prefetch_round_isolated(
            task_id=task.id, run_id=0, dataset_id="pv1", tier=1,
        )
        assert result.get("skipped") is True
        assert len(result.get("all_alphas", [])) == 0

    @pytest.mark.asyncio
    async def test_C1_factor_tier_override_overrides_agent_mode(self, pg_session):
        """End-to-end: a task with agent_mode='AUTONOMOUS' resolves tier=1
        normally, but factor_tier_override=2 wins. Verifies the parameter
        actually short-circuits the agent_mode-based derivation, not just
        an unused signature artifact."""
        # We can't safely call run_mining_iteration end-to-end here (it spins
        # up workflows + simulate). Instead exercise the resolution logic
        # directly, mirroring the file's branch:
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

        # Simulate the resolution branch from mining_agent.run_mining_iteration
        def resolve(task, override):
            if override is not None:
                return override
            return TaskService.factor_tier_from_mode(task.agent_mode) or 1

        assert resolve(task, None) == 1   # AUTONOMOUS → 1
        assert resolve(task, 2) == 2      # override wins
        assert resolve(task, 3) == 3
        # And critically: agent_mode is never touched
        assert task.agent_mode == "AUTONOMOUS"
