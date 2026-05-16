"""Integration: BRAIN capability strict isolation (P3-Brain plan §14.2).

Verifies the simulate_batch entrypoint shortcut keeps User-mode callers
from ever hitting BRAIN's multi-sim endpoint:
  - User mode (flag=False) → routed through _simulate_via_single (single-sim
    loop), no `POST /simulations` with list body, no Redis latch interaction
  - Consultant mode (flag=True) → enters the existing multi-sim code path
  - Revert mid-task (flag flipped False after task started) → immediate
    downgrade despite task-snapshot saying mode_at_start=True (Direction C:
    endpoint selection follows global flag, not snapshot)

We patch _simulate_via_single + _get_slot_redis so we can assert call
counts without an actual BRAIN HTTP roundtrip or Redis connection.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.adapters.brain_adapter import BrainAdapter
from backend.config import _flag_override_cache


@pytest.fixture(autouse=True)
def _clear_flag_cache():
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


@pytest.mark.asyncio
async def test_user_mode_simulate_batch_routes_to_single_sim_only():
    """User mode: simulate_batch entrypoint shortcuts to _simulate_via_single.
    Zero multi-sim Redis latch interactions, zero multi-sim POST."""
    adapter = BrainAdapter()
    fake_results = [{"success": True, "alpha_id": f"x{i}"} for i in range(3)]

    with (
        patch.object(
            adapter, "_simulate_via_single",
            new=AsyncMock(return_value=fake_results),
        ) as mock_single,
        patch.object(
            BrainAdapter, "_get_slot_redis",
            new=AsyncMock(),
        ) as mock_redis_getter,
        # Belt-and-suspenders: if the shortcut leaks, this would explode
        patch.object(adapter, "_request", new=AsyncMock(side_effect=AssertionError(
            "BUG: User-mode simulate_batch must NOT send list payload"
        ))),
    ):
        result = await adapter.simulate_batch(
            ["rank(close)", "ts_mean(close, 5)", "ts_rank(volume, 20)"],
            region="USA", universe="TOP3000",
        )

    # shortcut taken
    mock_single.assert_awaited_once()
    # latch path NEVER touched (no redis interaction in user-mode shortcut)
    mock_redis_getter.assert_not_called()
    assert result == fake_results


@pytest.mark.asyncio
async def test_consultant_mode_simulate_batch_enters_multi_sim_path():
    """Consultant mode: simulate_batch enters multi-sim path (touches latch
    Redis to check for prior 403s)."""
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    adapter = BrainAdapter()

    # Mock redis latch as "latched" so _simulate_via_single is still called,
    # but only AFTER the latch was checked — confirming we went down the
    # multi-sim path (not the §14.2 shortcut, which never reads the latch).
    fake_redis = AsyncMock()
    fake_redis.exists = AsyncMock(return_value=True)   # latch warm → fallback
    fake_redis.set = AsyncMock()

    fake_results = [{"success": True, "alpha_id": "x0"}]

    with (
        patch.object(
            BrainAdapter, "_get_slot_redis",
            new=AsyncMock(return_value=fake_redis),
        ) as mock_redis_getter,
        patch.object(
            adapter, "_simulate_via_single",
            new=AsyncMock(return_value=fake_results),
        ) as mock_single,
    ):
        result = await adapter.simulate_batch(
            ["rank(close)"], region="USA", universe="TOP3000",
        )

    # consultant-mode path checks latch (proof we went past §14.2 shortcut)
    mock_redis_getter.assert_awaited()
    fake_redis.exists.assert_awaited_with("brain:no_multisim")
    # latch warm → fallback to single-sim (existing v27.94 behavior)
    mock_single.assert_awaited_once()
    assert result == fake_results


@pytest.mark.asyncio
async def test_revert_mid_task_immediately_downgrades_multi_sim():
    """Direction C: endpoint-selection capability tracks GLOBAL flag, not
    task snapshot. Switching User→Consultant→User in same process:
      - first call (Consultant): goes through multi-sim path → latch read
      - second call (User after revert): pure shortcut → no latch read"""
    adapter = BrainAdapter()
    fake_results = [{"success": True}]

    fake_redis = AsyncMock()
    fake_redis.exists = AsyncMock(return_value=True)
    fake_redis.set = AsyncMock()

    with (
        patch.object(
            BrainAdapter, "_get_slot_redis",
            new=AsyncMock(return_value=fake_redis),
        ) as mock_redis_getter,
        patch.object(
            adapter, "_simulate_via_single",
            new=AsyncMock(return_value=fake_results),
        ),
    ):
        # Phase 1: Consultant mode — multi-sim path checks latch
        _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
        await adapter.simulate_batch(["x"], region="USA", universe="TOP3000")
        consultant_calls = mock_redis_getter.await_count
        assert consultant_calls >= 1, "Consultant mode should check latch"

        # Phase 2: revert to User — must NOT touch latch
        _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = False
        await adapter.simulate_batch(["y"], region="USA", universe="TOP3000")
        user_calls = mock_redis_getter.await_count

        # User-mode call added ZERO latch reads
        assert user_calls == consultant_calls, (
            f"Revert should bypass latch — but got {user_calls - consultant_calls} "
            f"extra latch read(s) after flag flipped to False"
        )


@pytest.mark.asyncio
async def test_user_mode_passes_through_sim_settings_unchanged():
    """The shortcut must pass region/universe/decay/etc. unchanged to
    _simulate_via_single — wrappers around simulate_batch shouldn't see
    behavioral drift between User and Consultant modes."""
    adapter = BrainAdapter()
    captured_args = {}

    async def _capture(*args, **kwargs):
        captured_args["args"] = args
        captured_args["kwargs"] = kwargs
        return []

    with patch.object(adapter, "_simulate_via_single", new=_capture):
        await adapter.simulate_batch(
            ["expr1"],
            region="CHN", universe="TOP2000A",
            delay=2, decay=8, neutralization="INDUSTRY",
            truncation=0.05, test_period="P1Y0M",
        )

    args = captured_args["args"]
    assert args[0] == ["expr1"]               # expressions
    assert args[1] == "CHN"                   # region
    assert args[2] == "TOP2000A"              # universe
    assert args[3] == 2                       # delay
    assert args[4] == 8                       # decay
    assert args[5] == "INDUSTRY"              # neutralization
    assert args[6] == 0.05                    # truncation
    assert args[7] == "P1Y0M"                 # test_period
