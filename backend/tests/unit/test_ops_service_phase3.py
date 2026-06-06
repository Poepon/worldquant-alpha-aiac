"""Unit tests for OpsService Phase 3 composers.

来源: docs/alphagbm_skills_research_2026-05-15.md ops dashboard plan Phase 3.

Covers the four new page composers (Pillar / Negative / Macro / Regime).
The underlying service methods (PillarService, NegativeKnowledgeService,
etc.) have their own integration suites that exercise live Postgres
JSONB; here we test the OpsService glue + the OpsReportReader double-
source story for each kind.

We mock the child services with AsyncMock — what we care about is that
the composer passes the right arguments down and shapes the return
properly for the router.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.ops_report_reader import (
    OpsReportReader,
    _reset_read_cache_for_tests,
)
from backend.services.ops_service import OpsService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_reader_cache():
    _reset_read_cache_for_tests()
    yield
    _reset_read_cache_for_tests()


@pytest.fixture
def docs_root(tmp_path: Path, monkeypatch) -> Path:
    docs = tmp_path / "docs"
    monkeypatch.setattr(
        "backend.services.ops_report_reader._DOCS_ROOT", docs,
    )
    return docs


def _write(docs_root: Path, kind: str, d: date, payload: dict) -> None:
    sub = docs_root / kind
    sub.mkdir(parents=True, exist_ok=True)
    (sub / f"{d.isoformat()}.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def svc():
    return OpsService(db=AsyncMock())


# ===========================================================================
# Pillar
# ===========================================================================

@pytest.mark.asyncio
async def test_get_pillar_latest_uses_fresh_service_today(svc, docs_root):
    today = OpsReportReader.today_sh()
    _write(docs_root, "pillar_balance", today, {"from": "stale_docs"})

    with patch(
        "backend.services.pillar_service.PillarService.compute_balance_report",
        new=AsyncMock(return_value={"from": "fresh", "report_date": today.isoformat()}),
    ):
        result = await svc.get_pillar_latest()

    # fresh_service path wins on T+0
    assert result["source"] == "service"
    assert result["payload"]["from"] == "fresh"


@pytest.mark.asyncio
async def test_get_pillar_latest_archive_for_past(svc, docs_root):
    """Past date never invokes fresh_service."""
    yesterday = OpsReportReader.today_sh() - timedelta(days=1)
    _write(docs_root, "pillar_balance", yesterday, {"from": "yesterday"})

    with patch(
        "backend.services.pillar_service.PillarService.compute_balance_report",
        new=AsyncMock(side_effect=AssertionError("must not be called")),
    ):
        result = await svc.get_pillar_latest(yesterday)
    assert result["source"] == "docs_archived"
    assert result["payload"]["from"] == "yesterday"


@pytest.mark.asyncio
async def test_get_pillar_history_chronological(svc, docs_root):
    today = OpsReportReader.today_sh()
    _write(docs_root, "pillar_balance", today, {"x": 1})
    _write(docs_root, "pillar_balance", today - timedelta(days=2), {"x": 2})
    out = await svc.get_pillar_history(days=5)
    assert [d["_date"] for d in out] == [
        (today - timedelta(days=2)).isoformat(),
        today.isoformat(),
    ]


@pytest.mark.asyncio
async def test_get_pillar_deficit_recommendation(svc):
    with patch(
        "backend.services.pillar_service.PillarService.get_next_pillar_for_region",
        new=AsyncMock(return_value="volatility"),
    ):
        result = await svc.get_pillar_deficit_recommendation("USA")
    assert result == {"region": "USA", "next_pillar": "volatility"}


# ===========================================================================
# Negative Knowledge
# ===========================================================================

@pytest.mark.asyncio
async def test_negative_top_passes_filters(svc):
    fake_rows = [
        {"id": 1, "pattern": "x", "fail_count": 9, "category": "scaffold"},
        {"id": 2, "pattern": "y", "fail_count": 5, "category": "sim_error"},
    ]
    with patch(
        "backend.services.negative_knowledge_service."
        "NegativeKnowledgeService.fetch_top_pitfalls_admin",
        new=AsyncMock(return_value=fake_rows),
    ) as mock_fetch:
        result = await svc.get_negative_knowledge_top(
            region="USA", limit=5, category="sim_error",
        )

    # Composer passes filters through
    mock_fetch.assert_awaited_once()
    kwargs = mock_fetch.call_args.kwargs
    assert kwargs == {
        "region": "USA", "limit": 5, "category_filter": "sim_error",
    }
    assert result["records"] == fake_rows
    assert result["source"] == "service"


@pytest.mark.asyncio
async def test_negative_category_breakdown(svc):
    with patch(
        "backend.services.negative_knowledge_service."
        "NegativeKnowledgeService.aggregate_by_category",
        new=AsyncMock(return_value={"scaffold": 12, "sim_error": 3}),
    ):
        result = await svc.get_negative_knowledge_category_breakdown(region="USA")
    assert result == {
        "by_category": {"scaffold": 12, "sim_error": 3},
        "source": "service",
    }


@pytest.mark.asyncio
async def test_negative_timeline(svc):
    fake_timeline = [
        {"date": "2026-05-15", "new_count": 3},
        {"date": "2026-05-16", "new_count": 7},
    ]
    with patch(
        "backend.services.negative_knowledge_service."
        "NegativeKnowledgeService.get_pitfall_timeline",
        new=AsyncMock(return_value=fake_timeline),
    ):
        out = await svc.get_negative_knowledge_timeline(days=7, region="USA")
    assert out == fake_timeline


@pytest.mark.asyncio
async def test_set_pitfall_active_delegates(svc):
    with patch(
        "backend.services.negative_knowledge_service."
        "NegativeKnowledgeService.set_pitfall_active",
        new=AsyncMock(return_value=True),
    ) as mock_set:
        ok = await svc.set_pitfall_active(42, False)
    assert ok is True
    mock_set.assert_awaited_once_with(42, False)


# ===========================================================================
# Macro Narratives
# ===========================================================================

@pytest.mark.asyncio
async def test_get_macro_latest_today(svc, docs_root):
    today = OpsReportReader.today_sh()
    _write(docs_root, "macro_narratives", today, {
        "report_date": today.isoformat(),
        "seed_counters": {"new": 3},
    })
    result = await svc.get_macro_latest()
    assert result["source"] == "docs_today"
    assert result["payload"]["seed_counters"]["new"] == 3


@pytest.mark.asyncio
async def test_macro_coverage(svc):
    with patch(
        "backend.services.macro_narrative_service."
        "MacroNarrativeService.coverage_stats",
        new=AsyncMock(return_value={
            "by_scope": {"field": 11, "category": 5},
            "total": 16,
            "fields_total": 200,
            "fields_with_narrative": 11,
            "fields_coverage_pct": 5.5,
        }),
    ):
        result = await svc.get_macro_coverage()
    assert result["coverage"]["total"] == 16
    assert result["coverage"]["fields_coverage_pct"] == 5.5


@pytest.mark.asyncio
async def test_macro_by_scope_field(svc):
    with patch(
        "backend.services.macro_narrative_service."
        "MacroNarrativeService.list_narratives_by_scope",
        new=AsyncMock(return_value=[
            {"id": 1, "scope": "field", "field_id": "close"},
        ]),
    ) as mock_list:
        result = await svc.get_macro_by_scope(scope="field", limit=10)
    mock_list.assert_awaited_once()
    args, kwargs = mock_list.call_args
    assert args[0] == "field"
    assert kwargs == {"dataset_category": None, "limit": 10}
    assert result["records"][0]["field_id"] == "close"


def test_macro_token_budget_redis_outage(monkeypatch):
    """Redis down → return zeros + redis_ok False, never raise."""
    def _boom():
        raise ConnectionError("redis off")
    monkeypatch.setattr(
        "backend.tasks.redis_pool.get_redis_client", _boom,
    )
    out = OpsService.get_macro_token_budget("2026-05-16")
    assert out["tokens_used"] == 0
    assert out["redis_ok"] is False


def test_macro_token_budget_reads_redis(monkeypatch):
    fake = MagicMock()
    fake.get.return_value = b"4200"
    monkeypatch.setattr(
        "backend.tasks.redis_pool.get_redis_client", lambda: fake,
    )
    out = OpsService.get_macro_token_budget("2026-05-16")
    assert out["tokens_used"] == 4200
    assert out["redis_ok"] is True
