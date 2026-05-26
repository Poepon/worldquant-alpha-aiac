"""Unit tests for AlphaService.get_marginal_contribution (IQC submission API).

Runs against live PG (Alpha model uses JSONB columns that SQLite can't
represent). Inserts a dedicated test alpha + cleans up.
"""
from __future__ import annotations

import socket
import uuid
import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from backend.models import Alpha, MiningTask
from backend.services import AlphaService
from backend.tests.fixtures.mock_brain import MockBrainAdapter


def _pg_reachable() -> bool:
    try:
        s = socket.create_connection(("localhost", 5433), timeout=1)
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason="Postgres not reachable on localhost:5433",
)


@pytest_asyncio.fixture
async def pg_session():
    """Real-PG session for tests that need JSONB columns."""
    from backend.config import settings as _s
    url = (
        f"postgresql+asyncpg://{_s.POSTGRES_USER}:{_s.POSTGRES_PASSWORD}@"
        f"{_s.POSTGRES_SERVER}:{_s.POSTGRES_PORT}/{_s.POSTGRES_DB}"
    )
    engine = create_async_engine(url, echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def test_alpha(pg_session):
    """Insert a test alpha with BRAIN id; auto-cleanup."""
    brain_id = f"test_{uuid.uuid4().hex[:8]}"
    task = MiningTask(
        task_name=f"marginal-test-{uuid.uuid4().hex[:8]}",
        region="USA",
        universe="TOP3000",
        dataset_strategy="AUTO",
        target_datasets=[],        status="PENDING",
        daily_goal=1,
        config={},
    )
    pg_session.add(task)
    await pg_session.commit()
    await pg_session.refresh(task)

    alpha = Alpha(
        alpha_id=brain_id,
        task_id=task.id,
        expression="ts_rank(close, 20)",
        expression_hash=f"test-{brain_id}",
        region="USA",
        universe="TOP3000",
        status="created",
        quality_status="PASS",
        human_feedback="NONE",
        can_submit=True,
        is_sharpe=3.19,
        is_fitness=2.67,
        is_turnover=0.156,
        is_margin=0.0010,  # 10 bps — clears the 5bps economic gate
    )
    pg_session.add(alpha)
    await pg_session.commit()
    await pg_session.refresh(alpha)

    yield alpha

    # Cleanup
    await pg_session.execute(delete(Alpha).where(Alpha.id == alpha.id))
    await pg_session.execute(delete(MiningTask).where(MiningTask.id == task.id))
    await pg_session.commit()


class TestMarginalContribution:
    """V-22 IQC submission marginal-contribution API."""

    @pytest.mark.asyncio
    async def test_returns_none_for_missing_alpha(self, pg_session):
        svc = AlphaService(pg_session)
        mock = MockBrainAdapter()
        result = await svc.get_marginal_contribution(
            alpha_pk=9_999_999, brain_adapter=mock,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_payload_with_deltas(self, pg_session, test_alpha):
        svc = AlphaService(pg_session)
        mock = MockBrainAdapter()
        # 2026-05-24: IQC2026S1 competition was deleted; the standing scope is
        # team deLkl06. Drive the comprehensive payload assertions through the
        # team scope.
        result = await svc.get_marginal_contribution(
            alpha_pk=test_alpha.id,
            team_id="deLkl06",
            brain_adapter=mock,
        )
        assert result is not None
        assert result["alpha_pk"] == test_alpha.id
        assert result["alpha_brain_id"] == test_alpha.alpha_id
        assert result["scope"] == "teams/deLkl06"

        deltas = result["deltas"]
        # MockBrainAdapter: before sharpe=3.19, after=3.16 → -0.03
        assert deltas["sharpe"] == pytest.approx(-0.03, abs=0.01)
        # before fitness=2.60, after=2.62 → +0.02
        assert deltas["fitness"] == pytest.approx(0.02, abs=0.01)
        # before pnl=5_387_851, after=6_000_000 → +612_149
        assert deltas["pnl"] == pytest.approx(612_149, abs=10)
        # margin now extracted: 0.0016 → 0.0019 → +0.0003
        assert deltas["margin"] == pytest.approx(0.0003, abs=1e-5)
        # 2026-05-26: `score` is competition-scope-ONLY (team/users omit it), so
        # under this team scope it must be absent from both deltas and raw.
        assert "score" not in deltas
        assert "score" not in result["raw"]

        assert "stats" in result["raw"]
        # partitionName is surfaced in the envelope
        assert result["partition_name"] == "EQUITY:USA:1"

        # analysis: multi-dimensional — Δsharpe slightly negative but returns/
        # margin/pnl up + drawdown down → a good diversifier → SUBMIT (the fix
        # vs the old Sharpe-led SKIP). Sharpe still surfaced as a negative.
        analysis = result["analysis"]
        assert analysis["recommendation"] == "SUBMIT"
        assert analysis["label"] == "推荐提交"
        assert analysis["composite_score"] > 0
        assert analysis["signals"]["sharpe"] == -1
        assert "sharpe" in {n["metric"] for n in analysis["negatives"]}
        assert {p["metric"] for p in analysis["positives"]} >= {"returns", "drawdown"}
        # alpha's own margin (10 bps) surfaced + clears the economic gate
        assert analysis["margin_bps"] == pytest.approx(10.0, abs=0.01)
        assert not any("Margin" in g for g in analysis["guardrails"])

    @pytest.mark.asyncio
    async def test_competition_scope_surfaces_score_display_only(
        self, pg_session, test_alpha,
    ):
        """IQC2026S2 (2026-05-26): a competition scope brings back the leaderboard
        `score`. deltas["score"] must be computed + raw.score surfaced, but it is
        DISPLAY-ONLY — it must NOT enter the composite scorecard (not a scored
        dimension), even when Δscore is negative while the stats look good.
        """
        svc = AlphaService(pg_session)
        mock = MockBrainAdapter()
        result = await svc.get_marginal_contribution(
            alpha_pk=test_alpha.id, competition="IQC2026S2", brain_adapter=mock,
        )
        assert result is not None
        assert result["scope"] == "competitions/IQC2026S2"
        # partition label is the global competition shape
        assert result["partition_name"] == "EQUITY:1"

        # score delta computed at the payload top level: 7603 - 7741 = -138
        assert result["raw"]["score"] == {"before": 7741.0, "after": 7603.0}
        assert result["deltas"]["score"] == pytest.approx(-138.0, abs=1e-6)

        # DISPLAY-ONLY: a negative Δscore must NOT drag the composite. The mock's
        # stats match the team-scope sample (a good diversifier), so `score` never
        # appears as a scored signal / row and the recommendation stays SUBMIT.
        analysis = result["analysis"]
        assert "score" not in analysis["signals"]
        assert "score" not in {r["metric"] for r in analysis["positives"]}
        assert "score" not in {r["metric"] for r in analysis["negatives"]}
        assert analysis["recommendation"] == "SUBMIT"

    @pytest.mark.asyncio
    async def test_scope_defaults_to_users_self(self, pg_session, test_alpha):
        svc = AlphaService(pg_session)
        mock = MockBrainAdapter()
        result = await svc.get_marginal_contribution(
            alpha_pk=test_alpha.id, brain_adapter=mock,
        )
        assert result is not None
        assert result["scope"] == "users/self"

    @pytest.mark.asyncio
    async def test_response_model_preserves_analysis(self, pg_session, test_alpha):
        """Regression: MarginalContributionResponse must keep `analysis` +
        `partition_name`. The service returns them, but a response_model that
        omits them makes Pydantic silently drop them on serialization → the
        AlphaDetail recommendation card (reads `analysis.*`) never renders.
        """
        from backend.routers.alphas import MarginalContributionResponse

        svc = AlphaService(pg_session)
        mock = MockBrainAdapter()
        result = await svc.get_marginal_contribution(
            alpha_pk=test_alpha.id, team_id="deLkl06", brain_adapter=mock,
        )
        assert result is not None
        dumped = MarginalContributionResponse(**result).model_dump()
        # The whole recommendation card hangs off these — they must survive.
        assert dumped.get("analysis") is not None
        assert dumped["analysis"]["recommendation"] == "SUBMIT"
        assert dumped["partition_name"] == "EQUITY:USA:1"

    @pytest.mark.asyncio
    async def test_scope_team_id(self, pg_session, test_alpha):
        svc = AlphaService(pg_session)
        mock = MockBrainAdapter()
        result = await svc.get_marginal_contribution(
            alpha_pk=test_alpha.id, team_id="deLkl06", brain_adapter=mock,
        )
        assert result is not None
        assert result["scope"] == "teams/deLkl06"

    @pytest.mark.asyncio
    async def test_returns_none_when_brain_payload_empty(
        self, pg_session, test_alpha, monkeypatch,
    ):
        svc = AlphaService(pg_session)
        mock = MockBrainAdapter()

        async def empty(*args, **kwargs):
            return {}
        monkeypatch.setattr(mock, "get_before_and_after_performance", empty)

        result = await svc.get_marginal_contribution(
            alpha_pk=test_alpha.id, brain_adapter=mock,
        )
        assert result is None
