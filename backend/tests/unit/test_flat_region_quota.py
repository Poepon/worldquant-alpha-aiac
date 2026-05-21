"""Phase 4 Sprint 1 A3 — flat-F4 cross-region quota unit tests.

Coverage:
  - compute_region_share against in-memory aiosqlite (real ORM)
  - check_quota pure-function decision logic (would_exceed cases)
  - build_distribution_summary status flags (ok / warn / exceeded / no_quota)
  - soft-fail on missing share data
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest


# ---------------------------------------------------------------------------
# compute_region_share — real ORM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_region_share_groups_by_region(db_session):
    """3 USA + 2 CHN active tasks → 60/40 share."""
    from backend.models import MiningTask
    from backend.services.flat_region_quota import compute_region_share

    for region in ("USA", "USA", "USA", "CHN", "CHN"):
        db_session.add(MiningTask(
            task_name=f"t_{region}_{id(region)}",
            region=region,
            universe="TOP3000",
            status="RUNNING",
            config={},
        ))
    await db_session.commit()

    share = await compute_region_share(db_session, lookback_days=30)
    assert share["__total__"]["count"] == 5
    assert share["USA"]["count"] == 3
    assert share["USA"]["share"] == pytest.approx(0.6, abs=0.01)
    assert share["CHN"]["count"] == 2
    assert share["CHN"]["share"] == pytest.approx(0.4, abs=0.01)


@pytest.mark.asyncio
async def test_compute_region_share_excludes_completed(db_session):
    """COMPLETED + STOPPED tasks must NOT count toward active share."""
    from backend.models import MiningTask
    from backend.services.flat_region_quota import compute_region_share

    db_session.add_all([
        MiningTask(task_name="t1", region="USA", universe="TOP3000",
                   status="RUNNING", config={}),
        MiningTask(task_name="t2", region="USA", universe="TOP3000",
                   status="COMPLETED", config={}),  # excluded
        MiningTask(task_name="t3", region="USA", universe="TOP3000",
                   status="STOPPED", config={}),    # excluded
        MiningTask(task_name="t4", region="CHN", universe="TOP3000",
                   status="PAUSED", config={}),     # PAUSED still counts
    ])
    await db_session.commit()

    share = await compute_region_share(db_session, lookback_days=30)
    assert share["__total__"]["count"] == 2  # 1 RUNNING + 1 PAUSED
    assert share["USA"]["count"] == 1
    assert share["CHN"]["count"] == 1


@pytest.mark.asyncio
async def test_compute_region_share_empty_db(db_session):
    """No tasks → returns __total__ with count=0, no other regions."""
    from backend.services.flat_region_quota import compute_region_share

    share = await compute_region_share(db_session, lookback_days=30)
    assert share == {"__total__": {"count": 0, "share": 0.0}}


@pytest.mark.asyncio
async def test_compute_region_share_soft_fails_on_broken_session():
    """When session.execute raises, return empty dict (router treats as
    'skip the check')."""
    from backend.services.flat_region_quota import compute_region_share

    class BrokenSession:
        async def execute(self, *a, **kw):
            raise ConnectionError("db down")
    result = await compute_region_share(BrokenSession(), lookback_days=30)
    assert result == {}


# ---------------------------------------------------------------------------
# check_quota — pure function decision logic
# ---------------------------------------------------------------------------


def test_check_quota_under_cap_returns_false():
    """USA 30% cap, current share 20% (2/10), adding 1 → 3/11 = 27% → OK."""
    from backend.services.flat_region_quota import check_quota
    share = {
        "USA": {"count": 2, "share": 0.20},
        "CHN": {"count": 8, "share": 0.80},
        "__total__": {"count": 10, "share": 1.0},
    }
    d = check_quota(new_region="USA", current_share=share, quota={"USA": 0.30})
    assert d["would_exceed"] is False
    assert d["projected_count"] == 3
    assert d["projected_total"] == 11
    assert d["projected_share"] == pytest.approx(3 / 11)


def test_check_quota_over_cap_returns_true():
    """USA 30% cap, current 4/10=40%, adding 1 → 5/11=45% → EXCEEDED."""
    from backend.services.flat_region_quota import check_quota
    share = {
        "USA": {"count": 4, "share": 0.40},
        "CHN": {"count": 6, "share": 0.60},
        "__total__": {"count": 10, "share": 1.0},
    }
    d = check_quota(new_region="USA", current_share=share, quota={"USA": 0.30})
    assert d["would_exceed"] is True
    assert d["quota"] == pytest.approx(0.30)
    assert d["projected_share"] > 0.30


def test_check_quota_missing_region_in_quota_uncapped():
    """If new_region isn't in QUOTA dict, treat as 1.0 (uncapped)."""
    from backend.services.flat_region_quota import check_quota
    share = {
        "USA": {"count": 100, "share": 1.0},
        "__total__": {"count": 100, "share": 1.0},
    }
    # KOR isn't in quota → uncapped → never exceed
    d = check_quota(new_region="KOR", current_share=share, quota={"USA": 0.30})
    assert d["would_exceed"] is False
    assert d["quota"] == 1.0


def test_check_quota_empty_share_skips_with_reason():
    """compute_region_share returned {} (soft-fail) → decision says skip."""
    from backend.services.flat_region_quota import check_quota
    d = check_quota(
        new_region="USA",
        current_share={},
        quota={"USA": 0.30},
    )
    assert d["would_exceed"] is False
    assert d["skip_reason"] == "no_share_data"


# ---------------------------------------------------------------------------
# build_distribution_summary — status flags
# ---------------------------------------------------------------------------


def test_distribution_summary_status_flags():
    """Verifies ok / warn (>=90% of cap) / exceeded / no_quota status flags."""
    from backend.services.flat_region_quota import build_distribution_summary
    share = {
        "USA": {"count": 5, "share": 0.50},   # 50% > 30% cap → exceeded
        "CHN": {"count": 2, "share": 0.20},   # 20% / 20% cap → 100% of cap → warn (>=90%)
        "JPN": {"count": 1, "share": 0.10},   # 10% < 15% cap → ok (< 90% of cap)
        "KOR": {"count": 2, "share": 0.20},   # not in quota → no_quota
        "__total__": {"count": 10, "share": 1.0},
    }
    quota = {"USA": 0.30, "CHN": 0.20, "JPN": 0.15, "EUR": 0.20, "HKG": 0.15}
    summary = build_distribution_summary(share, quota)
    by_region = {r["region"]: r for r in summary["regions"]}
    assert by_region["USA"]["status"] == "exceeded"
    assert by_region["CHN"]["status"] == "warn"
    assert by_region["JPN"]["status"] == "ok"
    # EUR + HKG have 0 tasks but are in quota → status=ok with count=0
    assert by_region["EUR"]["count"] == 0
    assert by_region["EUR"]["status"] == "ok"
    # KOR not in quota → no_quota chip
    assert by_region["KOR"]["status"] == "no_quota"
    assert by_region["KOR"]["quota"] is None
    assert summary["total_active_tasks"] == 10
