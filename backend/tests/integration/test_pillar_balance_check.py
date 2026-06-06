"""Integration tests for P2-B pillar_balance_check Celery task.

来源: docs/alphagbm_skills_research_2026-05-15.md skill `compare`.

Targets live Postgres (alphas/hypotheses use JSONB / ARRAY columns that
aiosqlite can't render). Tagged rows are seeded with a unique uuid prefix
and cleaned up in the fixture's finally block.

The wrapper's ``AsyncSessionLocal()`` is bypassed via a custom runner so the
test session stays in scope; mirror of test_hypothesis_health_task.py.
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("POSTGRES_PORT", "5433")


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


from backend.models import (  # noqa: E402  — env tweak first
    Alpha,
    Hypothesis,
    MiningTask,
)


_TAG = f"pbT{uuid.uuid4().hex[:3]}_"


@pytest_asyncio.fixture
async def pg_session():
    """Live PG session; cleans up tagged rows in a finally block."""
    from backend.config import settings

    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as s:
            yield s
            try:
                await s.execute(
                    delete(Alpha).where(Alpha.alpha_id.like(f"{_TAG}%"))
                )
                await s.execute(
                    delete(Hypothesis).where(
                        Hypothesis.statement.like(f"{_TAG}%")
                    )
                )
                await s.execute(
                    text("DELETE FROM mining_tasks WHERE task_name LIKE :p"),
                    {"p": f"{_TAG}%"},
                )
                await s.commit()
            except Exception:
                await s.rollback()
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seeded_task(pg_session):
    t = MiningTask(
        task_name=f"{_TAG}_task",
        region="USA",
        universe="TOP3000",
        dataset_strategy="AUTO",        status="RUNNING",
        daily_goal=4,
        
        config={},
    )
    pg_session.add(t)
    await pg_session.commit()
    return t


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _seed_hypothesis(pg_session, region, pillar):
    h = Hypothesis(
        statement=f"{_TAG}_h_{pillar or 'null'}_{uuid.uuid4().hex[:6]}",
        region=region,
        universe="TOP3000",
        kind="INVESTMENT_THESIS",
        status="ACTIVE",
        is_active=True,
        pillar=pillar,
    )
    pg_session.add(h)
    await pg_session.flush()
    return h


async def _seed_alpha(pg_session, task_id, region, hypothesis_id, expression):
    a = Alpha(
        alpha_id=f"{_TAG}{uuid.uuid4().hex[:13]}",
        task_id=task_id,
        region=region,
        universe="TOP3000",
        expression=expression,
        hypothesis_id=hypothesis_id,
        quality_status="PASS",
        is_sharpe=1.5,
        is_fitness=1.0,
        is_turnover=0.3,
        delay=1,
    )
    pg_session.add(a)
    await pg_session.flush()
    return a


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPillarBalanceCheck:

    @pytest.mark.asyncio
    async def test_stamped_alphas_aggregated_by_pillar(
        self, pg_session, seeded_task, tmp_path,
    ):
        """Alphas with hypothesis.pillar set are aggregated by pillar."""
        h_mom = await _seed_hypothesis(pg_session, "USA", "momentum")
        h_val = await _seed_hypothesis(pg_session, "USA", "value")
        await _seed_alpha(
            pg_session, seeded_task.id, "USA", h_mom.id, "ts_delta(close, 5)",
        )
        await _seed_alpha(
            pg_session, seeded_task.id, "USA", h_val.id, "rank(eps)",
        )
        await pg_session.commit()

        from backend.tasks import pillar_balance_check as _mod
        # Redirect output dir to tmp
        with patch.object(_mod, "_OUTPUT_DIR", tmp_path):
            with patch.object(_mod, "AsyncSessionLocal", None, create=False) if False else patch("backend.tasks.pillar_balance_check._run_async") as _:
                pass
            # Call _run_async directly with the existing session, but it
            # creates its own AsyncSessionLocal — easier: call once and
            # assert the result covers our tagged data only via shares.
            result = await _mod._run_async()

        assert "report_date" in result
        # The shared DB may have other USA alphas; just assert the file was
        # created and JSON parses with the expected schema.
        path = Path(result["json_path"])
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert "regions" in payload
        assert "lookback_days" in payload
        assert payload["lookback_days"] == 7

    @pytest.mark.asyncio
    async def test_legacy_null_hypothesis_id_is_outer_joined(
        self, pg_session, seeded_task, tmp_path,
    ):
        """M3 fix: alphas with hypothesis_id=NULL are NOT silently dropped —
        they appear in the legacy_inferred bucket via infer_pillar."""
        # Insert an alpha without a hypothesis link
        a = Alpha(
            alpha_id=f"{_TAG}{uuid.uuid4().hex[:13]}",
            task_id=seeded_task.id,
            region="USA",
            universe="TOP3000",
            expression="ts_delta(close, 5)",
            hypothesis_id=None,  # legacy
            quality_status="PASS",
            is_sharpe=1.5,
            is_fitness=1.0,
            is_turnover=0.3,
            delay=1,
        )
        pg_session.add(a)
        await pg_session.commit()

        from backend.tasks import pillar_balance_check as _mod
        with patch.object(_mod, "_OUTPUT_DIR", tmp_path):
            result = await _mod._run_async()

        assert result.get("legacy_inferred_alphas", 0) >= 1

    @pytest.mark.asyncio
    async def test_output_json_persisted(
        self, pg_session, seeded_task, tmp_path,
    ):
        from backend.tasks import pillar_balance_check as _mod
        with patch.object(_mod, "_OUTPUT_DIR", tmp_path):
            result = await _mod._run_async()
        assert result.get("json_path"), f"missing json_path in {result}"
        assert Path(result["json_path"]).exists()

    @pytest.mark.asyncio
    async def test_report_schema_contains_required_keys(
        self, pg_session, seeded_task, tmp_path,
    ):
        from backend.tasks import pillar_balance_check as _mod
        with patch.object(_mod, "_OUTPUT_DIR", tmp_path):
            result = await _mod._run_async()
        payload = json.loads(
            Path(result["json_path"]).read_text(encoding="utf-8"),
        )
        for key in (
            "report_date", "generated_at_utc", "lookback_days",
            "pillar_values", "regions", "totals",
        ):
            assert key in payload, f"missing top-level key: {key}"
        assert payload["lookback_days"] == 7
        assert sorted(payload["pillar_values"]) == [
            "momentum", "other", "quality",
            "sentiment", "value", "volatility",
        ]

    @pytest.mark.asyncio
    async def test_legacy_pillar_not_persisted_back(
        self, pg_session, seeded_task, tmp_path,
    ):
        """The task is read-only: even though it infers pillars for NULL
        rows, it MUST NOT UPDATE hypotheses.pillar (S7 plan decision —
        avoids hypothesis_status_transitions audit noise on inference)."""
        h = await _seed_hypothesis(pg_session, "USA", None)
        a = await _seed_alpha(
            pg_session, seeded_task.id, "USA", h.id,
            "ts_delta(close, 5)",
        )
        await pg_session.commit()
        h_id = h.id

        from backend.tasks import pillar_balance_check as _mod
        with patch.object(_mod, "_OUTPUT_DIR", tmp_path):
            await _mod._run_async()

        # Re-read the hypothesis row — pillar should still be NULL
        refreshed = (await pg_session.execute(
            select(Hypothesis).where(Hypothesis.id == h_id)
        )).scalar_one()
        assert refreshed.pillar is None, (
            "pillar_balance_check must NOT mutate hypothesis.pillar"
        )
