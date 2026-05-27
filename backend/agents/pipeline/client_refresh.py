"""Coordinated BRAIN-client refresh for the pipeline consumers (Sub-phase 1).

N pipeline consumers share ONE BrainAdapter httpx client. Over a long session
that client's pooled connections rot → sims hang (the d650222 production
failure). The legacy serial loop refreshed every N rounds between rounds (no
sim in flight). The pipeline has no such natural quiescent point, and
``_refresh_brain_client`` calls ``BrainAdapter.close()`` which nulls the global
client — closing it while a consumer is mid-call would crash.

``BrainClientRefresher`` is a drain-and-refresh barrier: a consumer wraps each
BRAIN sim in ``before_sim()`` / ``after_sim()``. After ``refresh_every`` sims
one consumer CLAIMS the refresh, which (a) blocks new sims from starting, (b)
waits for the in-flight sims to drain to zero, (c) refreshes the client, then
(d) unblocks. So the close+recreate only ever runs with zero sims in flight.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class BrainClientRefresher:
    def __init__(
        self,
        *,
        refresh_every: int,
        refresh_fn: Callable[[Any], Awaitable[Any]],
        brain: Any,
    ):
        """
        Args:
            refresh_every: refresh after this many completed sims (<=0 disables;
                callers should then just not use a refresher).
            refresh_fn: async (brain) -> Any. The client refresh (e.g.
                _refresh_brain_client); must be safe to call with zero sims in
                flight and should not raise (best-effort).
            brain: the shared BrainAdapter passed to refresh_fn.
        """
        self._cond = asyncio.Condition()
        self._in_flight = 0
        self._since = 0
        self._refreshing = False
        self.refresh_every = int(refresh_every or 0)
        self.refresh_fn = refresh_fn
        self.brain = brain
        self.refreshes = 0  # telemetry

    async def before_sim(self) -> None:
        async with self._cond:
            # Block while a refresh is in progress (the client may be closed).
            while self._refreshing:
                await self._cond.wait()
            self._in_flight += 1

    async def after_sim(self) -> None:
        claim = False
        async with self._cond:
            self._in_flight -= 1
            self._since += 1
            if (
                self.refresh_every > 0
                and self._since >= self.refresh_every
                and not self._refreshing
            ):
                # Claim the refresh: blocks new before_sim entrants from here on.
                self._refreshing = True
                claim = True
            self._cond.notify_all()
        if not claim:
            return
        # We own the refresh. Wait for the OTHER in-flight sims to finish, then
        # refresh while the gate is closed, then reopen.
        try:
            async with self._cond:
                while self._in_flight > 0:
                    await self._cond.wait()
            await self.refresh_fn(self.brain)
            self.refreshes += 1
            logger.info("[pipeline] BRAIN client refreshed (drain-and-refresh #%d)", self.refreshes)
        except Exception:  # noqa: BLE001 — refresh is best-effort, never fatal
            logger.exception("[pipeline] BRAIN client refresh failed (non-fatal)")
        finally:
            async with self._cond:
                self._refreshing = False
                self._since = 0
                self._cond.notify_all()
