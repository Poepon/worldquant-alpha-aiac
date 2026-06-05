"""Phase A — submitted-pool pillar profile (orthogonality-steered exploration).

Tests the data layer: compute_submitted_pool_profile (real infer_pillar
aggregation over canned rows + defensive empty-on-error) and the pure
render_profile_block (neutral framing, orthogonal target = highest-Sharpe
non-dominant pillar, empty → "" so the caller's prompt stays byte-for-byte).
"""
import pytest

from backend.submitted_pool_profile import (
    compute_submitted_pool_profile,
    render_profile_block,
)


# --- fakes: a session_factory() async-context yielding a db whose execute().all()
#     returns canned (expression, sharpe) rows. Exercises the REAL infer_pillar. ---
class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *_a, **_k):
        return _FakeResult(self._rows)


class _FakeFactory:
    def __init__(self, rows):
        self._rows = rows

    def __call__(self):
        return self

    async def __aenter__(self):
        return _FakeDB(self._rows)

    async def __aexit__(self, *_a):
        return False


class _BoomFactory:
    def __call__(self):
        return self

    async def __aenter__(self):
        raise RuntimeError("db down")

    async def __aexit__(self, *_a):
        return False


@pytest.mark.asyncio
async def test_compute_profile_aggregates_real_pillars():
    rows = [
        ("ts_decay_linear(-ts_rank(returns, 5), 10)", 2.01),                 # momentum
        ("multiply(-1, ts_decay_linear(ts_delta(returns, 5), 4))", 1.75),   # momentum
        ("group_neutralize(rank(ts_zscore(divide(cashflow_op, enterprise_value))))", 2.18),  # value-ish
        ("ts_std_dev(returns, 20)", 1.5),                                   # volatility
    ]
    prof = await compute_submitted_pool_profile(_FakeFactory(rows), "USA")
    assert prof["region"] == "USA"
    assert prof["n_total"] == 4
    # momentum present + dominant (2 of 4); every pillar carries n + mean_sharpe.
    assert prof["pillars"]["momentum"]["n"] == 2
    assert all({"n", "mean_sharpe"} <= set(d) for d in prof["pillars"].values())
    # sharpe averaged correctly for momentum (2.01 + 1.75)/2 = 1.88
    assert prof["pillars"]["momentum"]["mean_sharpe"] == pytest.approx(1.88, abs=0.01)


@pytest.mark.asyncio
async def test_compute_profile_empty_when_no_submitted():
    prof = await compute_submitted_pool_profile(_FakeFactory([]), "USA")
    assert prof["n_total"] == 0
    assert prof["pillars"] == {}
    assert render_profile_block(prof) == ""  # → caller skips injection (legacy)


@pytest.mark.asyncio
async def test_compute_profile_defensive_on_db_error():
    # ANY failure → empty profile (never raises into the mining hot path).
    prof = await compute_submitted_pool_profile(_BoomFactory(), "USA")
    assert prof["n_total"] == 0
    assert prof["region"] == "USA"


def test_render_empty_returns_blank():
    assert render_profile_block({"n_total": 0, "pillars": {}}) == ""
    assert render_profile_block({"n_total": 5, "pillars": {}}) == ""
    assert render_profile_block({}) == ""


def test_render_neutral_framing_and_orthogonal_target():
    # Mirrors the real 13-alpha profile: momentum dominant; value under-covered
    # but highest Sharpe (the orthogonal-AND-promising target).
    prof = {
        "region": "USA",
        "n_total": 13,
        "pillars": {
            "momentum": {"n": 6, "mean_sharpe": 1.68},
            "value": {"n": 2, "mean_sharpe": 2.25},
            "quality": {"n": 1, "mean_sharpe": 1.61},
        },
        "top_fields": ["returns", "close", "return_equity"],
    }
    block = render_profile_block(prof)
    assert block
    assert "momentum" in block                       # dominant surfaced
    assert "value" in block and "2.25" in block       # orthogonal target = max-Sharpe non-dominant
    assert "N=13" in block                            # sample-size honesty
    # neutral framing — nudge to explore orthogonal, NOT "momentum is bad"
    assert "正交" in block and "探索其它机制" in block
    assert "垃圾" not in block and "避免" not in block


def test_render_orthogonal_target_is_max_sharpe_not_just_least_covered():
    # quality has fewer samples (1) but value (2) has higher sharpe → target = value.
    prof = {
        "region": "USA", "n_total": 9,
        "pillars": {
            "momentum": {"n": 6, "mean_sharpe": 1.5},
            "value": {"n": 2, "mean_sharpe": 2.4},
            "quality": {"n": 1, "mean_sharpe": 1.0},
        },
        "top_fields": [],
    }
    block = render_profile_block(prof)
    assert "value" in block and "2.4" in block
