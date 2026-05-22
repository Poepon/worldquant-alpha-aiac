"""
Unit Tests - Service Layer

Tests for AlphaService, DashboardService, and MiningService.
"""

import pytest
import pytest_asyncio
from datetime import datetime, timedelta

from backend.services import AlphaService, AlphaListFilters, DashboardService
from backend.models import Alpha, MiningTask


class TestAlphaService:
    """Tests for AlphaService."""
    
    @pytest.mark.asyncio
    async def test_list_alphas_empty(self, alpha_service):
        """Test listing alphas when none exist."""
        filters = AlphaListFilters()
        
        items, total = await alpha_service.list_alphas(filters)
        
        assert isinstance(items, list)
        assert total >= 0
    
    @pytest.mark.asyncio
    async def test_list_alphas_with_data(self, alpha_service, sample_alpha):
        """Test listing alphas with data."""
        filters = AlphaListFilters()
        
        items, total = await alpha_service.list_alphas(filters)
        
        assert total >= 1
        assert len(items) >= 1
    
    @pytest.mark.asyncio
    async def test_list_alphas_with_filters(self, alpha_service, sample_alpha):
        """Test listing alphas with filters."""
        filters = AlphaListFilters(region="USA")
        
        items, total = await alpha_service.list_alphas(filters)
        
        assert all(item.region == "USA" for item in items)
    
    @pytest.mark.asyncio
    async def test_get_alpha(self, alpha_service, sample_alpha):
        """Test getting alpha by ID."""
        alpha = await alpha_service.get_alpha(sample_alpha.id)
        
        assert alpha is not None
        assert alpha.id == sample_alpha.id
        assert alpha.expression == sample_alpha.expression
    
    @pytest.mark.asyncio
    async def test_get_alpha_not_found(self, alpha_service):
        """Test getting non-existent alpha."""
        alpha = await alpha_service.get_alpha(99999)
        
        assert alpha is None
    
    @pytest.mark.asyncio
    async def test_get_alpha_by_brain_id(self, alpha_service, sample_alpha):
        """Test getting alpha by BRAIN ID."""
        alpha = await alpha_service.get_alpha_by_brain_id(sample_alpha.alpha_id)
        
        assert alpha is not None
        assert alpha.alpha_id == sample_alpha.alpha_id
    
    @pytest.mark.asyncio
    async def test_submit_feedback(self, alpha_service, sample_alpha):
        """Test submitting feedback."""
        success = await alpha_service.submit_feedback(
            alpha_id=sample_alpha.id,
            rating="LIKED",
            comment="Great alpha!",
        )
        
        assert success is True
    
    @pytest.mark.asyncio
    async def test_submit_feedback_invalid_rating(self, alpha_service, sample_alpha):
        """Test submitting invalid feedback."""
        with pytest.raises(ValueError):
            await alpha_service.submit_feedback(
                alpha_id=sample_alpha.id,
                rating="INVALID",
            )
    
    @pytest.mark.asyncio
    async def test_submit_feedback_not_found(self, alpha_service):
        """Test submitting feedback for non-existent alpha."""
        success = await alpha_service.submit_feedback(
            alpha_id=99999,
            rating="LIKED",
        )
        
        assert success is False


