"""opt A/B: CorrelationService per-round PnL fetch cache (2026-05-25).

news12 EVALUATE hit ~10min because calc_self_corr AND calc_self_corr_by_window
each fetched the SAME target alpha's PnL from BRAIN, each with up to 3 retries.
The per-instance cache serves the repeat fetch (incl. an empty/not-yet-ready
result) so the duplicate BRAIN round-trip + retry storm is eliminated.
"""
import pandas as pd
import pytest

import backend.services.correlation_service as cs


@pytest.mark.asyncio
async def test_pnl_fetch_cached_no_second_brain_call(monkeypatch):
    """A repeat fetch of the same alpha within one CorrelationService instance
    (= one round) is served from cache — no second BRAIN call."""
    monkeypatch.setattr(
        cs, "_pnl_records_to_series", lambda payload, aid: pd.Series([1.0, 2.0, 3.0], name=aid)
    )
    calls = {"n": 0}

    class _Brain:
        async def get_alpha_pnl(self, aid):
            calls["n"] += 1
            return {"any": "payload"}

    svc = cs.CorrelationService(_Brain())
    s1 = await svc._fetch_pnl_series("alpha-1")
    s2 = await svc._fetch_pnl_series("alpha-1")  # opt A: cache hit
    assert calls["n"] == 1, "repeat fetch must hit the per-round cache, not BRAIN"
    assert not s1.empty and s1.equals(s2)


@pytest.mark.asyncio
async def test_empty_pnl_cached_short_circuits_retry(monkeypatch):
    """A not-yet-ready (empty) PnL is cached too — the second fetch does NOT
    re-run the retry loop (no additional BRAIN calls). This is opt B's
    complement: a just-simulated alpha's empty PnL isn't retried twice."""
    monkeypatch.setattr(
        cs, "_pnl_records_to_series", lambda payload, aid: pd.Series(dtype="float64", name=aid)
    )
    calls = {"n": 0}

    class _Brain:
        async def get_alpha_pnl(self, aid):
            calls["n"] += 1
            return {}  # empty

    svc = cs.CorrelationService(_Brain())
    s1 = await svc._fetch_pnl_series("alpha-2", max_attempts=2)
    after_first = calls["n"]
    s2 = await svc._fetch_pnl_series("alpha-2")  # cache hit (empty)
    assert s1.empty and s2.empty
    assert calls["n"] == after_first, "cached empty must short-circuit the retry loop"
    assert after_first <= 2, "first fetch should not exceed max_attempts"


@pytest.mark.asyncio
async def test_distinct_alphas_fetched_independently(monkeypatch):
    """Cache is keyed by alpha_id — different alphas each fetch once."""
    monkeypatch.setattr(
        cs, "_pnl_records_to_series", lambda payload, aid: pd.Series(dtype="float64", name=aid)
    )
    seen = []

    class _Brain:
        async def get_alpha_pnl(self, aid):
            seen.append(aid)
            return {}

    svc = cs.CorrelationService(_Brain())
    await svc._fetch_pnl_series("a", max_attempts=1)
    await svc._fetch_pnl_series("b", max_attempts=1)
    await svc._fetch_pnl_series("a", max_attempts=1)  # cache hit, no new fetch
    assert seen == ["a", "b"], f"expected a,b fetched once each; got {seen}"


@pytest.mark.asyncio
async def test_backoff_base_shortened(monkeypatch):
    """opt B: the retry backoff base was lowered from 1.5 to 1.0."""
    assert cs._PNL_RETRY_BACKOFF_BASE == 1.0
