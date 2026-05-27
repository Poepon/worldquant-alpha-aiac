"""Unit tests for BrainClientRefresher — the drain-and-refresh barrier
(Sub-phase 1). Locks down: refresh fires on cadence; refresh runs ONLY with
zero sims in flight; new sims are blocked during a refresh."""

import asyncio

import pytest

from backend.agents.pipeline.client_refresh import BrainClientRefresher


@pytest.mark.asyncio
async def test_refresh_fires_on_cadence():
    calls = []

    async def refresh_fn(_brain):
        calls.append(1)

    r = BrainClientRefresher(refresh_every=3, refresh_fn=refresh_fn, brain=None)
    for _ in range(6):
        await r.before_sim()
        await r.after_sim()
    # Fires at sim #3 and #6.
    assert len(calls) == 2
    assert r.refreshes == 2


@pytest.mark.asyncio
async def test_disabled_when_refresh_every_zero():
    calls = []

    async def refresh_fn(_brain):
        calls.append(1)

    r = BrainClientRefresher(refresh_every=0, refresh_fn=refresh_fn, brain=None)
    for _ in range(10):
        await r.before_sim()
        await r.after_sim()
    assert calls == []


@pytest.mark.asyncio
async def test_refresh_waits_for_in_flight_to_drain():
    """The refresh must NOT run while a sim is in flight (closing the shared
    client mid-call would crash)."""
    started = asyncio.Event()

    async def refresh_fn(_brain):
        started.set()

    r = BrainClientRefresher(refresh_every=2, refresh_fn=refresh_fn, brain=None)

    # Sim A enters and stays in flight.
    await r.before_sim()  # in_flight=1
    # Sim B completes (_since=1, no trigger).
    await r.before_sim()
    await r.after_sim()
    # Sim C enters, then its after_sim claims the refresh (_since reaches 2) and
    # must wait for A to drain — run it as a task so we can finish A.
    await r.before_sim()  # in_flight=2 (A + C)
    c_after = asyncio.create_task(r.after_sim())
    await asyncio.sleep(0.02)
    assert not started.is_set(), "refresh ran while sim A was still in flight"

    # A finishes → in_flight hits 0 → the claimed refresh proceeds.
    await r.after_sim()
    await c_after
    assert started.is_set()
    assert r.refreshes == 1


@pytest.mark.asyncio
async def test_new_sims_blocked_during_refresh():
    release = asyncio.Event()

    async def refresh_fn(_brain):
        await release.wait()  # hold the refresh open

    r = BrainClientRefresher(refresh_every=1, refresh_fn=refresh_fn, brain=None)

    await r.before_sim()
    ref_task = asyncio.create_task(r.after_sim())  # claims + refresh blocks on release
    await asyncio.sleep(0.02)  # refresh now in progress (_refreshing=True)

    entered = asyncio.Event()

    async def new_sim():
        await r.before_sim()
        entered.set()
        await r.after_sim()

    n_task = asyncio.create_task(new_sim())
    await asyncio.sleep(0.02)
    assert not entered.is_set(), "a new sim started during the refresh"

    release.set()  # let the refresh finish
    await ref_task
    await n_task
    assert entered.is_set()


@pytest.mark.asyncio
async def test_refresh_failure_is_non_fatal_and_resets():
    async def boom(_brain):
        raise RuntimeError("refresh down")

    r = BrainClientRefresher(refresh_every=1, refresh_fn=boom, brain=None)
    # Should not raise; _refreshing must reset so subsequent sims aren't blocked.
    await r.before_sim()
    await r.after_sim()
    # Next sim proceeds normally (gate reopened despite the refresh error).
    await r.before_sim()
    await r.after_sim()
    assert r.refreshes == 0  # never counted a success