class TestDashboardService:
    """Tests for DashboardService."""
    
    @pytest.mark.asyncio
    async def test_get_daily_stats(self, dashboard_service):
        """Test getting daily stats."""
        stats = await dashboard_service.get_daily_stats()
        
        assert stats is not None
        assert stats.date is not None
        assert isinstance(stats.goal, int)
        assert isinstance(stats.total_simulations, int)
    
    @pytest.mark.asyncio
    async def test_get_daily_stats_specific_date(self, dashboard_service):
        """Test getting daily stats for specific date."""
        from datetime import date
        
        yesterday = date.today() - timedelta(days=1)
        stats = await dashboard_service.get_daily_stats(yesterday)
        
        assert stats.date == yesterday.isoformat()
    
    @pytest.mark.asyncio
    async def test_get_active_tasks_empty(self, dashboard_service):
        """Test getting active tasks when none running."""
        tasks = await dashboard_service.get_active_tasks()
        
        assert isinstance(tasks, list)
    
    @pytest.mark.asyncio
    async def test_get_kpi_metrics(self, dashboard_service):
        """Test getting KPI metrics."""
        kpi = await dashboard_service.get_kpi_metrics()
        
        assert kpi is not None
        assert isinstance(kpi.today_simulations, int)
        assert isinstance(kpi.today_success_rate, float)
        assert isinstance(kpi.week_total_alphas, int)
    
    @pytest.mark.asyncio
    async def test_get_recent_trace_steps(self, dashboard_service):
        """Test getting recent trace steps."""
        steps = await dashboard_service.get_recent_trace_steps()
        
        assert isinstance(steps, list)
    
    @pytest.mark.asyncio
    async def test_get_task_status_counts(self, dashboard_service, sample_task):
        """Test getting task status counts."""
        counts = await dashboard_service.get_task_status_counts()

        assert isinstance(counts, dict)

    @pytest.mark.asyncio
    async def test_kpi_excludes_brain_synced_alphas(
        self, dashboard_service, db_session, sample_task
    ):
        """KPI must not count BRAIN-synced alphas (task_id IS NULL).

        Regression for the dashboard-data-inaccurate bug: sync_user_alphas
        inserts user's BRAIN history with task_id=NULL. Counting them as
        "today's simulations" inflated every KPI on first-sync day.
        """
        today = datetime.now()

        # 3 BRAIN-synced (task_id=NULL) — must NOT count
        for i in range(3):
            db_session.add(
                Alpha(
                    alpha_id=f"brain-sync-{i}",
                    task_id=None,
                    expression="rank(close)",
                    region="USA",
                    universe="TOP3000",
                    quality_status="PASS",
                    is_sharpe=2.0,
                )
            )

        # 2 AIAC-mined (task_id set, PASS) — must count
        for i in range(2):
            db_session.add(
                Alpha(
                    alpha_id=f"aiac-pass-{i}",
                    task_id=sample_task.id,
                    expression="rank(volume)",
                    region="USA",
                    universe="TOP3000",
                    quality_status="PASS",
                    is_sharpe=1.5,
                )
            )

        # 1 AIAC-mined PENDING — counts in today_simulations, not in today_passed
        db_session.add(
            Alpha(
                alpha_id="aiac-pending-0",
                task_id=sample_task.id,
                expression="rank(returns)",
                region="USA",
                universe="TOP3000",
                quality_status="PENDING",
            )
        )
        await db_session.commit()

        kpi = await dashboard_service.get_kpi_metrics()

        # 2 PASS + 1 PENDING AIAC-mined = 3 sims total. BRAIN-synced 3 excluded.
        assert kpi.today_simulations == 3
        # 2 PASS / 3 sims
        assert kpi.today_success_rate == pytest.approx(2 / 3, rel=1e-3)
        # avg sharpe over 2 AIAC PASS alphas = 1.5 (BRAIN sharpe 2.0 excluded)
        assert kpi.today_avg_sharpe == pytest.approx(1.5, rel=1e-3)
        # week total = 2 AIAC PASS (BRAIN-synced excluded)
        assert kpi.week_total_alphas == 2

    @pytest.mark.asyncio
    async def test_daily_stats_excludes_brain_synced_alphas(
        self, dashboard_service, db_session, sample_task
    ):
        """get_daily_stats must filter BRAIN-synced alphas the same way."""
        # 5 BRAIN-synced PASS — must not be counted toward avg_sharpe
        for i in range(5):
            db_session.add(
                Alpha(
                    alpha_id=f"brain-{i}",
                    task_id=None,
                    expression="rank(close)",
                    region="USA",
                    universe="TOP3000",
                    quality_status="PASS",
                    is_sharpe=3.0,
                )
            )
        # 1 AIAC PASS — only contributor to avg_sharpe
        db_session.add(
            Alpha(
                alpha_id="aiac-pass",
                task_id=sample_task.id,
                expression="rank(volume)",
                region="USA",
                universe="TOP3000",
                quality_status="PASS",
                is_sharpe=1.0,
            )
        )
        await db_session.commit()

        stats = await dashboard_service.get_daily_stats()
        # avg of the 1 AIAC alpha (1.0), not avg of 6 (BRAIN-polluted = ~2.67)
        assert stats.avg_sharpe == pytest.approx(1.0, rel=1e-3)
        assert stats.total_simulations == 1
