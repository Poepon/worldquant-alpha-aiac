"""Unit tests for OpsService Phase 2 composers.

来源: docs/alphagbm_skills_research_2026-05-15.md ops dashboard plan §1.5 / Phase 2.

Covers the pure-derivation paths:

* _summarize_alpha_health: band counting + per-region breakdown + pct calc
* _summarize_hypothesis_health: trigger histogram + score buckets + avg
* get_alpha_health: round-trip via OpsReportReader (tmp_path docs root)
* get_alpha_health_records: band / region filter + limit
* get_alpha_health_history: chronological order + per-day summary
* get_hypothesis_health + history: same shape
* get_overview: 7 beat sources aggregated + region_regime + top_pitfalls

We do NOT exercise get_hypothesis_transitions here — it's pure DB
read and lives behind a model that needs a Postgres + JSONB schema (the
transition table imports Boolean/Text only but the surrounding metadata
collection drags JSONB in). It gets coverage via the integration tests.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

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
    """Redirect OpsReportReader's _DOCS_ROOT to a tmp dir for hermetic tests."""
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
    return OpsService(db=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure summary helpers
# ---------------------------------------------------------------------------

def test_summarize_alpha_health_counts_bands_and_per_region():
    payload = {
        "report_date": "2026-05-16",
        "totals": {"checked": 5},
        "records": [
            {"region": "USA", "health_band": "GREEN"},
            {"region": "USA", "health_band": "green"},   # case folded
            {"region": "USA", "health_band": "RED"},
            {"region": "CHN", "health_band": "YELLOW"},
            {"region": "CHN", "health_band": None},      # → UNKNOWN
        ],
    }
    s = OpsService._summarize_alpha_health(payload, source="docs_today")

    assert s.report_date == "2026-05-16"
    assert s.band_counts == {"GREEN": 2, "RED": 1, "YELLOW": 1, "UNKNOWN": 1}
    # 2/5 = 40.0
    assert s.band_pcts["GREEN"] == 40.0
    assert s.by_region["USA"] == {"GREEN": 2, "RED": 1}
    assert s.by_region["CHN"] == {"YELLOW": 1, "UNKNOWN": 1}
    assert s.total_alphas == 5     # from `totals.checked`, overrides derived
    assert s.record_count == 5
    assert s.source == "docs_today"


def test_summarize_alpha_health_empty_payload():
    s = OpsService._summarize_alpha_health({}, source="missing")
    assert s.band_counts == {}
    assert s.band_pcts == {}
    assert s.total_alphas == 0
    assert s.source == "missing"


def test_summarize_alpha_health_uses_alphas_key_fallback():
    """Some older snapshots used `alphas` instead of `records`."""
    payload = {"alphas": [{"region": "USA", "health_band": "GREEN"}]}
    s = OpsService._summarize_alpha_health(payload, source="docs_archived")
    assert s.record_count == 1
    assert s.band_counts == {"GREEN": 1}


def test_summarize_hypothesis_health_buckets_and_histogram():
    payload = {
        "report_date": "2026-05-16",
        "hypotheses": [
            {
                "is_triggered": True,
                "thesis_score": 75,
                "trigger_detail": {"fired": ["sharpe_down_50pct", "pass_rate_dropped"]},
            },
            {
                "is_triggered": True,
                "thesis_score": 30,
                "trigger_detail": {"fired": ["sharpe_down_50pct"]},
            },
            {"is_triggered": False, "thesis_score": 95},
            {"is_triggered": False, "thesis_score": None},
        ],
    }
    s = OpsService._summarize_hypothesis_health(payload, source="docs_today")
    assert s["total_active"] == 4
    assert s["total_triggered"] == 2
    # avg of 75, 30, 95 = 66.67
    assert s["avg_thesis_score"] == pytest.approx(66.67, abs=0.01)
    # 75 → "60-80", 30 → "20-40", 95 → "80-100"
    assert s["score_buckets"] == {"60-80": 1, "20-40": 1, "80-100": 1}
    assert s["trigger_histogram"] == {"sharpe_down_50pct": 2, "pass_rate_dropped": 1}
    assert s["source"] == "docs_today"


def test_summarize_hypothesis_health_no_scores():
    """No scored hypothesis → avg_thesis_score is None (not 0)."""
    s = OpsService._summarize_hypothesis_health(
        {"hypotheses": [{"is_triggered": False}]}, source="docs_today",
    )
    assert s["avg_thesis_score"] is None
    assert s["score_buckets"] == {}


# ---------------------------------------------------------------------------
# get_alpha_health round-trip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_alpha_health_today_via_docs(svc, docs_root):
    today = OpsReportReader.today_sh()
    _write(docs_root, "alpha_health_check", today, {
        "report_date": today.isoformat(),
        "records": [
            {"region": "USA", "health_band": "GREEN"},
            {"region": "USA", "health_band": "RED"},
        ],
    })
    result = await svc.get_alpha_health()
    assert result["source"] == "docs_today"
    assert result["summary"].band_counts == {"GREEN": 1, "RED": 1}
    assert result["payload"]["report_date"] == today.isoformat()


@pytest.mark.asyncio
async def test_get_alpha_health_missing(svc, docs_root):
    result = await svc.get_alpha_health()
    assert result["source"] == "missing"
    assert result["payload"] == {}
    assert result["summary"].total_alphas == 0


@pytest.mark.asyncio
async def test_get_alpha_health_archive_fallback(svc, docs_root):
    """Yesterday's file present, today's missing → archived + _stale_days."""
    yesterday = OpsReportReader.today_sh() - timedelta(days=1)
    _write(docs_root, "alpha_health_check", yesterday, {
        "report_date": yesterday.isoformat(),
        "records": [{"region": "USA", "health_band": "YELLOW"}],
    })
    result = await svc.get_alpha_health()
    assert result["source"] == "docs_archived"
    assert result["summary"].stale_days == 1


# ---------------------------------------------------------------------------
# get_alpha_health_records — filtering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_records_filter_by_band(svc, docs_root):
    today = OpsReportReader.today_sh()
    _write(docs_root, "alpha_health_check", today, {
        "records": [
            {"alpha_id": "A1", "region": "USA", "health_band": "GREEN"},
            {"alpha_id": "A2", "region": "USA", "health_band": "RED"},
            {"alpha_id": "A3", "region": "CHN", "health_band": "RED"},
        ],
    })
    result = await svc.get_alpha_health_records(bands=["red"])
    assert {r["alpha_id"] for r in result["records"]} == {"A2", "A3"}
    assert result["total_unfiltered"] == 3


@pytest.mark.asyncio
async def test_records_filter_by_region(svc, docs_root):
    today = OpsReportReader.today_sh()
    _write(docs_root, "alpha_health_check", today, {
        "records": [
            {"alpha_id": "A1", "region": "USA", "health_band": "GREEN"},
            {"alpha_id": "A2", "region": "CHN", "health_band": "GREEN"},
        ],
    })
    result = await svc.get_alpha_health_records(region="USA")
    assert [r["alpha_id"] for r in result["records"]] == ["A1"]


@pytest.mark.asyncio
async def test_records_limit_enforced(svc, docs_root):
    today = OpsReportReader.today_sh()
    _write(docs_root, "alpha_health_check", today, {
        "records": [
            {"alpha_id": f"A{i}", "region": "USA", "health_band": "GREEN"}
            for i in range(50)
        ],
    })
    result = await svc.get_alpha_health_records(limit=5)
    assert len(result["records"]) == 5
    assert result["total_unfiltered"] == 50


# ---------------------------------------------------------------------------
# get_alpha_health_history
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_history_returns_chronological(svc, docs_root):
    today = OpsReportReader.today_sh()
    _write(docs_root, "alpha_health_check", today, {
        "records": [{"region": "USA", "health_band": "GREEN"}],
    })
    _write(docs_root, "alpha_health_check", today - timedelta(days=2), {
        "records": [{"region": "USA", "health_band": "RED"}],
    })
    out = await svc.get_alpha_health_history(days=5)
    assert [d["_date"] for d in out] == [
        (today - timedelta(days=2)).isoformat(),
        today.isoformat(),
    ]
    assert out[-1]["band_counts"] == {"GREEN": 1}
    assert out[0]["band_counts"] == {"RED": 1}


# ---------------------------------------------------------------------------
# get_hypothesis_health
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_hypothesis_health_today(svc, docs_root):
    today = OpsReportReader.today_sh()
    _write(docs_root, "hypothesis_health_check", today, {
        "report_date": today.isoformat(),
        "hypotheses": [
            {"is_triggered": True, "thesis_score": 50,
             "trigger_detail": {"fired": ["sharpe_down"]}},
            {"is_triggered": False, "thesis_score": 80},
        ],
    })
    result = await svc.get_hypothesis_health()
    assert result["source"] == "docs_today"
    s = result["summary"]
    assert s["total_active"] == 2
    assert s["total_triggered"] == 1
    assert s["trigger_histogram"] == {"sharpe_down": 1}


# ---------------------------------------------------------------------------
# get_overview — fans out to seven readers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_overview_aggregates_all_sources(svc, docs_root):
    today = OpsReportReader.today_sh()
    _write(docs_root, "alpha_health_check", today, {
        "report_date": today.isoformat(),
        "records": [{"region": "USA", "health_band": "GREEN"}],
    })
    _write(docs_root, "hypothesis_health_check", today, {
        "report_date": today.isoformat(),
        "hypotheses": [{"is_triggered": False, "thesis_score": 70}],
    })
    _write(docs_root, "regime_state", today, {
        "regions": {"USA": {"regime": "calm"}, "CHN": {"regime": "elevated"}},
    })
    _write(docs_root, "negative_knowledge", today, {
        "top_patterns": [
            {"signature_key": "k1", "fail_count": 9},
            {"signature_key": "k2", "fail_count": 5},
        ],
    })

    overview = await svc.get_overview()

    # Every beat key present
    expected_beats = {
        "alpha_health_check", "hypothesis_health_check", "pillar_balance",
        "regime_infer", "negative_knowledge_extract",
        "macro_narrative_extract", "llm_op_monitor",
    }
    assert expected_beats == set(overview["beat_status"].keys())

    # Files we wrote → docs_today; the others → missing
    assert overview["beat_status"]["alpha_health_check"]["source"] == "docs_today"
    assert overview["beat_status"]["pillar_balance"]["source"] == "missing"

    # Derived summaries flow through
    assert overview["alpha_health_summary"].band_counts == {"GREEN": 1}
    assert overview["hypothesis_health_summary"]["total_active"] == 1
    assert overview["region_regime"] == {"USA": "calm", "CHN": "elevated"}
    assert len(overview["top_pitfalls"]) == 2
    assert overview["top_pitfalls"][0]["signature_key"] == "k1"


@pytest.mark.asyncio
async def test_get_overview_handles_all_missing(svc, docs_root):
    """Completely empty docs/ — overview must still return a well-formed shape."""
    overview = await svc.get_overview()
    for beat_meta in overview["beat_status"].values():
        assert beat_meta["source"] == "missing"
        assert beat_meta["date"] is None
    assert overview["region_regime"] == {}
    assert overview["top_pitfalls"] == []
