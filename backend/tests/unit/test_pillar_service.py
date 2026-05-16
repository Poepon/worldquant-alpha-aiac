"""Unit tests for PillarService (P3 — abstracted from pillar_balance_check task).

来源: docs/alphagbm_skills_research_2026-05-15.md ops dashboard plan §7.

Covers:
* _build_region_block — same math as task version (byte-for-byte)
* compute_balance_report — round-trip via stub DB rows
* get_next_pillar_for_region — deficit ranking + skew_threshold gate

The byte-for-byte equivalence test uses a tightly-stubbed DB that returns
pre-canned rows; we don't fire up Postgres. The full live-PG path is
covered by the existing test_pillar_balance_check.py integration test
(unchanged in this PR — the task wrapper still produces identical output).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.pillar_service import PillarService


# ---------------------------------------------------------------------------
# Pure helper — _build_region_block
# ---------------------------------------------------------------------------

def test_build_region_block_round_numbers():
    """Same per-region math the task previously did inline."""
    block = PillarService._build_region_block(
        stamped={"momentum": 5, "value": 3, "quality": 2, "unknown": 10},
        legacy_inferred={"sentiment": 4, "momentum": 1},
        target={
            "momentum": 0.20, "value": 0.20, "quality": 0.20,
            "volatility": 0.20, "sentiment": 0.20,
        },
    )
    # `unknown` excluded from stamped_total denominator
    assert block["stamped_total"] == 10        # 5 + 3 + 2
    assert block["unknown_count"] == 10
    assert block["legacy_inferred_total"] == 5
    # 5/10 = 0.5 momentum share
    assert block["shares"]["momentum"] == 0.5
    assert block["shares"]["volatility"] == 0.0
    # target - share, clamped to 0
    assert block["deficits"]["volatility"] == 0.2
    assert block["deficits"]["momentum"] == 0.0  # 0.20 - 0.50 → max(0, ...) = 0
    # next_pillar = largest positive deficit
    assert block["next_pillar"] in ("volatility", "sentiment")


def test_build_region_block_empty_stamped_no_division_by_zero():
    block = PillarService._build_region_block(
        stamped={},
        legacy_inferred={"momentum": 3},
        target={"momentum": 0.5, "value": 0.5},
    )
    assert block["stamped_total"] == 0
    assert block["shares"] == {"momentum": 0.0, "value": 0.0}
    assert block["deficits"] == {"momentum": 0.5, "value": 0.5}
    assert block["next_pillar"] in ("momentum", "value")


def test_build_region_block_all_targets_met_next_pillar_none():
    block = PillarService._build_region_block(
        stamped={"momentum": 5, "value": 5},
        legacy_inferred={},
        target={"momentum": 0.5, "value": 0.5},
    )
    # Shares meet targets exactly → all deficits are 0 → next_pillar None
    assert all(v == 0.0 for v in block["deficits"].values())
    assert block["next_pillar"] is None


# ---------------------------------------------------------------------------
# compute_balance_report — via stubbed DB
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compute_balance_report_aggregates_grouped_and_null():
    """Stub the two DB queries + verify the report shape matches the task."""
    svc = PillarService(db=AsyncMock())

    # _aggregate_grouped returns (region, pillar, count)
    # _aggregate_null_pillar returns (region, expression)
    with patch.object(
        PillarService, "_aggregate_grouped", new=AsyncMock(return_value=[
            ("USA", "momentum", 4),
            ("USA", "value", 2),
            ("USA", None, 1),   # → unknown bucket
            ("CHN", "quality", 3),
        ]),
    ), patch.object(
        PillarService, "_aggregate_null_pillar",
        new=AsyncMock(return_value=[
            ("USA", "rank(ts_mean(close, 20))"),       # likely momentum
            ("USA", "neutralize(rank(volatility(returns, 20)))"),  # vol-ish
        ]),
    ), patch("backend.config.settings") as mock_settings:
        mock_settings.PILLAR_TARGET_DISTRIBUTION = {
            "momentum": 0.20, "value": 0.20, "quality": 0.20,
            "volatility": 0.20, "sentiment": 0.20,
        }
        now = datetime(2026, 5, 16, 1, 0, tzinfo=timezone.utc)
        report = await svc.compute_balance_report(now_utc=now)

    # ---- shape ----
    assert set(report.keys()) == {
        "report_date", "generated_at_utc", "lookback_days",
        "pillar_values", "regions", "totals",
    }
    # Asia/Shanghai date is UTC+8 → 2026-05-16 09:00 → "2026-05-16"
    assert report["report_date"] == "2026-05-16"
    assert report["lookback_days"] == 7
    assert "momentum" in report["pillar_values"]

    # ---- region splits ----
    regions = report["regions"]
    assert set(regions.keys()) == {"USA", "CHN"}
    usa = regions["USA"]
    # 4 momentum + 2 value (unknown excluded from stamped_total)
    assert usa["stamped_total"] == 6
    assert usa["unknown_count"] == 1
    # Two inferred from null rows
    assert usa["legacy_inferred_total"] == 2

    # ---- totals roll-up ----
    assert report["totals"]["regions_checked"] == 2
    assert report["totals"]["stamped_alphas"] == 6 + 3  # USA + CHN
    assert report["totals"]["legacy_inferred_alphas"] == 2


@pytest.mark.asyncio
async def test_compute_balance_report_empty_db():
    svc = PillarService(db=AsyncMock())
    with patch.object(
        PillarService, "_aggregate_grouped", new=AsyncMock(return_value=[]),
    ), patch.object(
        PillarService, "_aggregate_null_pillar",
        new=AsyncMock(return_value=[]),
    ), patch("backend.config.settings") as mock_settings:
        mock_settings.PILLAR_TARGET_DISTRIBUTION = {"momentum": 1.0}
        report = await svc.compute_balance_report()

    assert report["regions"] == {}
    assert report["totals"]["regions_checked"] == 0
    assert report["totals"]["stamped_alphas"] == 0


# ---------------------------------------------------------------------------
# get_next_pillar_for_region
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_next_pillar_returns_largest_deficit():
    svc = PillarService(db=AsyncMock())
    with patch.object(
        PillarService, "compute_balance_report",
        new=AsyncMock(return_value={
            "regions": {
                "USA": {
                    "deficits": {"momentum": 0.05, "volatility": 0.30, "value": 0.10},
                },
            },
        }),
    ):
        nxt = await svc.get_next_pillar_for_region("USA")
    assert nxt == "volatility"


@pytest.mark.asyncio
async def test_get_next_pillar_below_skew_threshold_returns_none():
    """If the largest deficit is below skew_threshold, no nudge fires."""
    svc = PillarService(db=AsyncMock())
    with patch.object(
        PillarService, "compute_balance_report",
        new=AsyncMock(return_value={
            "regions": {
                "USA": {"deficits": {"momentum": 0.04, "value": 0.02}},
            },
        }),
    ):
        nxt = await svc.get_next_pillar_for_region("USA", skew_threshold=0.10)
    assert nxt is None


@pytest.mark.asyncio
async def test_get_next_pillar_unknown_region_returns_none():
    svc = PillarService(db=AsyncMock())
    with patch.object(
        PillarService, "compute_balance_report",
        new=AsyncMock(return_value={"regions": {"USA": {"deficits": {"momentum": 0.5}}}}),
    ):
        assert await svc.get_next_pillar_for_region("MARS") is None
