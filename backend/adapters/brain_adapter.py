"""
BRAIN Adapter - WorldQuant BRAIN Platform API Integration

Implements BrainProtocol for dependency injection and testability.

Refactored based on ace_lib.py best practices:
- Singleton Session (httpx.AsyncClient)
- Active Token Expiry Checking
- Basic Authentication
- Retry-After Handling
"""

import os
import asyncio
import json
import random
import time
from typing import Dict, List, Optional, Any, Union
from datetime import datetime, timedelta
import httpx
import redis.asyncio as redis
from tenacity import retry, stop_after_attempt, wait_exponential, wait_random_exponential
from loguru import logger
from sqlalchemy import select
import logging

# Suppress httpx interaction logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.models import BrainAuthToken

# Import protocol for type checking (Protocol is runtime_checkable)
from backend.protocols.brain_protocol import BrainProtocol

# A+ circuit breaker (2026-05-19): backend.circuit_breaker is intentionally
# at the top-level (NOT under services/) to avoid the
# adapters→services→mining_service→adapters cycle. When BRAIN auth fails
# persistently, trip this circuit so every BrainAdapter caller fast-fails
# (no LLM cost burnt running a round whose sim can't succeed). TTL=300s
# gives ops a 5min window; after that the circuit auto-HALF_OPENs and the
# next call probes. authenticate() success calls .clear().
from backend.circuit_breaker import CircuitBreaker
BRAIN_AUTH_CIRCUIT = CircuitBreaker("brain_auth", default_ttl_sec=300)

# Singleton Client Storage (Loop-aware)
_GLOBAL_CLIENT: Optional[httpx.AsyncClient] = None
_GLOBAL_CLIENT_LOOP: Optional[asyncio.AbstractEventLoop] = None

