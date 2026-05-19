"""Generic Redis-backed circuit breaker (2026-05-19, A+ pattern).

Purpose
-------
When an external dependency (BRAIN auth, LLM provider, sync HTTP, etc.)
enters a transient failure mode, we want every caller across every worker
to STOP hammering it until either (a) a TTL elapses and we re-probe, or
(b) ops manually clears the circuit. The alternative — every caller
inlining its own retry loop — burns LLM/BRAIN cost and floods logs while
the upstream is down.

This file is the framework. The first client is ``brain_auth``; future
clients (LLM provider 5xx storm, BRAIN /alphas list 429 storm, etc.) can
instantiate ``CircuitBreaker(name=...)`` with their own TTL and reuse the
state machine 0-effort.

State machine
-------------
- CLOSED      — normal operation, callers proceed
- OPEN        — last failure recent (now < until_ts) → callers fast-fail
- HALF_OPEN   — TTL elapsed; first caller's success/failure flips
                CLOSED/OPEN

Cross-worker semantics
----------------------
The state lives in Redis as JSON at key ``circuit:{name}``. Every is_open
query reads Redis (no local cache — circuit transitions need to propagate
within milliseconds, not the 60s flag-cache TTL window). Trip writes are
unconditional (last write wins — a freshly-trip from worker A within ms
of worker B's clear is fine, the underlying failure mode hasn't moved).

Soft-fail
---------
EVERY method swallows Redis exceptions and either returns the safe-default
(is_open → False, "let traffic through") or no-ops (trip → log warning).
A Redis blip MUST NEVER cause a global brown-out by spuriously opening
all circuits.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from loguru import logger


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CircuitStatus:
    """Snapshot of circuit state at a point in time."""
    state: CircuitState
    until_ts: Optional[float]      # epoch seconds when OPEN auto-rolls to HALF_OPEN
    last_failure_at: Optional[float]
    last_failure_reason: Optional[str]
    trip_count: int                # cumulative since last clear (operator audit)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "until_ts": self.until_ts,
            "until_iso": _iso(self.until_ts),
            "last_failure_at": self.last_failure_at,
            "last_failure_iso": _iso(self.last_failure_at),
            "last_failure_reason": self.last_failure_reason,
            "trip_count": self.trip_count,
            "seconds_until_half_open": (
                max(0, int(self.until_ts - time.time()))
                if self.until_ts and self.state == CircuitState.OPEN
                else 0
            ),
        }


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


CLOSED_STATUS = CircuitStatus(
    state=CircuitState.CLOSED,
    until_ts=None,
    last_failure_at=None,
    last_failure_reason=None,
    trip_count=0,
)


class CircuitBreaker:
    """A named circuit, Redis-backed for cross-worker propagation.

    Example::

        BRAIN_AUTH_CIRCUIT = CircuitBreaker("brain_auth", default_ttl_sec=300)

        async def simulate_alpha(...):
            if BRAIN_AUTH_CIRCUIT.is_open():
                return {"success": False, "retryable": True,
                        "error_kind": "brain_auth_circuit_open"}
            try:
                resp = await self._request(...)
                if _is_auth_error(resp):
                    BRAIN_AUTH_CIRCUIT.trip(reason="brain_401_after_retry")
                    return {...retryable...}
            except ...:
                ...
    """

    KEY_PREFIX = "circuit"

    def __init__(self, name: str, *, default_ttl_sec: int = 300):
        if not name or not isinstance(name, str):
            raise ValueError(f"CircuitBreaker name must be a non-empty string, got {name!r}")
        self.name = name
        self.default_ttl_sec = int(default_ttl_sec)

    # ------------------------------------------------------------------
    # Redis key + serialization
    # ------------------------------------------------------------------

    @property
    def _redis_key(self) -> str:
        return f"{self.KEY_PREFIX}:{self.name}"

    def _get_redis(self):
        try:
            from backend.tasks.redis_pool import get_redis_client
            return get_redis_client()
        except Exception as e:
            logger.debug(f"[CircuitBreaker {self.name}] Redis unavailable: {e}")
            return None

    # ------------------------------------------------------------------
    # State read
    # ------------------------------------------------------------------

    def status(self) -> CircuitStatus:
        """Read current status from Redis. Returns CLOSED on any read failure
        (fail-open — Redis blip must not brown out callers)."""
        r = self._get_redis()
        if r is None:
            return CLOSED_STATUS
        try:
            raw = r.get(self._redis_key)
        except Exception as e:
            logger.debug(f"[CircuitBreaker {self.name}] GET failed: {e}")
            return CLOSED_STATUS
        if raw is None:
            return CLOSED_STATUS
        try:
            data = json.loads(raw)
        except Exception:
            return CLOSED_STATUS

        # State machine: if Redis still has OPEN with until_ts in the past,
        # promote to HALF_OPEN on read. We don't write back — the next
        # trip() or clear() will sync.
        until_ts = data.get("until_ts")
        state_str = data.get("state", "closed")
        if state_str == CircuitState.OPEN.value and until_ts is not None:
            try:
                if time.time() >= float(until_ts):
                    state_str = CircuitState.HALF_OPEN.value
            except Exception:
                pass
        try:
            state = CircuitState(state_str)
        except ValueError:
            state = CircuitState.CLOSED
        return CircuitStatus(
            state=state,
            until_ts=until_ts,
            last_failure_at=data.get("last_failure_at"),
            last_failure_reason=data.get("last_failure_reason"),
            trip_count=int(data.get("trip_count", 0) or 0),
        )

    def is_open(self) -> bool:
        """True when callers should fast-fail. HALF_OPEN returns False (one
        probe is allowed); the probe's outcome will trip() or clear()."""
        return self.status().state == CircuitState.OPEN

    # ------------------------------------------------------------------
    # State write
    # ------------------------------------------------------------------

    def trip(self, *, reason: str = "unspecified", ttl_sec: Optional[int] = None) -> None:
        """Open the circuit. TTL defaults to ``default_ttl_sec``. Idempotent
        on repeated calls (TTL refresh + trip_count++). NEVER raises."""
        ttl = int(ttl_sec) if ttl_sec is not None else self.default_ttl_sec
        ttl = max(1, ttl)
        now = time.time()
        until_ts = now + ttl

        r = self._get_redis()
        if r is None:
            logger.warning(
                f"[CircuitBreaker {self.name}] trip() called but Redis "
                f"unavailable — circuit will only be local to this process"
            )
            return

        try:
            prev_raw = r.get(self._redis_key)
            prev_trip_count = 0
            if prev_raw is not None:
                try:
                    prev_trip_count = int(json.loads(prev_raw).get("trip_count", 0) or 0)
                except Exception:
                    prev_trip_count = 0
            new = {
                "state": CircuitState.OPEN.value,
                "until_ts": until_ts,
                "last_failure_at": now,
                "last_failure_reason": str(reason)[:200],
                "trip_count": prev_trip_count + 1,
            }
            # SET with TTL — Redis auto-deletes after ttl, returning callers
            # to CLOSED without our needing a sweeper.
            r.set(self._redis_key, json.dumps(new), ex=ttl)
            logger.warning(
                f"[CircuitBreaker {self.name}] TRIPPED reason={reason!r} "
                f"ttl={ttl}s trip_count={prev_trip_count + 1}"
            )
        except Exception as e:
            logger.warning(f"[CircuitBreaker {self.name}] trip() Redis write failed: {e}")

    def clear(self, *, reason: str = "cleared") -> None:
        """Close the circuit immediately. Called by:
          - Successful authenticate() / health check in client code
          - Ops console (/ops/.../clear endpoint)
        NEVER raises.
        """
        r = self._get_redis()
        if r is None:
            return
        try:
            r.delete(self._redis_key)
            logger.info(f"[CircuitBreaker {self.name}] CLEARED reason={reason!r}")
        except Exception as e:
            logger.warning(f"[CircuitBreaker {self.name}] clear() Redis delete failed: {e}")


__all__ = [
    "CircuitBreaker",
    "CircuitState",
    "CircuitStatus",
    "CLOSED_STATUS",
]
