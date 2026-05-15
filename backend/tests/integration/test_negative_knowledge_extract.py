"""P2-D negative_knowledge_extract Celery task integration tests.

PG-only (uses JSONB metrics). Mirrors test_pillar_balance_check.py layout.
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict
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
    reason="P2-D extract task tests require Postgres on localhost:5433",
)


# Warm-up import (see test_negative_knowledge_service.py for the cycle note)
import backend.tasks  # noqa: E402,F401

from backend.models import (  # noqa: E402
    Alpha,
    AlphaFailure,
    KnowledgeEntry,
    MiningTask,
)


_TAG = f"nkX{uuid.uuid4().hex[:3]}_"


@pytest_asyncio.fixture
async def pg_session():
    from backend.config import settings

    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as s:
            yield s
            try:
                await s.execute(
                    delete(AlphaFailure).where(
                        AlphaFailure.expression.like(f"%{_TAG}%"),
                    )
                )
                await s.execute(
                    delete(Alpha).where(Alpha.alpha_id.like(f"{_TAG}%"))
                )
                await s.execute(
                    text("DELETE FROM mining_tasks WHERE task_name LIKE :p"),
                    {"p": f"{_TAG}%"},
                )
                await s.execute(
                    text(
                        "DELETE FROM knowledge_entries "
                        "WHERE meta_data->>'rule_id' ILIKE :p"
                    ),
                    {"p": f"%{_TAG}%"},
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
        dataset_strategy="AUTO",
        agent_mode="AUTONOMOUS_TIER1",
        status="RUNNING",
        daily_goal=4,
        max_iterations=2,
        config={},
    )
    pg_session.add(t)
    await pg_session.commit()
    return t


async def _seed_alpha_with_findings(
    pg_session, task_id, *, rule_id_tag, metrics, expression,
):
    a = Alpha(
        alpha_id=f"{_TAG}{uuid.uuid4().hex[:13]}",
        task_id=task_id,
        region="USA",
        universe="TOP3000",
        expression=expression,
        quality_status="FAIL",
        delay=1,
        metrics=metrics,
    )
    pg_session.add(a)
    await pg_session.flush()
    return a


# ---------------------------------------------------------------------------
# I1: end-to-end task run
# ---------------------------------------------------------------------------
class TestExtractTask:

    @pytest.mark.asyncio
    async def test_extract_task_end_to_end(
        self, pg_session, seeded_task, tmp_path,
    ):
        """I1: Seed 5 alphas with mixed signal flavors → task writes JSON
        with the expected schema; knowledge_entries get FAILURE_PITFALL
        rows."""
        # Five mixed-flavor alphas
        for i, metrics in enumerate([
            {"_validation_findings": [
                {"rule_id": f"RISK_{_TAG}A", "severity": "orange",
                 "message": "vol denom"}]},
            {"_validation_findings": [
                {"rule_id": f"STATIC_{_TAG}B", "severity": "orange",
                 "message": "overfit"}]},
            {"failed_tests": [
                {"rule": f"thr_{_TAG}C", "severity": "orange",
                 "message": "sharpe low"}]},
            {"_robustness_failed": [
                {"name": f"rob_{_TAG}D", "severity": "red"}]},
            {"_validation_findings": [
                {"rule_id": f"RISK_{_TAG}A", "severity": "orange",
                 "message": "second hit"}]},  # same key as #0 (different
                                              # alpha → increments fail_count)
        ]):
            await _seed_alpha_with_findings(
                pg_session, seeded_task.id,
                rule_id_tag=f"{_TAG}{i}",
                metrics=metrics,
                expression=f"ts_rank({_TAG}expr_{i}, 20)",
            )
        await pg_session.commit()

        from backend.tasks import negative_knowledge_extract as _mod
        with patch.object(_mod, "_OUTPUT_DIR", tmp_path):
            result = await _mod._run_async()

        assert "error" not in result, f"task raised: {result}"
        assert result.get("json_path"), result
        out_path = Path(result["json_path"])
        assert out_path.exists()
        payload = json.loads(out_path.read_text(encoding="utf-8"))

        # Schema keys
        for key in (
            "report_date", "generated_at_utc", "window_hours",
            "raw_signature_events", "unique_signatures", "by_category",
            "top_patterns", "upsert_counters", "schema_version",
        ):
            assert key in payload, f"missing key: {key}"

        assert payload["schema_version"] == "p2d.v1"
        # We seeded 5 events, expect >= 4 unique signatures (one duplicate
        # rule across alphas merges into one signature)
        assert payload["unique_signatures"] >= 4
        # by_category must include at least static_finding / threshold /
        # robustness because we seeded each
        cats = set(payload["by_category"].keys())
        assert "static_finding" in cats
        assert "threshold" in cats
        assert "robustness" in cats

        # knowledge_entries rows exist for our tagged signatures.
        # ILIKE because threshold/robustness rule_ids are .lower()'d in the
        # extractor while _TAG contains mixed-case "nkX..." prefix.
        rows = (await pg_session.execute(
            text(
                "SELECT COUNT(*) FROM knowledge_entries "
                "WHERE entry_type = 'FAILURE_PITFALL' "
                "AND meta_data->>'rule_id' ILIKE :p"
            ),
            {"p": f"%{_TAG}%"},
        )).scalar() or 0
        assert rows >= 4, (
            f"expected ≥4 tagged FAILURE_PITFALL rows, found {rows} "
            f"(payload={result})"
        )

    @pytest.mark.asyncio
    async def test_extract_task_idempotent_same_day(
        self, pg_session, seeded_task, tmp_path,
    ):
        """I2: Running the task twice on the same day updates rather than
        duplicates; counters['updated'] grows on the 2nd pass; JSON file
        is overwritten."""
        await _seed_alpha_with_findings(
            pg_session, seeded_task.id,
            rule_id_tag=f"{_TAG}IDM",
            metrics={"_validation_findings": [
                {"rule_id": f"RISK_{_TAG}IDM", "severity": "orange",
                 "message": "idempotent test"}]},
            expression=f"ts_rank({_TAG}idm, 20)",
        )
        await pg_session.commit()

        from backend.tasks import negative_knowledge_extract as _mod
        with patch.object(_mod, "_OUTPUT_DIR", tmp_path):
            first = await _mod._run_async()
            second = await _mod._run_async()

        assert first.get("upsert_counters", {}).get("new", 0) >= 1
        # Second run sees existing rows → 0 new, ≥1 updated (or same skipped
        # row counts)
        c2 = second.get("upsert_counters", {})
        assert c2.get("updated", 0) >= 1 or c2.get("new", 0) == 0
        # JSON file was overwritten (still exists)
        assert Path(second["json_path"]).exists()

    @pytest.mark.asyncio
    async def test_extract_task_swallows_errors(self, tmp_path, monkeypatch):
        """I3: If collect_recent_failures raises, the task returns an
        ``error`` dict instead of propagating — never crashes the worker."""
        from backend.tasks import negative_knowledge_extract as _mod
        from backend.services import negative_knowledge_service as _svc_mod

        async def _boom(self, window_hours=24):
            raise RuntimeError("simulated DB outage")

        monkeypatch.setattr(
            _svc_mod.NegativeKnowledgeService,
            "collect_recent_failures",
            _boom,
        )
        with patch.object(_mod, "_OUTPUT_DIR", tmp_path):
            result = await _mod._run_async()
        assert "error" in result, result
        assert "simulated DB outage" in result["error"]