class BrainAdapter:
    """
    Adapter for WorldQuant BRAIN platform.
    Uses a singleton AsyncClient for persistent session management within the same event loop.
    
    Credentials priority:
    1. Constructor arguments (explicit)
    2. Database configuration (via CredentialsService)
    3. Environment variables (fallback)
    """
    
    BASE_URL = "https://api.worldquantbrain.com"
    SESSION_BUFFER_SECONDS = 300  # Re-auth if expiring in < 5 mins
    REDIS_SESSION_KEY = "brain_session:cookies"
    
    # Class-level cached credentials (to avoid DB queries on every request)
    _cached_email: Optional[str] = None
    _cached_password: Optional[str] = None
    _credentials_loaded: bool = False

    # Multi-simulation permission gate. Latched after first 403 from
    # POST /simulations (list payload) so subsequent simulate_batch calls go
    # straight to the single-sim fallback. BRAIN exposes multi-simulation only
    # to Consultant+ accounts; lower tiers still have access to single-sim.
    #
    # V-26.25 (2026-05-13): the latch is stamp+TTL'd so a worker restart
    # isn't needed to undo it after a BRAIN account upgrade.
    #
    # V-27.94/118 (2026-05-14): the latch lives in Redis, NOT a class
    # attribute. The old `_no_multisim` / `_no_multisim_at` were per-process
    # — each worker latched independently, so a 403 learned by one worker
    # didn't stop the others and the re-probe fired N×workers times instead
    # of once. The Redis key's TTL IS the re-probe window: key present ⇒
    # latched & warm; key absent (never set OR TTL expired) ⇒ (re-)probe.
    # Aligns with _SLOT_COUNTER_KEY's cross-process Redis design.
    _NO_MULTISIM_KEY: str = "brain:no_multisim"               # TTL latch — warm ⇒ skip probe
    _NO_MULTISIM_EVER_KEY: str = "brain:no_multisim_ever"     # permanent — account was 403'd ≥ once
    _NO_MULTISIM_PROBE_LOCK: str = "brain:no_multisim_probe"  # SET NX — re-probe herd control
    _NO_MULTISIM_REPROBE_SEC: float = 24 * 3600.0  # 24h — also the _NO_MULTISIM_KEY TTL
    _NO_MULTISIM_PROBE_LOCK_TTL: int = 120  # re-probe coordination window (s)
    _SINGLE_SIM_FALLBACK_CONCURRENCY: int = 3

    # BRAIN server-side hard limit on in-flight sims per account, *across all
    # processes*. Per-process asyncio.Semaphore is insufficient when multiple
    # celery workers run on the same account — each had its own counter and the
    # account would overflow with N_workers × limit in-flight, triggering 429
    # CONCURRENT_SIMULATION_LIMIT_EXCEEDED. The slot is held from POST
    # /simulations through to terminal status, mirroring BRAIN's accounting.
    # Limit is role-aware (USER=3, CONSULTANT=80) — read at acquire-time via
    # _current_sim_slot_limit() so ENABLE_BRAIN_CONSULTANT_MODE flips take
    # effect immediately (settings.__getattribute__ hooks FeatureFlagOverride).
    _SLOT_COUNTER_KEY: str = "brain:concurrent_sims"
    # 2026-05-08 fix: shorten TTL from 30 min to 10 min so that orphaned slots
    # (worker force-killed mid-simulate, _release_sim_slot never reached) clear
    # within ~10 min idle instead of starving the pool for half an hour. TTL
    # is refreshed on every successful acquire so live workers never lose their
    # slot — cleanup only triggers when ALL slots leaked AND no new acquires.
    _SLOT_TTL_SEC: int = 600   # safety: orphaned counter resets after 10 min
    _SLOT_POLL_INTERVAL: float = 1.5
    _SLOT_ACQUIRE_TIMEOUT: float = 1800.0   # 30 min upper bound on wait
    _redis_client: Optional["redis.Redis"] = None
    # Bug fix (2026-05-01): redis.from_url binds to whatever event loop is
    # current when called. tasks/__init__.run_async creates a new loop per
    # Celery task and closes it on exit, so the cached _redis_client outlives
    # its loop and the next task hits "Event loop is closed". We track which
    # loop the client was created on; if it differs from the current one, we
    # rebuild.
    _redis_client_loop_id: Optional[int] = None

    @classmethod
    async def _get_slot_redis(cls):
        # Separate cached connection from the per-instance _get_redis (used for
        # session cookies) so we don't tear it down between sims.
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        current_loop_id = id(current_loop) if current_loop is not None else None
        loop_changed = (
            cls._redis_client is not None
            and cls._redis_client_loop_id is not None
            and current_loop_id is not None
            and current_loop_id != cls._redis_client_loop_id
        )
        loop_dead = (
            cls._redis_client is not None
            and current_loop is not None
            and current_loop.is_closed()
        )
        if loop_changed or loop_dead:
            # The old client's transport is bound to a closed/different loop.
            # Don't try to await disconnect across loops — just drop the ref;
            # GC + the OS will reclaim the socket. This is the standard
            # "asyncio singleton across loops" pattern.
            cls._redis_client = None
            cls._redis_client_loop_id = None

        if cls._redis_client is None:
            cls._redis_client = redis.from_url(
                settings.REDIS_URL, decode_responses=True
            )
            cls._redis_client_loop_id = current_loop_id
        return cls._redis_client

    @classmethod
    def _current_sim_slot_limit(cls) -> int:
        """Return the in-flight slot ceiling for the current BRAIN role.

        Reads ENABLE_BRAIN_CONSULTANT_MODE via settings each call so an ops
        flip (FeatureFlagOverride) takes effect on the very next acquire —
        no restart. Falls back to the USER ceiling on any error so a Redis
        / settings hiccup degrades safely toward the more restrictive bound.
        """
        try:
            if getattr(settings, "ENABLE_BRAIN_CONSULTANT_MODE", False):
                return int(getattr(settings, "BRAIN_SIM_SLOT_LIMIT_CONSULTANT", 80))
            return int(getattr(settings, "BRAIN_SIM_SLOT_LIMIT_USER", 3))
        except Exception:
            return 3

    @classmethod
    async def _acquire_sim_slot(cls) -> bool:
        """Atomically acquire one of the role-aware concurrent sim slots.

        Returns True when a slot is held; loops with sleep until acquired or
        deadline. The TTL on the counter prevents a dead worker from starving
        the pool forever. The ceiling is resolved per acquire so a USER→
        CONSULTANT flip mid-flight expands capacity for the next waiter
        instead of blocking on the old 3-slot bound.
        """
        r = await cls._get_slot_redis()
        deadline = asyncio.get_event_loop().time() + cls._SLOT_ACQUIRE_TIMEOUT
        warned = False
        while True:
            limit = cls._current_sim_slot_limit()
            count = await r.incr(cls._SLOT_COUNTER_KEY)
            if count <= limit:
                # Refresh expiry as a safety net
                await r.expire(cls._SLOT_COUNTER_KEY, cls._SLOT_TTL_SEC)
                return True
            # Over capacity — release and back off
            await r.decr(cls._SLOT_COUNTER_KEY)
            if not warned:
                logger.info(
                    f"[BrainAdapter] BRAIN sim slot full ({count-1}/{limit}); waiting"
                )
                warned = True
            if asyncio.get_event_loop().time() > deadline:
                logger.error("[BrainAdapter] BRAIN sim slot acquire timed out (30 min)")
                return False
            await asyncio.sleep(cls._SLOT_POLL_INTERVAL)

    @classmethod
    async def _release_sim_slot(cls) -> None:
        try:
            r = await cls._get_slot_redis()
            n = await r.decr(cls._SLOT_COUNTER_KEY)
            if n < 0:
                # Recover from bad state (e.g. counter manually cleared)
                await r.set(cls._SLOT_COUNTER_KEY, 0)
        except Exception as e:
            logger.warning(f"[BrainAdapter] release sim slot failed (non-fatal): {e}")

    # ---- Cross-process rate-limit cooldown -----------------------------------
    # Each endpoint shares a Redis-backed cooldown so that when one caller is
    # rate-limited, every other caller (across processes) pauses before its
    # next request. A separate "strike counter" decays after a quiet window so
    # the backoff floor grows under sustained pressure even when each call
    # individually retries-and-succeeds (which would otherwise reset its local
    # `retries` counter to 0 every invocation).
    _RL_COOLDOWN_PREFIX: str = "brain:rl_cooldown"
    _RL_STRIKE_TTL_SEC: int = 60
    _RL_BACKOFF_CAP_SEC: float = 60.0

    @classmethod
    async def _rl_remaining(cls, endpoint: str) -> float:
        try:
            r = await cls._get_slot_redis()
            ms = await r.pttl(f"{cls._RL_COOLDOWN_PREFIX}:{endpoint}")
            return ms / 1000.0 if ms and ms > 0 else 0.0
        except Exception:
            return 0.0

    @classmethod
    async def _rl_set_cooldown(cls, endpoint: str, seconds: float) -> None:
        try:
            r = await cls._get_slot_redis()
            await r.set(
                f"{cls._RL_COOLDOWN_PREFIX}:{endpoint}",
                "1",
                px=max(1, int(seconds * 1000)),
            )
        except Exception:
            pass

    @classmethod
    async def _rl_record_strike(cls, endpoint: str) -> int:
        try:
            r = await cls._get_slot_redis()
            key = f"{cls._RL_COOLDOWN_PREFIX}:{endpoint}:strikes"
            n = await r.incr(key)
            await r.expire(key, cls._RL_STRIKE_TTL_SEC)
            return int(n)
        except Exception:
            return 1

    # Baseline inter-request gap — applied even at strikes=0 so paginated
    # bursts don't trigger the first 429 in the first place. Each 429 then
    # multiplies this gap on top.
    _RL_BASELINE_GAP_SEC: float = 0.3
    _RL_PACE_CAP_SEC: float = 16.0

    @classmethod
    async def _rl_pace(cls, endpoint: str) -> None:
        """Proactive rate-limit pacing: ensure a minimum inter-request gap on
        this endpoint to avoid bursting into 429.

        - strikes == 0: baseline gap (~0.3s) — keeps paginated reads safe.
        - strikes >= 1: gap doubles per strike (1, 2, 4, 8, 16s) capped at 16s.
        - strikes counter naturally TTLs out after 60s of quiet — pacing relaxes
          back to baseline automatically.

        Cross-process via Redis so all callers cooperate on the same endpoint.
        """
        try:
            r = await cls._get_slot_redis()
            strikes_raw = await r.get(f"{cls._RL_COOLDOWN_PREFIX}:{endpoint}:strikes")
            strikes = int(strikes_raw) if strikes_raw else 0
            if strikes <= 0:
                min_gap = cls._RL_BASELINE_GAP_SEC
            else:
                min_gap = float(min(2 ** min(strikes - 1, 4), cls._RL_PACE_CAP_SEC))
            last_key = f"{cls._RL_COOLDOWN_PREFIX}:{endpoint}:last_req_at"
            last_raw = await r.get(last_key)
            now = time.time()
            if last_raw:
                elapsed = now - float(last_raw)
                if elapsed < min_gap:
                    await asyncio.sleep(min_gap - elapsed)
            await r.set(last_key, str(time.time()), ex=120)
        except Exception:
            pass

    def __init__(self, email: str = None, password: str = None):
        # Store explicit credentials if provided
        self._explicit_email = email
        self._explicit_password = password
        
        # Initialize with explicit or env fallback (DB credentials loaded async)
        self.email = email or settings.BRAIN_EMAIL
        self.password = password or settings.BRAIN_PASSWORD
        self.session_token = None
    
    async def _load_credentials_from_db(self) -> bool:
        """
        Load credentials from database if not already loaded.
        Returns True if credentials were loaded/updated.
        """
        # Skip if explicit credentials were provided in constructor
        if self._explicit_email and self._explicit_password:
            return False
        
        # Skip if already loaded
        if BrainAdapter._credentials_loaded:
            if BrainAdapter._cached_email:
                self.email = BrainAdapter._cached_email
            if BrainAdapter._cached_password:
                self.password = BrainAdapter._cached_password
            return bool(BrainAdapter._cached_email)
        
        try:
            from backend.services.credentials_service import (
                CredentialsService, 
                CredentialKey
            )
            
            async with AsyncSessionLocal() as db:
                service = CredentialsService(db)
                
                # Load email
                db_email = await service.get_credential(
                    CredentialKey.BRAIN_EMAIL,
                    fallback_env="BRAIN_EMAIL"
                )
                if db_email:
                    BrainAdapter._cached_email = db_email
                    self.email = db_email
                
                # Load password
                db_password = await service.get_credential(
                    CredentialKey.BRAIN_PASSWORD,
                    fallback_env="BRAIN_PASSWORD"
                )
                if db_password:
                    BrainAdapter._cached_password = db_password
                    self.password = db_password
                
                BrainAdapter._credentials_loaded = True
                
                if db_email or db_password:
                    logger.info("Loaded Brain credentials from database")
                    return True
                
        except Exception as e:
            logger.warning(f"Failed to load credentials from DB: {e}")
        
        return False
    
    @classmethod
    def invalidate_credentials_cache(cls):
        """Invalidate cached credentials (call after updating credentials)."""
        cls._cached_email = None
        cls._cached_password = None
        cls._credentials_loaded = False
        logger.info("Brain credentials cache invalidated")
    
    @classmethod
    async def get_client(cls) -> httpx.AsyncClient:
        """Get or create the global singleton client for the current event loop."""
        global _GLOBAL_CLIENT, _GLOBAL_CLIENT_LOOP
        
        current_loop = asyncio.get_running_loop()
        
        # If client exists but loop doesn't match (or loop closed), reset it
        if _GLOBAL_CLIENT:
            if _GLOBAL_CLIENT.is_closed or _GLOBAL_CLIENT_LOOP != current_loop:
                logger.debug("Event loop changed or client closed, resetting BrainAdapter client")
                # Try to close old one if loop still open (unlikely if loop changed) 
                # but we can't await on old loop easily. Just drop ref.
                _GLOBAL_CLIENT = None
                _GLOBAL_CLIENT_LOOP = None

        if _GLOBAL_CLIENT is None:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Origin": "https://platform.worldquantbrain.com",
                "Referer": "https://platform.worldquantbrain.com/",
                "Accept": "application/json;version=2.0"
            }
            _GLOBAL_CLIENT = httpx.AsyncClient(
                timeout=60.0, 
                headers=headers,
                follow_redirects=True
            )
            _GLOBAL_CLIENT_LOOP = current_loop
            
        return _GLOBAL_CLIENT

    async def __aenter__(self):
        self.client = await self.get_client()
        await self.ensure_session()
        return self

    async def __aexit__(self, *args):
        # Do not close the global client here; it persists.
        pass
    
    @classmethod
    async def close(cls):
        """Explicitly close the global client (app shutdown)."""
        global _GLOBAL_CLIENT
        if _GLOBAL_CLIENT:
            await _GLOBAL_CLIENT.aclose()
            _GLOBAL_CLIENT = None

    async def _get_redis(self):
        """Get redis connection"""
        return redis.from_url(settings.REDIS_URL, decode_responses=True)

    async def _load_session_from_redis(self) -> bool:
        """Load cookies from Redis if they exist."""
        try:
            r = await self._get_redis()
            cookies_json = await r.get(self.REDIS_SESSION_KEY)
            await r.aclose()
            
            if cookies_json:
                cookies = json.loads(cookies_json)
                self.client.cookies.update(cookies)
                logger.debug("Loaded session cookies from Redis")
                # When loaded from Redis, we trust it aligns with expiry.
                return True
            return False
        except Exception as e:
            logger.warning(f"Failed to load session from Redis: {e}")
            return False

    async def _invalidate_session_cache(self) -> None:
        """V-26.24 (2026-05-13): explicitly drop the Redis session cache.

        Pre-fix the auth-failure handler in `_request` re-authenticated
        and overwrote the Redis cache on success — but if re-auth itself
        failed (BRAIN auth-endpoint down, credential rotation, etc.) the
        stale cookies stayed in Redis and the *next* `ensure_session()`
        cheerfully loaded them again. By deleting the key on first 401
        we guarantee no other worker (or this worker's next call)
        recovers the dead session by accident.
        """
        try:
            r = await self._get_redis()
            await r.delete(self.REDIS_SESSION_KEY)
            await r.aclose()
            logger.info("[BrainAdapter] V-26.24 Redis session cache invalidated")
        except Exception as e:
            logger.warning(f"[BrainAdapter] V-26.24 cache invalidate failed: {e}")

    async def _save_session_to_redis(self, expiry_seconds: int):
        """Save current cookies to Redis with TTL."""
        try:
            cookies = dict(self.client.cookies)
            if not cookies:
                return
                
            r = await self._get_redis()
            # Set TTL slightly less than actual expiry to be safe (e.g. 5 min buffer logic already in caller or here)
            # If expiry_seconds is "seconds remaining", we use it as TTL directly.
            # If it's a timestamp, we calculate diff? 
            # Brain API returns "expiry": 14400 (seconds remaining). So use directly.
            ttl = max(60, int(expiry_seconds) - 60) # Reduce by 1 min to be safe
            await r.set(self.REDIS_SESSION_KEY, json.dumps(cookies), ex=ttl)
            await r.aclose()
            logger.debug(f"Saved session to Redis (TTL: {ttl}s)")
        except Exception as e:
            logger.error(f"Failed to save session to Redis: {e}")

    async def ensure_session(self, force_refresh: bool = False):
        """Ensure valid session exists, refreshing if needed. Prefer Redis cache.

        V-26.2 (2026-05-13): `force_refresh=True` skips the Redis-cache
        short-circuit and re-validates against /authentication. Used at
        cascade phase boundaries because the Redis cache TTL can outlive
        the actual BRAIN token's `expiry` field — a fresh cache hit can
        still be 30 minutes from expiry. Forcing the validity check at
        T1→T2→T3 boundaries (each phase potentially 1-2h) prevents the
        next phase from opening BRAIN sims with a soon-to-expire token.
        """
        # 0. Load credentials from DB if not already loaded
        await self._load_credentials_from_db()

        # 1. Try to load from Redis first (skip when force_refresh)
        if not force_refresh and await self._load_session_from_redis():
            # If loaded from Redis, we assume it is valid for now (TTL handles expiry)
            # We could do a lightweight check, but to save requests, we trust Redis.
            return

        # 2. Check active client state. force_refresh path still consults
        # _is_session_valid so a fresh token from another worker is reused
        # if it's healthy — we only re-auth when actually invalid.
        if not await self._is_session_valid():
            logger.info(
                f"Session invalid or expiring "
                f"(force_refresh={force_refresh}), re-authenticating..."
            )
            await self.authenticate()

    async def _is_session_valid(self) -> bool:
        """
        Check if current session is valid by querying API.
        Reference: ace_lib.py `check_session_timeout`
        """
        try:
            # We need to use the client directly to check
            response = await self.client.get(f"{self.BASE_URL}/authentication")
            
            if response.status_code == 200:
                data = response.json()
                expiry = data.get("token", {}).get("expiry", 0)
                logger.debug(f"Session check: expiry={expiry}, buffer={self.SESSION_BUFFER_SECONDS}")
                # expiry is seconds remaining
                if expiry > self.SESSION_BUFFER_SECONDS:
                    return True
                else:
                    logger.debug(f"Session expiring soon: {expiry}s remaining")
                    return False
            return False
        except Exception:
            return False

    # V-26.74 (2026-05-13): added random jitter to the exponential backoff.
    # Multiple celery workers expire their tokens at roughly the same wall
    # clock (Redis TTL is shared, BRAIN's token expiry is shared) — without
    # jitter all workers re-authenticate within the same exponential window
    # and hammer /authentication, increasing the chance BRAIN rate-limits
    # the auth endpoint itself. wait_random_exponential adds a uniform
    # multiplier on each retry so the worker pool desynchronizes.
    @retry(stop=stop_after_attempt(5), wait=wait_random_exponential(multiplier=2, min=4, max=60))
    async def authenticate(self) -> bool:
        """
        Authenticate using Basic Auth.
        Reference: ace_lib.py `start_session` uses Basic Auth (via requests.auth).
        """
        try:
            response = await self.client.post(
                f"{self.BASE_URL}/authentication",
                auth=(self.email, self.password)
            )
            
            if response.status_code == 201:
                logger.info("BRAIN authentication successful")

                # Save session to Redis
                data = response.json()
                expiry = data.get("token", {}).get("expiry", 3600*4) # Default 4h if missing
                await self._save_session_to_redis(expiry)

                # A+ circuit breaker: a successful authenticate() is the
                # canonical recovery signal — clear the circuit so callers
                # resume sim/poll/pnl immediately (even if the TTL hasn't
                # elapsed). Safe no-op when circuit is already CLOSED.
                try:
                    BRAIN_AUTH_CIRCUIT.clear(reason="authenticate_success")
                except Exception:
                    pass

                return True
            elif response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                     logger.warning(f"Rate limited. Sleeping {retry_after}s")
                     await asyncio.sleep(float(retry_after))
                raise Exception("Rate limit exceeded")
            else:
                logger.error(f"Auth failed: {response.status_code} - {response.text}")
                raise Exception(f"Auth failed: {response.status_code}")
                
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            raise

    # ... Methods (simulate_alpha, get_datasets, etc.) need to use self.client ...
    # I will replicate them below, ensuring they use self.client and handle errors.
    
    async def simulate_alpha(self, expression: str, region: str = "USA", universe: str = "TOP3000", delay: int = 1, decay: int = 4, neutralization: str = "SUBINDUSTRY", truncation: float = 0.08, test_period: str = "P2Y0M") -> Dict:
        # A+ circuit breaker (2026-05-19): fast-fail when BRAIN auth is in
        # known-bad state. Saves us from burning a sim slot acquire + 3 retry
        # attempts + a 401 response just to discover what the LAST sim already
        # told us. Caller's `retryable=True` path holds the alpha at PENDING
        # (V-27.61) and the round-entry check in mining_tasks._run_one_round_inline
        # short-circuits the whole LangGraph workflow so no LLM cost is burnt.
        if BRAIN_AUTH_CIRCUIT.is_open():
            status = BRAIN_AUTH_CIRCUIT.status()
            return {
                "success": False,
                "error": (
                    f"BRAIN auth circuit OPEN — fast-fail (reason="
                    f"{status.last_failure_reason!r}, "
                    f"reopens_in={max(0, int((status.until_ts or 0) - time.time()))}s)"
                ),
                "retryable": True,
                "retry_after_sec": max(30, int((status.until_ts or time.time() + 60) - time.time())),
                "error_kind": "brain_auth_circuit_open",
            }

        # Construct payload
        sim_payload = {
            "type": "REGULAR",
            "settings": {
                "instrumentType": "EQUITY", "region": region, "universe": universe, "delay": delay,
                "decay": decay, "neutralization": neutralization, "truncation": truncation,
                "testPeriod": test_period, "nanHandling": "OFF", "unitHandling": "VERIFY", "pasteurization": "ON",
                "language": "FASTEXPR", "visualization": False
            },
            "regular": expression
        }

        # Acquire one of BRAIN's 3 server-side concurrent sim slots (cross-process
        # via Redis). Held until terminal status to mirror BRAIN's accounting.
        slot_held = await BrainAdapter._acquire_sim_slot()
        if not slot_held:
            # V-26.73 (2026-05-13): mark slot timeouts as retryable so the
            # caller can re-queue instead of writing the alpha off as
            # permanently failed. 30s default backoff matches the typical
            # BRAIN sim duration so the slot has a realistic chance of being
            # free on retry.
            return {
                "success": False,
                "error": "BRAIN sim slot acquire timeout",
                "retryable": True,
                "retry_after_sec": 30,
            }

        try:
            try:
                response = await self._request("POST", f"{self.BASE_URL}/simulations", json=sim_payload)
                # V-26.75 (2026-05-13): 429 used to be a one-shot fail —
                # the alpha was abandoned and the caller had no signal to
                # retry. Now we return a 'retryable' marker so the caller
                # (node_simulate / simulate_batch) can enqueue the alpha
                # back instead of writing it off as permanently failed.
                if response.status_code == 429 and "CONCURRENT_SIMULATION_LIMIT_EXCEEDED" in (response.text or ""):
                    logger.warning(
                        "[BrainAdapter] 429 CONCURRENT_SIMULATION_LIMIT_EXCEEDED despite slot held; "
                        "Redis counter may be stale. Releasing and signalling retryable failure."
                    )
                    return {
                        "success": False,
                        "error": "BRAIN concurrent limit exceeded",
                        "retryable": True,
                        "retry_after_sec": 30,
                    }
                if response.status_code == 429:
                    # Non-concurrent 429 (rate limit on the endpoint itself).
                    # Respect Retry-After if BRAIN sent it; otherwise short
                    # default. Same retryable signal so caller can backoff.
                    retry_after = response.headers.get("Retry-After")
                    try:
                        retry_after_sec = float(retry_after) if retry_after else 10.0
                    except (TypeError, ValueError):
                        retry_after_sec = 10.0
                    logger.warning(
                        f"[BrainAdapter] V-26.75 BRAIN sim 429 rate-limit; "
                        f"retry_after={retry_after_sec}s"
                    )
                    return {
                        "success": False,
                        "error": f"BRAIN 429 rate limit",
                        "retryable": True,
                        "retry_after_sec": retry_after_sec,
                    }
                if response.status_code not in [200, 201, 202]:
                    # 2026-05-19 silent-burn fix: _request already attempted
                    # _coalesced_reauth + 2x retry above. If the response is
                    # STILL an auth error (401 or "Incorrect authentication
                    # credentials" body marker), this is a transient BRAIN
                    # session / account issue, NOT a per-alpha quality fail.
                    # The pre-fix path returned ordinary {"success": False,
                    # "error": "Creation failed: ..."} which made the caller
                    # node_simulate write the alpha to alpha_failures, then
                    # workflow continued through evaluate / hypothesis_feedback
                    # / round_summary burning LLM cost — observed 121× /24h
                    # "Incorrect authentication credentials" rows while other
                    # workflow stages reported SUCCESS. Mark retryable +
                    # error_kind so node_simulate (V-27.61) holds the alpha
                    # at PENDING (not alpha_failures), tally classifies as
                    # transient, and the round can soft-abort instead of
                    # burning the rest of the LLM pipeline.
                    if self._is_auth_error(response):
                        logger.error(
                            f"[BrainAdapter] simulate POST persists auth-error "
                            f"after _request reauth+2x-retry — task likely "
                            f"hit BRAIN session expiry or account lock. "
                            f"Tripping BRAIN_AUTH_CIRCUIT to prevent silent-burn. "
                            f"Status={response.status_code} body={response.text[:200]}"
                        )
                        # A+ circuit breaker: trip so subsequent calls fast-fail
                        # without going through reauth+retry again. authenticate()
                        # success (manual ops trigger or natural retry after TTL)
                        # will clear the circuit.
                        try:
                            BRAIN_AUTH_CIRCUIT.trip(
                                reason=f"simulate_post_auth_fail_{response.status_code}",
                                ttl_sec=300,
                            )
                        except Exception:
                            pass
                        return {
                            "success": False,
                            "error": f"BRAIN auth failure: {response.text[:200]}",
                            "retryable": True,
                            "retry_after_sec": 300,
                            "error_kind": "brain_auth_failure",
                        }
                    logger.error(f"Brain Simulation Failed [{response.status_code}] | Payload: {json.dumps(sim_payload)} | Response: {response.text}")
                    return {"success": False, "error": f"Creation failed: {response.text}"}

                location = response.headers.get("Location")
                if not location:
                     location = f"/simulations/{response.json().get('id')}"

                return await self._wait_for_simulation(location)
            except Exception as e:
                logger.error(f"Simulate error: {e}")
                return {"success": False, "error": str(e)}
        finally:
            await BrainAdapter._release_sim_slot()

    async def simulate_batch(self, expressions: List[str], region: str = "USA", universe: str = "TOP3000", delay: int = 1, decay: int = 4, neutralization: str = "SUBINDUSTRY", truncation: float = 0.08, test_period: str = "P2Y0M") -> List[Dict]:
        """
        Simulate multiple alphas in a single batch request (Multi-Simulation).
        Returns a list of results in the same order as expressions.

        Falls back to bounded-concurrency single simulations when the account
        lacks Consultant-level multi-simulation permission (BRAIN returns 403).
        """
        # P3-Brain §14.2:严格能力隔离 — User 模式不主动调 Consultant multi-sim
        # endpoint。现有 brain:no_multisim Redis TTL latch + 403 fallback 仍保留
        # 作为防御纵深(Consultant 突然被 BRAIN 撤权时),但默认路径应用层主动
        # 隔离,不让 User 状态下的 POST /simulations list payload 抵达 BRAIN。
        #
        # 注:此处违反"adapter 不感知业务 mode"分层 — 工程取舍。理由:
        #   (a) 1 处入口 shortcut < 改 3 个调用方 (evaluation:1061/1101 + mining_service:145)
        #   (b) brain_adapter.py:31 已 import settings(连接参数),非首例
        #   (c) legacy mining_service.py:145 路径无 state 上下文,无法走 task
        #       快照路线 — 入口 shortcut 是唯一干净选择
        # 未来若加第二个 endpoint-level mode 字段,应考虑提升到 protocols 层。
        if not settings.ENABLE_BRAIN_CONSULTANT_MODE:
            return await self._simulate_via_single(
                expressions, region, universe, delay, decay,
                neutralization, truncation, test_period,
            )

        # V-27.94/118: cross-process multi-sim latch via Redis. Key present
        # ⇒ account known to lack multi-sim permission, latch still warm
        # (TTL = re-probe window) → straight to single-sim fallback. Key
        # absent ⇒ never latched OR TTL expired → fall through and
        # (re-)probe multi-sim; a 403 re-latches the key below. Redis errors
        # are non-fatal — on failure we fall through and probe (safe default).
        try:
            _slot_redis = await self._get_slot_redis()
            _latched = await _slot_redis.exists(self._NO_MULTISIM_KEY)
        except Exception as _redis_e:
            logger.warning(f"[BrainAdapter] multi-sim latch read failed: {_redis_e}")
            _slot_redis = None
            _latched = False
        if _latched:
            return await self._simulate_via_single(
                expressions, region, universe, delay, decay,
                neutralization, truncation, test_period,
            )

        # V-27.94: latch absent ⇒ either NEVER latched (normal account —
        # multi-sim freely, no coordination) OR latch TTL expired (re-probe
        # window). Only the re-probe case needs herd control, gated on the
        # permanent _NO_MULTISIM_EVER_KEY so a normal account is never
        # throttled. In re-probe: one worker wins SET NX and actually probes;
        # the rest fall back to single-sim and pick up the refreshed latch
        # (or its absence) on the next call.
        if _slot_redis is not None:
            try:
                _ever = await _slot_redis.exists(self._NO_MULTISIM_EVER_KEY)
            except Exception:
                _ever = False
            if _ever:
                try:
                    _got_probe = bool(await _slot_redis.set(
                        self._NO_MULTISIM_PROBE_LOCK, "1",
                        nx=True, ex=self._NO_MULTISIM_PROBE_LOCK_TTL,
                    ))
                except Exception:
                    _got_probe = True  # redis hiccup → don't block the probe
                if not _got_probe:
                    return await self._simulate_via_single(
                        expressions, region, universe, delay, decay,
                        neutralization, truncation, test_period,
                    )

        # Construct payload list
        sim_payloads = []
        for expr in expressions:
            sim_payloads.append({
                "type": "REGULAR",
                "settings": {
                    "instrumentType": "EQUITY", "region": region, "universe": universe, "delay": delay,
                    "decay": decay, "neutralization": neutralization, "truncation": truncation,
                    "testPeriod": test_period, "nanHandling": "OFF", "unitHandling": "VERIFY", "pasteurization": "ON",
                    "language": "FASTEXPR", "visualization": False
                },
                "regular": expr
            })

        try:
            # POST list of configs
            response = await self._request("POST", f"{self.BASE_URL}/simulations", json=sim_payloads)

            # Account is not Consultant-level — multi-sim is blocked. Latch the
            # gate so future calls skip the probe, and fall back to single-sim.
            if response.status_code == 403:
                # V-27.94/118: latch in Redis with TTL = re-probe window
                # (cross-process; replaces the per-process class attr). Key
                # auto-expires after _NO_MULTISIM_REPROBE_SEC → next call
                # naturally re-probes.
                if _slot_redis is not None:
                    try:
                        await _slot_redis.set(
                            self._NO_MULTISIM_KEY, str(time.time()),
                            ex=int(self._NO_MULTISIM_REPROBE_SEC),
                        )
                        # V-27.94: permanent marker — this account has been
                        # 403'd at least once, so future re-probes (after the
                        # TTL latch expires) go through SET NX herd control.
                        await _slot_redis.set(self._NO_MULTISIM_EVER_KEY, "1")
                    except Exception as _redis_e:
                        logger.warning(
                            f"[BrainAdapter] multi-sim latch write failed: {_redis_e}"
                        )
                logger.warning(
                    f"Multi-simulation denied (403); switching to single-sim fallback "
                    f"(concurrency={self._SINGLE_SIM_FALLBACK_CONCURRENCY}). "
                    f"V-26.25 will re-probe in "
                    f"{self._NO_MULTISIM_REPROBE_SEC/3600:.0f}h. "
                    f"Body: {response.text[:200]}"
                )
                return await self._simulate_via_single(
                    expressions, region, universe, delay, decay,
                    neutralization, truncation, test_period,
                )

            # V-27.94: multi-sim probe did NOT 403 — the account has the
            # permission (possibly an upgrade since the last latch). Clear
            # the permanent "ever" marker so future calls stop entering the
            # re-probe herd-control path.
            if _slot_redis is not None:
                try:
                    await _slot_redis.delete(self._NO_MULTISIM_EVER_KEY)
                except Exception:
                    pass

            if response.status_code not in [200, 201, 202]:
                logger.error(f"Batch Simulation Failed [{response.status_code}] | Response: {response.text}")
                # Return failures for all
                return [{"success": False, "error": f"Batch creation failed: {response.text}"} for _ in expressions]

            location = response.headers.get("Location")
            if not location:
                # If no location header, check body (unlikely for multi-sim)
                return [{"success": False, "error": "No location header"} for _ in expressions]

            # Wait for parent simulation
            parent_result = await self._wait_for_multisim(location)

            if not parent_result["success"]:
                return [{"success": False, "error": parent_result.get("error")} for _ in expressions]

            # Map results back to order is tricky if Brain doesn't guarantee order,
            # but usually 'children' list order might allow correlation if we trust it?
            # Better: match by alpha ID if possible?
            # Actually ace_lib iterates children and fetches results.

            return parent_result["results"]

        except Exception as e:
            logger.error(f"Batch Simulate error: {e}")
            return [{"success": False, "error": str(e)} for _ in expressions]

    async def _simulate_via_single(
        self,
        expressions: List[str],
        region: str,
        universe: str,
        delay: int,
        decay: int,
        neutralization: str,
        truncation: float,
        test_period: str,
    ) -> List[Dict]:
        """Run single-sim per expression, bounded concurrency. Used when the
        account can't do multi-simulation. Result shape matches simulate_batch
        (each entry is what _get_completed_alpha_details / _wait_for_simulation
        would return)."""
        sem = asyncio.Semaphore(self._SINGLE_SIM_FALLBACK_CONCURRENCY)

        async def run_one(expr: str) -> Dict:
            async with sem:
                try:
                    return await self.simulate_alpha(
                        expression=expr,
                        region=region,
                        universe=universe,
                        delay=delay,
                        decay=decay,
                        neutralization=neutralization,
                        truncation=truncation,
                        test_period=test_period,
                    )
                except Exception as e:
                    logger.error(f"Single-sim fallback error for {expr[:80]!r}: {e}")
                    return {"success": False, "error": str(e)}

        return await asyncio.gather(*(run_one(e) for e in expressions))

    async def _wait_for_multisim(self, location: str, max_wait: int = 900) -> Dict:
        """
        Poll for multi-simulation completion.
        Reference: ace_lib.py `multisimulation_progress` function.
        Key insight: Use Retry-After header presence to determine if still running.
        """
        # Determine full URL
        if location.startswith("http"):
            poll_url = location
        else:
            poll_url = f"{self.BASE_URL}{location}"
        
        error_flag = False
        retry_count = 0
        max_retries = 3
        
        while True:
            try:
                response = await self._request("GET", poll_url)

                # Handle non-2xx with retry
                if response.status_code // 100 != 2:
                    logger.error(f"Multi-sim poll {poll_url}, Status: {response.status_code}, Retry")
                    await asyncio.sleep(30)
                    retry_count += 1
                    if retry_count <= max_retries:
                        continue
                    else:
                        error_flag = True
                        break
                
                # Key check: If Retry-After header is missing or 0, simulation is complete
                retry_after = response.headers.get("Retry-After") or response.headers.get("retry-after")
                
                if not retry_after or retry_after == "0":
                    # Simulation completed - check for error status
                    data = response.json()
                    if data.get("status", "ERROR") == "ERROR":
                        error_flag = True
                        logger.error(f"Multi-simulation error: {data}")
                    break
                
                # Still running, wait as instructed
                await asyncio.sleep(float(retry_after))
                
            except Exception as e:
                import traceback
                logger.error(f"Multi-sim poll error: {traceback.format_exc()}")
                await asyncio.sleep(3)
                retry_count += 1
                if retry_count > max_retries:
                    return {"success": False, "error": str(e)}
        
        # Get children from final response
        try:
            data = response.json()
            children = data.get("children", [])
        except:
            return {"success": False, "error": "Failed to parse multi-sim response"}
        
        # Handle error case
        if error_flag:
            if not children:
                logger.error(f"Multi-simulation failed: {data}")
                return {"success": False, "error": data.get("message", "Multi-simulation failed")}
            # Log child errors
            for child_id in children:
                child_resp = await self._request("GET", f"{self.BASE_URL}/simulations/{child_id}")
                logger.error(f"Child simulation {child_id} failed: {child_resp.json()}")
            return {"success": False, "error": "Multi-simulation children failed"}
        
        # Check if we have children
        if not children or len(children) == 0:
            logger.warning(f"Multi-simulation completed but no children: {data}")
            return {"success": False, "error": "No children in multi-simulation"}
        
        # Fetch results for each child
        async def fetch_child_result(child_id):
            try:
                # Fetch child simulation to get alpha ID
                child_url = f"{self.BASE_URL}/simulations/{child_id}"
                child_resp = await self._request("GET", child_url)
                
                if child_resp.status_code != 200:
                    logger.error(f"Failed to fetch child sim {child_id}: {child_resp.status_code}")
                    return {"success": False, "error": f"Failed to fetch child {child_id}"}
                
                child_data = child_resp.json()
                alpha_id = child_data.get("alpha")
                
                if not alpha_id:
                    logger.warning(f"Child simulation {child_id} has no alpha: {child_data}")
                    return {"success": False, "error": f"No alpha in child {child_id}"}
                
                # Fetch full alpha details
                return await self._get_completed_alpha_details(alpha_id)
                
            except Exception as e:
                logger.error(f"Error fetching child {child_id}: {e}")
                return {"success": False, "error": str(e)}
        
        # Fetch all children (parallel)
        results = await asyncio.gather(*(fetch_child_result(cid) for cid in children))
        return {"success": True, "results": list(results)}

    async def _wait_for_simulation(self, location: str, max_wait: int = 900) -> Dict:
        """
        Monitor simulation progress and return result when complete.
        Reference: ace_lib.py `simulation_progress` function.
        Key insight: Use Retry-After header presence to determine if still running.
        """
        # Determine full URL
        if location.startswith("http"):
            poll_url = location
        else:
            poll_url = f"{self.BASE_URL}{location}"
            
        error_flag = False
        retry_count = 0
        max_retries = 3
        
        while True:
            try:
                response = await self._request("GET", poll_url)

                # Handle non-2xx response with retry
                if response.status_code // 100 != 2:
                    logger.error(f"Simulation poll {poll_url}, Status: {response.status_code}, Retry")
                    await asyncio.sleep(30)
                    retry_count += 1
                    if retry_count <= max_retries:
                        continue
                    else:
                        logger.error(f"Simulation {poll_url} failed after {max_retries} retries")
                        error_flag = True
                        break
                
                # Key check: If Retry-After header is missing or 0, simulation is complete
                retry_after = response.headers.get("Retry-After") or response.headers.get("retry-after")
                
                if not retry_after or retry_after == "0":
                    # Simulation completed - check for error status
                    data = response.json()
                    if data.get("status", "ERROR") == "ERROR":
                        error_flag = True
                        logger.error(f"Simulation error: {data}")
                    break
                
                # Still running, wait as instructed
                await asyncio.sleep(float(retry_after))
                
            except Exception as e:
                import traceback
                logger.error(f"Poll loop error: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(3)
                retry_count += 1
                if retry_count > max_retries:
                    return {"success": False, "error": str(e)}
        
        if error_flag:
            try:
                error_data = response.json()
                return {"success": False, "error": error_data.get("message", str(error_data))}
            except:
                return {"success": False, "error": "Simulation failed"}
        
        # Get alpha ID from completed simulation
        try:
            data = response.json()
            alpha_id = data.get("alpha")
            
            if not alpha_id:
                logger.warning(f"Simulation completed but no alpha ID: {data}")
                return {"success": False, "error": "No Alpha ID returned"}
            
            # Fetch full alpha details
            return await self._get_completed_alpha_details(alpha_id)
            
        except Exception as e:
            logger.error(f"Failed to parse simulation result: {e}")
            return {"success": False, "error": str(e)}

    async def _get_completed_alpha_details(self, alpha_id: str) -> Dict:
        """
        Fetch full details for a completed alpha.
        Reference: ace_lib.py `get_simulation_result_json` function.
        Uses retry-after header polling to ensure data is ready.
        
        Real API response structure (from BRAIN MCP):
        - id, type, author, settings, regular{code, description, operatorCount}
        - dateCreated, dateSubmitted, dateModified, name, favorite, hidden
        - stage, status, grade, category, tags, classifications
        - is{pnl, bookSize, longCount, shortCount, turnover, returns, drawdown, margin, sharpe, fitness, startDate, investabilityConstrained{}, riskNeutralized{}, checks[]}
        - os, train, test (same structure as is)
        - prod, competitions, themes, pyramids, pyramidThemes, team, osmosisPoints
        """
        if alpha_id is None:
            return {"success": False, "error": "No alpha ID provided"}
            
        try:
            url = f"{self.BASE_URL}/alphas/{alpha_id}"

            # Poll until no retry-after header (matching ace_lib.py pattern)
            while True:
                response = await self._request("GET", url)
                
                # Check for retry-after header (case-insensitive)
                retry_after = response.headers.get("Retry-After") or response.headers.get("retry-after")
                
                if retry_after:
                    await asyncio.sleep(float(retry_after))
                else:
                    break
            
            if response.status_code != 200:
                logger.error(f"Failed to get alpha details [{response.status_code}]: {response.text}")
                return {"success": False, "error": f"Failed to fetch details: {response.status_code}"}
            
            try:
                alpha = response.json()
            except Exception:
                logger.error(f"Failed to parse alpha JSON: alpha_id={alpha_id}, headers={response.headers}, text={response.text}")
                return {"success": False, "error": "Failed to parse alpha response"}
            
            # Extract stats from each period
            is_stats = alpha.get("is") or {}
            train_stats = alpha.get("train") or {}
            test_stats = alpha.get("test") or {}
            os_stats = alpha.get("os") or {}
            
            # Extract checks from IS stats (important for submission validation)
            checks = is_stats.get("checks", [])
            failed_checks = [c for c in checks if c.get("result") == "FAIL"]
            pending_checks = [c for c in checks if c.get("result") == "PENDING"]
            passed_checks = [c for c in checks if c.get("result") == "PASS"]
            
            # Extract expression from regular.code
            regular = alpha.get("regular") or {}
            expression = regular.get("code")

            return {
                "success": True, 
                "alpha_id": alpha.get("id"),
                "expression": expression,
                "settings": alpha.get("settings", {}),
                "stage": alpha.get("stage"),  # IS or OS
                "status": alpha.get("status"),  # UNSUBMITTED, SUBMITTED, etc.
                "type": alpha.get("type"),  # REGULAR or SUPER
                "dateCreated": alpha.get("dateCreated"),
                "dateSubmitted": alpha.get("dateSubmitted"),
                "classifications": alpha.get("classifications", []),
                
                # Metrics dictionary with all available stats
                "metrics": {
                    # Primary IS metrics (for scoring)
                    "sharpe": is_stats.get("sharpe"),
                    "returns": is_stats.get("returns"),
                    "turnover": is_stats.get("turnover"),
                    "fitness": is_stats.get("fitness"),
                    "drawdown": is_stats.get("drawdown"),  # Renamed from max_dd
                    "pnl": is_stats.get("pnl"),
                    "margin": is_stats.get("margin"),
                    "bookSize": is_stats.get("bookSize"),
                    "longCount": is_stats.get("longCount"),
                    "shortCount": is_stats.get("shortCount"),
                    
                    # Train/Test metrics
                    "train_sharpe": train_stats.get("sharpe"),
                    "train_fitness": train_stats.get("fitness"),
                    "train_turnover": train_stats.get("turnover"),
                    "train_returns": train_stats.get("returns"),
                    "train_drawdown": train_stats.get("drawdown"),
                    
                    "test_sharpe": test_stats.get("sharpe"),
                    "test_fitness": test_stats.get("fitness"),
                    "test_turnover": test_stats.get("turnover"),
                    "test_returns": test_stats.get("returns"),
                    "test_drawdown": test_stats.get("drawdown"),
                    
                    # OS metrics (if available)
                    "os_sharpe": os_stats.get("sharpe") if os_stats else None,
                    "os_fitness": os_stats.get("fitness") if os_stats else None,
                    
                    # Investability and Risk Neutralized stats (nested dicts)
                    "investabilityConstrained": is_stats.get("investabilityConstrained") or {},
                    "riskNeutralized": is_stats.get("riskNeutralized") or {},
                    
                    # Train investability/risk stats
                    "train_investabilityConstrained": train_stats.get("investabilityConstrained") or {},
                    "train_riskNeutralized": train_stats.get("riskNeutralized") or {},
                    
                    # Test investability/risk stats  
                    "test_investabilityConstrained": test_stats.get("investabilityConstrained") or {},
                    "test_riskNeutralized": test_stats.get("riskNeutralized") or {},
                },
                
                # Submission checks (critical for knowing if alpha can be submitted)
                "checks": checks,
                "failed_checks": [c.get("name") for c in failed_checks],
                "pending_checks": [c.get("name") for c in pending_checks],
                "passed_checks": [c.get("name") for c in passed_checks],
                "can_submit": len(failed_checks) == 0 and len(pending_checks) == 0,
                
                # Full period data (for detailed analysis)
                "is": is_stats,
                "os": os_stats,
                "train": train_stats,
                "test": test_stats,
                
                # Additional metadata
                "regular": regular,  # Contains code, description, operatorCount
                "competitions": alpha.get("competitions"),
                "themes": alpha.get("themes"),
                "pyramids": alpha.get("pyramids"),
                
                # Include full raw response for debugging
                "raw": alpha
            }
        except Exception as e:
            logger.error(f"Get alpha details error: {e}")
            return {"success": False, "error": str(e)}

    # 2026-05-11: instance-level mutex coalescing concurrent re-auth attempts.
    # Without this, V-20.1 pipeline's 2 concurrent rounds each hit 401 and
    # each fire authenticate() (which is @retry(5)+backoff). BRAIN throttles
    # the auth endpoint when called too often → both fail → cascade stuck
    # in 401 loop. Observed 2026-05-11 06:55+ UTC: 4 consecutive SIMULATE
    # batches returned 'Incorrect authentication credentials.' because the
    # poll-path re-auth never recovered.
    _auth_lock: Optional[asyncio.Lock] = None

    def _get_auth_lock(self) -> asyncio.Lock:
        if BrainAdapter._auth_lock is None:
            BrainAdapter._auth_lock = asyncio.Lock()
        return BrainAdapter._auth_lock

    # 2026-05-12 (V-22.7): BRAIN returns this body string on some auth-failure
    # responses with non-401 status codes (observed: simulate POST returning
    # the message with the request body intact). The plain status-code check
    # used to miss these and the round burned with 0 alphas while the session
    # silently expired. Treat any response whose body contains this marker as
    # a 401-equivalent for the purposes of triggering re-auth.
    _AUTH_ERROR_BODY_MARKER: str = "Incorrect authentication credentials"

    def _is_auth_error(self, response: "httpx.Response") -> bool:
        """True iff the response signals an authentication failure either by
        status code (401) or by the BRAIN body marker. Reading body is cheap
        for these short error responses (httpx caches content)."""
        if response.status_code == 401:
            return True
        # Only inspect short, error-shaped bodies to avoid scanning large
        # success payloads. Cap at 2KB — auth-error bodies are tens of bytes.
        if response.status_code >= 400 or len(response.content) <= 2048:
            try:
                return self._AUTH_ERROR_BODY_MARKER in response.text
            except Exception:
                return False
        return False

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Single-shot HTTP call with auth-failure → coalesced re-auth → retry.

        For methods that own their own state machine (simulate POST + poll
        loop, slot accounting, paginated cursors) and can't easily wrap the
        full _safe_api_call retry/backoff machinery. This is just the
        token-refresh path — 429/5xx still need to be handled by the caller.

        Why this exists: BRAIN session tokens expire every ~4h. Task #19
        loop-stuck for 2.6h with all simulates returning 401 because
        simulate_alpha / simulate_batch / poll GET / get_alpha_pnl etc.
        were calling self.client.* directly, bypassing _safe_api_call's
        re-auth branch.

        2026-05-11 refinement: serialize concurrent re-auths via _auth_lock.
        After acquiring lock, re-check session validity — another coroutine
        may have already refreshed. This prevents BRAIN auth-endpoint
        throttling when multiple rounds hit 401 simultaneously.

        V-22.7 (2026-05-12): detection broadened to include the
        "Incorrect authentication credentials" body marker. Spike on task 530
        showed BRAIN returning non-401 statuses with that body, bypassing the
        old status-only check; 3 consecutive simulate rounds returned 0
        alphas because the session silently expired without re-auth firing.
        """
        response = await getattr(self.client, method.lower())(url, **kwargs)
        if not self._is_auth_error(response):
            return response
        logger.warning(
            f"auth failure on {method} {url} (status={response.status_code}) — re-authenticating"
        )
        await self._coalesced_reauth()
        # Retry up to 2x — cookie propagation across httpx pool can take
        # a tick on busy event loops.
        for _attempt in range(2):
            response = await getattr(self.client, method.lower())(url, **kwargs)
            if not self._is_auth_error(response):
                break
            await asyncio.sleep(0.5)
        return response

    async def _coalesced_reauth(self) -> bool:
        """Serialised, cache-invalidating re-auth shared by _request and
        _safe_api_call (V-27.91).

        Acquire _auth_lock → re-check session validity (another coroutine may
        have refreshed under the lock) → invalidate the Redis session cache
        BEFORE re-auth (V-26.24: so a failed authenticate() can't be undone by
        a stale cache resurrection) → authenticate().

        Before this helper existed, _safe_api_call's 401 branch was a bare
        status==401 check + bare authenticate() — it bypassed V-22.7 body-
        marker detection, V-26.24 cache invalidation, and the _auth_lock
        coalescing. Every data-fetch / submit path (get_alpha, submit_alpha,
        get_datasets, get_datafields, get_user_alphas) goes through
        _safe_api_call, so all of them could resurrect a dead session from
        Redis and stampede the BRAIN auth endpoint on concurrent 401s.

        Returns True if the session is usable afterwards.
        """
        async with self._get_auth_lock():
            # Thundering-herd fix (2026-05-19): a sibling worker (or this
            # worker's own prior task) may have refreshed and written a
            # fresh cookie to Redis while we were waiting for the lock.
            # Reload from Redis BEFORE _is_session_valid so our stale
            # in-memory cookie doesn't trigger a redundant authenticate()
            # that races against BRAIN's /authentication rate-limit (5/min).
            # Without this, N concurrent workers each invalidate the
            # Redis cache and call authenticate(), mutually nuking the
            # freshly-written cookie of the predecessor in the lock queue
            # — symptom: a sustained loop of "Incorrect authentication
            # credentials" errors observed on task 3083 (~36 rows / 90min).
            try:
                await self._load_session_from_redis()
                if await self._is_session_valid():
                    return True
            except Exception:
                pass
            # Redis cookie also stale (or load/check raised) — we really
            # are the first worker in this auth window. Invalidate + re-auth.
            await self._invalidate_session_cache()
            try:
                return bool(await self.authenticate())
            except Exception as e:
                logger.error(f"[BrainAdapter] coalesced re-auth failed: {e}")
                return False

    async def _safe_api_call(self, method: str, endpoint: str, **kwargs) -> httpx.Response:
        """
        Execute API call with auto-reauth on 401 and exponential backoff +
        jitter on 429/5xx. Backoff is shared across processes via Redis so
        concurrent callers cooperate when a rate limit is hit.
        """
        url = f"{self.BASE_URL}{endpoint}"
        retries = 0
        max_retries = 5

        while retries < max_retries:
            # 1) Hard cooldown set by a recent 429 — sleep until it expires.
            cooldown = await self._rl_remaining(endpoint)
            if cooldown > 0:
                await asyncio.sleep(cooldown)
            # 2) Soft pacing while strikes are warm — enforces a minimum gap
            #    between consecutive requests (across processes) so paginated
            #    bursts don't burst right back into the next 429.
            await self._rl_pace(endpoint)

            try:
                response = await getattr(self.client, method.lower())(url, **kwargs)

                # 1. Handle 401 / auth-failure body marker.
                # V-27.91: was a status==401-only check + bare authenticate(),
                # bypassing V-22.7 body-marker detection, V-26.24 cache
                # invalidation, and _auth_lock coalescing. Now routes through
                # _coalesced_reauth like _request does.
                #
                # V-27.91 follow-up: if re-auth fails, or the single retry
                # after re-auth STILL hits an auth error (cookie propagation
                # lag across the httpx pool), don't fall through and leak the
                # 401 to the caller — back off and re-enter the loop, same as
                # the 429/5xx branches. The bare `while` had no auth-error
                # `continue` so a transient post-reauth 401 escaped.
                if self._is_auth_error(response):
                    logger.warning(
                        f"auth failure for {endpoint} (status={response.status_code}), "
                        f"re-authenticating... (attempt {retries+1}/{max_retries})"
                    )
                    reauthed = await self._coalesced_reauth()
                    if reauthed:
                        response = await getattr(self.client, method.lower())(url, **kwargs)
                    if not reauthed or self._is_auth_error(response):
                        base = min(2 ** (retries + 1), self._RL_BACKOFF_CAP_SEC)
                        wait_time = base + random.uniform(0, base * 0.25)
                        await asyncio.sleep(wait_time)
                        retries += 1
                        continue

                # 2. Handle 429 Too Many Requests (Rate Limit)
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    strikes = await self._rl_record_strike(endpoint)
                    # Exponential floor that grows with recent 429s across all
                    # callers: 2,4,8,16,32,64s (capped). Retry-After acts as a
                    # lower bound so we never wait less than the server asks.
                    backoff = min(2 ** min(strikes, 6), self._RL_BACKOFF_CAP_SEC)
                    base = max(float(retry_after), backoff) if retry_after else backoff
                    wait_time = base + random.uniform(0, base * 0.25)
                    await self._rl_set_cooldown(endpoint, wait_time)
                    logger.warning(
                        f"429 Rate Limit for {endpoint}. Sleeping {wait_time:.2f}s "
                        f"(Retry-After={retry_after}, strikes={strikes}, "
                        f"attempt {retries+1}/{max_retries})"
                    )
                    await asyncio.sleep(wait_time)
                    retries += 1
                    continue

                # 3. Handle 5xx Server Errors (Temporary Glitch)
                if 500 <= response.status_code < 600:
                    base = min(2 ** (retries + 1), self._RL_BACKOFF_CAP_SEC)
                    wait_time = base + random.uniform(0, base * 0.25)
                    logger.warning(
                        f"Server Error {response.status_code} for {endpoint}. "
                        f"Sleeping {wait_time:.2f}s (attempt {retries+1}/{max_retries})"
                    )
                    await asyncio.sleep(wait_time)
                    retries += 1
                    continue

                return response

            except (httpx.RequestError, httpx.TimeoutException) as e:
                base = min(2 ** (retries + 1), self._RL_BACKOFF_CAP_SEC)
                wait_time = base + random.uniform(0, base * 0.25)
                logger.error(f"Network error {endpoint}: {e}. Retrying in {wait_time:.2f}s...")
                await asyncio.sleep(wait_time)
                retries += 1

        # If exhausted retries, return the last response or raise
        logger.error(f"Max retries exceeded for {endpoint}")
        if 'response' in locals():
            return response
        raise Exception(f"Failed to connect to {endpoint} after {max_retries} attempts")

    async def get_datasets(self, region: str = "USA", delay: int = 1, universe: str = "TOP3000") -> List[Dict]:
        try:
            response = await self._safe_api_call(
                "GET", "/data-sets",
                params={"region": region, "delay": delay, "universe": universe, "instrumentType": "EQUITY"}
            )
            return response.json().get("results", []) if response.status_code == 200 else []
        except Exception:
            return []

    async def get_datafields(self, dataset_id: str, region: str = "USA", delay: int = 1, universe: str = "TOP3000") -> List[Dict]:
        all_results = []
        offset = 0
        limit = 50
        
        while True:
            try:
                response = await self._safe_api_call(
                    "GET", "/data-fields",
                    params={
                        "dataset.id": dataset_id, 
                        "region": region, 
                        "delay": delay, 
                        "universe": universe, 
                        "instrumentType": "EQUITY",
                        "limit": limit,
                        "offset": offset
                    }
                )
                
                if response.status_code != 200:
                    logger.error(f"Get fields failed: {response.status_code} - {response.text}")
                    break
                    
                data = response.json()
                results = data.get("results", [])
                
                if not results:
                    break
                    
                all_results.extend(results)
                
                if len(results) < limit:
                    break
                    
                offset += limit
                
            except Exception as e:
                logger.error(f"Get fields error: {e}")
                break
                
        return all_results

    async def get_operators(self, detailed: bool = False) -> List[Any]:
        try:
            response = await self._safe_api_call("GET", "/operators")
            if response.status_code == 200:
                data = response.json()
                results = data if isinstance(data, list) else data.get("results", [])
                return results if detailed else [op.get("name") for op in results]
            return self._get_common_operators()
        except Exception:
            return self._get_common_operators()

    async def get_alpha_pnl(self, alpha_id: str) -> Dict:
        # V-27.128: go through _safe_api_call (like get_alpha) so cross-process
        # rate-limit cooldowns + retries apply. The old _request path had no
        # rate-limit handling and `except: return {}` silently swallowed 429s
        # as empty payloads — indistinguishable from an alpha with genuinely
        # no PnL, so a rate-limited fetch looked like real emptiness upstream.
        try:
            response = await self._safe_api_call(
                "GET", f"/alphas/{alpha_id}/recordsets/pnl"
            )
            return response.json() if response.status_code == 200 else {}
        except Exception:
            return {}

    async def get_alpha(self, alpha_id: str) -> Dict:
        """GET /alphas/{id} — full alpha detail with current is.sharpe / is.fitness /
        metrics.checks. Distinct from get_alpha_pnl which fetches the PnL series.

        Goes through _safe_api_call so cross-process rate-limit cooldowns and
        retries apply.
        """
        try:
            response = await self._safe_api_call("GET", f"/alphas/{alpha_id}")
            if response.status_code == 200:
                return response.json()
            logger.warning(
                f"[BrainAdapter] get_alpha({alpha_id}) status={response.status_code}"
            )
            return {}
        except Exception as e:
            logger.warning(f"[BrainAdapter] get_alpha({alpha_id}) failed: {e}")
            return {}

    async def submit_alpha(self, alpha_id: str, max_polls: int = 60) -> Dict:
        """POST /alphas/{id}/submit then poll on Retry-After until terminal.

        Mirrors ace_lib.submit_alpha + scripts/submit_alpha.py. BRAIN's submit
        endpoint kicks off an async job: the initial POST returns with a
        Retry-After header while the job runs; we GET the same path until the
        header disappears, at which point status 200 = accepted, anything
        else = rejected (body carries the reason).

        Never raises — transport/parse errors collapse to success=False so the
        caller (AlphaService.submit_alpha) can surface a clean reason. Submit
        is irreversible and consumes BRAIN quota; callers MUST run pre-flight
        gates (can_submit / self_corr precheck) before calling this.

        Returns: {success, status_code, body, polls}.
        """
        try:
            resp = await self._safe_api_call("POST", f"/alphas/{alpha_id}/submit")
        except Exception as e:
            logger.warning(f"[BrainAdapter] submit_alpha({alpha_id}) POST failed: {e}")
            return {"success": False, "status_code": 0, "body": str(e), "polls": 0}

        polls = 0
        reached_terminal = False
        while polls < max_polls:
            retry_after = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
            if not retry_after:
                # No Retry-After ⇒ BRAIN job reached a terminal state
                # (ace_lib.submit_alpha contract). status/body decide
                # accept vs reject below.
                reached_terminal = True
                break
            try:
                await asyncio.sleep(float(retry_after))
            except (TypeError, ValueError):
                await asyncio.sleep(2.0)
            try:
                resp = await self._safe_api_call("GET", f"/alphas/{alpha_id}/submit")
            except Exception as e:
                logger.warning(f"[BrainAdapter] submit_alpha({alpha_id}) poll failed: {e}")
                return {"success": False, "status_code": 0, "body": str(e), "polls": polls}
            polls += 1

        # V-27.122: max_polls exhausted while BRAIN still returns Retry-After
        # means the job never reached a terminal state. A 200 on the last
        # poll just means "still processing" — must NOT be treated as
        # success (submit is irreversible; a false success permanently
        # stamps date_submitted on an un-submitted alpha).
        if not reached_terminal:
            logger.warning(
                f"[BrainAdapter] submit_alpha({alpha_id}) polling timed out "
                f"after {polls} polls — job still running, treating as failure"
            )
            return {
                "success": False,
                "status_code": resp.status_code,
                "body": "submit polling timed out (max_polls exhausted; job still running)",
                "polls": polls,
                "timed_out": True,
            }

        body_text = resp.text or ""
        try:
            body = resp.json() if body_text else {}
        except Exception:
            body = body_text
        return {
            # V-27.102: accept any 2xx, not just 200 — BRAIN returns 200
            # today, but a future 201/202 async-accept must not be misread
            # as a rejection.
            "success": 200 <= resp.status_code < 300,
            "status_code": resp.status_code,
            "body": body,
            "polls": polls,
        }

    async def get_before_and_after_performance(
        self,
        alpha_id: str,
        competition: Optional[str] = None,
        team_id: Optional[str] = None,
    ) -> Dict:
        """GET /competitions/{competition}/alphas/{alpha_id}/before-and-after-performance
        (or /teams/{team_id}/... or /users/self/... when neither is given).

        BRAIN's marginal-contribution comparison: returns this alpha's PnL
        standalone vs. merged into the named competition / team / personal
        portfolio. Used by IQC submission-selection workflow — competition
        leaderboard scores merged metrics not standalone IS metrics, so picking
        which can_submit alphas to actually submit depends on this delta.

        Response shape (key fields):
          stats.before / stats.after  — sharpe / fitness / turnover / pnl /
                                        margin / returns / drawdown / longCount
          yearlyStats.before / .after — per-year records
          pnl.records                  — per-day [date, beforePnL, afterPnL]
          score.before / .after        — competition score
          competition / team / partition

        Polls on Retry-After (BRAIN computes merged stats asynchronously).
        Returns {} on persistent failure; caller checks for empty dict.
        """
        if competition is not None:
            scope = f"competitions/{competition}"
        elif team_id is not None:
            scope = f"teams/{team_id}"
        else:
            scope = "users/self"
        url = f"{self.BASE_URL}/{scope}/alphas/{alpha_id}/before-and-after-performance"

        max_polls = 30  # ~30 × Retry-After (BRAIN typically returns 1-5s)
        for poll_idx in range(max_polls):
            try:
                response = await self._safe_api_call("GET", url.replace(self.BASE_URL, ""))
            except Exception as e:
                logger.warning(
                    f"[BrainAdapter] get_before_and_after_performance({alpha_id}) "
                    f"scope={scope} poll={poll_idx} failed: {e}"
                )
                return {}
            if response.status_code != 200:
                logger.warning(
                    f"[BrainAdapter] get_before_and_after_performance({alpha_id}) "
                    f"scope={scope} status={response.status_code}"
                )
                return {}
            # Retry-After signals "still computing"; absent/0 = ready.
            retry_after = (
                response.headers.get("Retry-After")
                or response.headers.get("retry-after")
            )
            if not retry_after or retry_after == "0":
                data = response.json() or {}
                if data.get("stats"):
                    return data
                logger.warning(
                    f"[BrainAdapter] get_before_and_after_performance({alpha_id}) "
                    f"empty stats payload"
                )
                return {}
            try:
                await asyncio.sleep(float(retry_after))
            except (TypeError, ValueError):
                await asyncio.sleep(2)
        logger.warning(
            f"[BrainAdapter] get_before_and_after_performance({alpha_id}) "
            f"polled {max_polls}× without completion"
        )
        return {}

    async def check_correlation(self, alpha_id: str, check_type: str = "PROD") -> Dict:
        """Return correlation report wrapped with status_code.

        P3-Brain (plan §6.1): caller needs to distinguish HTTP 200/empty,
        HTTP 403 (no Consultant permission → auto-revert safety net), and
        network/5xx errors. Old shape (bare dict / empty on any failure)
        loses that info — callers using check_correlation_with_poll get the
        classified status; legacy callers expecting a dict can still treat
        result["data"] as the correlation payload.

        Returns:
            {"status_code": int, "data": Dict}
            status_code=0 on exception/network failure (caller decides retry).
        """
        try:
            response = await self._request("GET", f"{self.BASE_URL}/alphas/{alpha_id}/correlations/{check_type}")
            return {
                "status_code": response.status_code,
                "data": response.json() if response.status_code == 200 else {},
            }
        except Exception as ex:
            logger.warning(f"[check_correlation] {alpha_id}/{check_type} exception: {ex}")
            return {"status_code": 0, "data": {}}

    async def check_correlation_with_poll(
        self,
        alpha_id: str,
        check_type: str = "PROD",
        *,
        max_polls: int = 3,
        poll_interval: float = 5.0,
    ) -> Dict:
        """Poll-aware wrapper around check_correlation with status classification.

        BRAIN async-computes correlation:首次调用常 200 + 空 records(还在算)→ retry。
        403 = 账号无 Consultant 权限 → AUTH_DENIED(触发安全网 auto-revert)。
        Returns:
            {"status": "OK", "data": {...}}        — 200 + max present
            {"status": "PENDING"}                  — exhausted retries while empty
            {"status": "AUTH_DENIED"}              — 403 from BRAIN
            {"status": "ERROR"}                    — 其它意外(目前折回 PENDING)
        """
        for attempt in range(max_polls):
            result = await self.check_correlation(alpha_id, check_type)
            sc = result["status_code"]
            data = result["data"]
            if sc == 403:
                return {"status": "AUTH_DENIED"}
            if sc == 200 and "max" in data:
                return {"status": "OK", "data": data}
            if attempt < max_polls - 1:
                await asyncio.sleep(poll_interval)
        return {"status": "PENDING"}

    async def get_user_alphas(self, limit: int = 100, offset: int = 0, stage: str = None, search: str = None, start_date: str = None) -> Dict:
        """
        Get user's alphas with pagination.
        endpoint: /users/self/alphas
        """
        try:
            params = {
                "limit": limit, 
                "offset": offset,
                "hidden": False,
                "order": "-dateCreated"
            }
            if stage:
                params["stage"] = stage
            if search:
                params["search"] = search
            if start_date:
                # Brain API often uses 'startDate' for filtering creation date
                params["startDate"] = start_date
                
            response = await self._safe_api_call("GET", "/users/self/alphas", params=params)
            
            if response.status_code == 200:
                return response.json()
            return {"results": [], "count": 0}
        except Exception as e:
            logger.error(f"Failed to get user alphas: {e}")
            return {"results": [], "count": 0}

    def _get_common_operators(self) -> List[str]:
        return ["rank", "ts_rank", "ts_zscore", "ts_mean", "ts_delay", "ts_corr", "ts_max", "ts_min", "abs", "log", "sign"]


# =============================================================================
# Singleton Instance Management
# =============================================================================

_brain_adapter_instance: Optional[BrainAdapter] = None
_brain_adapter_lock = asyncio.Lock()


async def get_brain_adapter() -> BrainAdapter:
    """
    Get or create the singleton BrainAdapter instance.
    
    This provides a standard way to access the adapter throughout the application,
    ensuring session reuse and proper authentication state management.
    
    Returns:
        BrainAdapter instance implementing BrainProtocol
    """
    global _brain_adapter_instance
    
    if _brain_adapter_instance is None:
        async with _brain_adapter_lock:
            if _brain_adapter_instance is None:
                _brain_adapter_instance = BrainAdapter()
                await _brain_adapter_instance.ensure_session()
    
    return _brain_adapter_instance


def get_brain_adapter_sync() -> BrainAdapter:
    """
    Get or create the singleton BrainAdapter instance (sync version).
    
    Warning: This does NOT ensure the session is valid.
    Use get_brain_adapter() in async contexts when possible.
    
    Returns:
        BrainAdapter instance
    """
    global _brain_adapter_instance
    
    if _brain_adapter_instance is None:
        _brain_adapter_instance = BrainAdapter()
    
    return _brain_adapter_instance


# Backward compatibility alias
brain_adapter = get_brain_adapter_sync()


def reset_brain_adapter():
    """Reset the singleton instance. Useful for testing."""
    global _brain_adapter_instance
    _brain_adapter_instance = None
