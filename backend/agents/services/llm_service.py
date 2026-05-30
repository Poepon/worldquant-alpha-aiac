"""
LLM Service - Unified LLM calling interface with logging and retries

Implements LLMProtocol for dependency injection and testability.
High cohesion: All LLM-related logic in one place.
"""

import asyncio
import json
import time
from contextvars import ContextVar
from typing import Dict, List, Optional, Any, Type, Tuple
from pydantic import BaseModel
import httpx
import openai
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from loguru import logger

from backend.config import settings
from backend.protocols.llm_protocol import LLMProtocol, LLMResponse as LLMResponseProtocol
from backend.circuit_breaker import CircuitBreaker

# W5: Anthropic SDK is optional — only loaded when LLM_PROVIDER=anthropic.
try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    anthropic = None
    _ANTHROPIC_AVAILABLE = False


# ---------------------------------------------------------------------------
# Phase 4 Sprint 0 PR0 — LLM_API_CIRCUIT (2026-05-19)
# ---------------------------------------------------------------------------
# Module-level CircuitBreaker instance — defended against DeepSeek/Anthropic
# 5xx / timeout outages. Pattern mirrors BRAIN_AUTH_CIRCUIT (brain_adapter.py:46)
# but with N-consecutive-fail trip threshold rather than immediate trip on
# single auth-error. Rationale: BRAIN 401 is hard fail; LLM 5xx is often
# transient blip (rate limit, network), shouldn't trip on single error.
# Per-(provider, endpoint, model) circuit registry (2026-05-31, gap-1 fix).
# ---------------------------------------------------------------------------
# The original design was a SINGLE module-level circuit for ALL LLM traffic. On
# the production single-gateway deployment (one Aliyun-MaaS endpoint serving
# deepseek/qwen/kimi behind provider="openai"), a 5xx storm on ONE model tripped
# the ONE circuit and fast-failed EVERY node's calls (cross-model brown-out),
# AND gated the P1#6 runtime fallback (routed model down → fall back to default
# model → same circuit OPEN → fallback suppressed). Fix: one circuit per
# (provider, endpoint, model) SCOPE, so a model's outage only fast-fails that
# model and the default-model fallback rides its OWN (closed) circuit. Endpoint
# marker is "default" when the routed entry carries no base_url override (stable
# regardless of cred-resolution order); an explicit override gets an 8-char sha
# so distinct endpoints isolate.
#
# LLM_API_CIRCUIT is retained as a LEGACY handle (import back-compat) but is no
# longer consulted on the hot path — production trips per-scope circuits below.
LLM_API_CIRCUIT = CircuitBreaker("llm_api", default_ttl_sec=300)

_LLM_CIRCUIT_NAME_PREFIX = "llm_api"
_LLM_CIRCUIT_REGISTRY: Dict[str, CircuitBreaker] = {}


def _llm_circuit_scope(provider: Optional[str], base_url: Optional[str],
                       model: Optional[str]) -> str:
    """Stable per-(provider, endpoint, model) scope string. base_url falsy → the
    construction-default endpoint (marker 'default'), so the scope does NOT
    depend on whether creds have resolved self.base_url yet. On the single-
    gateway deployment every entry uses the default endpoint → scopes differ
    ONLY by model, which is the real isolation axis there."""
    import hashlib
    ep = "default"
    if base_url:
        ep = hashlib.sha256(base_url.encode("utf-8")).hexdigest()[:8]
    return f"{(provider or 'openai').lower()}:{ep}:{model or '_default'}"


def _get_llm_circuit(scope: str) -> CircuitBreaker:
    """Lazily build + cache the CircuitBreaker for a scope (construction is cheap
    — it only stores the name; the cache just avoids re-alloc per call)."""
    cb = _LLM_CIRCUIT_REGISTRY.get(scope)
    if cb is None:
        cb = CircuitBreaker(f"{_LLM_CIRCUIT_NAME_PREFIX}:{scope}", default_ttl_sec=300)
        _LLM_CIRCUIT_REGISTRY[scope] = cb
    return cb


def _llm_fail_counter_key(scope: str) -> str:
    return f"{_LLM_CIRCUIT_NAME_PREFIX}:fail_counter:{scope}"


def _llm_get_redis():
    """Soft-fail Redis getter — never raises."""
    try:
        from backend.tasks.redis_pool import get_redis_client
        return get_redis_client()
    except Exception:
        return None


def _llm_record_fail(scope: str, error_kind: str = "unknown") -> None:
    """Increment the per-SCOPE consecutive-fail counter; trip THAT scope's
    circuit when it reaches LLM_API_CIRCUIT_FAIL_THRESHOLD within
    LLM_API_CIRCUIT_FAIL_WINDOW_SEC.

    Called from the LLMService.call() exception path. Soft-fail Redis blip →
    no-op (a Redis outage MUST NEVER cause brown-out by spuriously tripping).
    """
    from backend.config import settings as _stg
    if not getattr(_stg, "ENABLE_LLM_API_CIRCUIT", True):
        return
    threshold = int(getattr(_stg, "LLM_API_CIRCUIT_FAIL_THRESHOLD", 5))
    window = int(getattr(_stg, "LLM_API_CIRCUIT_FAIL_WINDOW_SEC", 60))
    cooldown = int(getattr(_stg, "LLM_API_CIRCUIT_COOLDOWN_SEC", 300))
    r = _llm_get_redis()
    if r is None:
        return
    counter_key = _llm_fail_counter_key(scope)
    try:
        new_count = r.incr(counter_key)
        if new_count == 1:
            # First failure in window — set TTL so the counter naturally expires.
            r.expire(counter_key, window)
        if int(new_count) >= threshold:
            _get_llm_circuit(scope).trip(
                reason=f"llm_consec_fail_{int(new_count)}_{error_kind[:40]}_{scope[:48]}",
                ttl_sec=cooldown,
            )
            # Reset so the next `threshold` post-clear failures can re-trip.
            r.delete(counter_key)
    except Exception:
        pass


def _llm_record_success(scope: str) -> None:
    """Reset the per-SCOPE fail counter AND clear THAT scope's circuit on any
    success. The clear() is a no-op when already CLOSED; only matters when we
    recovered from an OPEN/HALF_OPEN probe for this scope.
    """
    from backend.config import settings as _stg
    if not getattr(_stg, "ENABLE_LLM_API_CIRCUIT", True):
        return
    r = _llm_get_redis()
    if r is not None:
        try:
            r.delete(_llm_fail_counter_key(scope))
        except Exception:
            pass
    try:
        cb = _get_llm_circuit(scope)
        if cb.is_open():
            cb.clear(reason="llm_api_success_probe")
    except Exception:
        pass


def llm_circuits_status_all() -> Dict[str, Any]:
    """Aggregate snapshot over all per-scope LLM circuits (for ops). Enumerates
    Redis ``circuit:llm_api:*`` keys (tiny keyspace; ops-only, infrequent) and
    folds them into a single aggregate (back-compat with the old single-circuit
    endpoint) plus the list of currently-open scopes."""
    r = _llm_get_redis()
    scopes: List[Dict[str, Any]] = []
    if r is not None:
        try:
            prefix = f"{CircuitBreaker.KEY_PREFIX}:{_LLM_CIRCUIT_NAME_PREFIX}:"
            for k in (r.keys(f"{prefix}*") or []):
                key = k.decode("utf-8") if isinstance(k, (bytes, bytearray)) else k
                name = key[len(CircuitBreaker.KEY_PREFIX) + 1:]   # strip "circuit:"
                st = CircuitBreaker(name).status().to_dict()
                st["scope"] = name[len(_LLM_CIRCUIT_NAME_PREFIX) + 1:]  # strip "llm_api:"
                scopes.append(st)
        except Exception:
            pass
    open_scopes = [s for s in scopes if s.get("state") == "open"]
    half = [s for s in scopes if s.get("state") == "half_open"]
    state = "open" if open_scopes else ("half_open" if half else "closed")
    newest = max(scopes, key=lambda s: (s.get("last_failure_at") or 0), default=None)
    return {
        "state": state,
        "until_ts": max((s.get("until_ts") or 0) for s in open_scopes) if open_scopes else None,
        "last_failure_at": (newest or {}).get("last_failure_at"),
        "last_failure_reason": (newest or {}).get("last_failure_reason"),
        "trip_count": sum(int(s.get("trip_count") or 0) for s in scopes),
        "seconds_until_half_open": max((s.get("seconds_until_half_open") or 0) for s in open_scopes) if open_scopes else 0,
        "open_scopes": [s["scope"] for s in open_scopes],
        "scopes": scopes,
    }


def llm_circuits_clear_all(reason: str = "clear_all") -> int:
    """Clear every per-scope LLM circuit + its fail counter. Returns the number
    of circuits cleared. Used by ops /clear and the per-node benchmark."""
    r = _llm_get_redis()
    n = 0
    if r is not None:
        try:
            cprefix = f"{CircuitBreaker.KEY_PREFIX}:{_LLM_CIRCUIT_NAME_PREFIX}:"
            for k in (r.keys(f"{cprefix}*") or []):
                r.delete(k)
                n += 1
            for k in (r.keys(f"{_LLM_CIRCUIT_NAME_PREFIX}:fail_counter:*") or []):
                r.delete(k)
        except Exception:
            pass
    _LLM_CIRCUIT_REGISTRY.clear()
    return n


def _llm_error_is_api_failure(exc: BaseException) -> bool:
    """Classify whether an exception is an LLM-provider API failure worth
    incrementing the fail counter, versus a *content* failure (JSON parse,
    empty response, bad arg) which we shouldn't trip on.

    Recognized API failures:
      - openai.APIConnectionError / APITimeoutError / RateLimitError /
        APIStatusError (5xx) / AuthenticationError (401) /
        PermissionDeniedError (403)
      - anthropic.APIConnectionError / APITimeoutError / RateLimitError /
        APIStatusError (5xx) / AuthenticationError (401) /
        PermissionDeniedError (403) — only if anthropic SDK loaded

    F-S1 (post-review): 401 / 403 are now treated as API failures (mirrors
    BRAIN_AUTH_CIRCUIT auth-error trip). Both providers return 401 when API
    key is revoked / expired and 403 when org quota is exhausted; without
    this, callers loop indefinitely and burn budget silently — exactly the
    pattern this circuit was built to root-cause.
    """
    name = type(exc).__name__
    # openai/anthropic SDK exception hierarchy (class names are identical
    # across both providers' SDKs, so single name set covers both)
    api_exc_names = {
        "APIConnectionError", "APITimeoutError", "RateLimitError",
        "InternalServerError", "APIStatusError",
        # F-S1: 401 + 403 auth/permission errors
        "AuthenticationError", "PermissionDeniedError",
        # 2026-05-21: asyncio.wait_for hard-deadline raises builtin TimeoutError
        # (== asyncio.TimeoutError). Classify as API failure so a hung provider
        # trips LLM_API_CIRCUIT instead of silently burning rounds.
        "TimeoutError",
    }
    if name in api_exc_names:
        return True
    # status_code attribute on APIError subclasses
    status = getattr(exc, "status_code", None)
    if status is not None:
        try:
            s = int(status)
            # F-S1: 401, 403, 429, 5xx all trip
            return s >= 500 or s in (401, 403, 429)
        except Exception:
            return False
    return False


# ---------------------------------------------------------------------------
# Per-functional-block model routing (PR1, 2026-05-29)
# ---------------------------------------------------------------------------
_VALID_PROVIDERS = frozenset({"openai", "anthropic"})

# Per-task node→model overrides (from task.config["llm_overrides"]), bound to the
# current async context (PR5). A ContextVar — NOT instance state — so concurrent
# mining tasks and the pipeline producer/consumer coroutines never bleed each
# other's overrides (mirrors PR2's per-call concurrency contract). Child tasks
# created after the set() inherit a copy of the context.
_TASK_FN_OVERRIDES: ContextVar = ContextVar("task_llm_function_overrides", default=None)


def set_task_function_overrides(overrides):
    """Bind per-task node→model overrides to the current async context.

    Independent of the global ENABLE_PER_FUNCTION_LLM_ROUTING flag → enables a
    single-task single-node A/B (Phase C attribution): the global flag can stay
    OFF while one task routes one node. Non-dict → cleared. Returns the reset Token.
    """
    return _TASK_FN_OVERRIDES.set(overrides if isinstance(overrides, dict) else None)


def clear_task_function_overrides(token=None):
    """Clear per-task overrides; resets to the Token returned by set_* when given
    (preferred — restores the prior context value instead of blanking it)."""
    if token is not None:
        try:
            _TASK_FN_OVERRIDES.reset(token)
            return
        except Exception:  # noqa: BLE001
            pass
    _TASK_FN_OVERRIDES.set(None)


def _validate_model_entry(entry) -> Optional[Dict[str, Any]]:
    """Validate + shallow-copy a ``{model, provider, ...}`` entry. Returns None
    when malformed (not a dict / missing model / bad provider). The shallow copy
    prevents callers from mutating a shared cache/override object."""
    if not isinstance(entry, dict):
        return None
    model = entry.get("model")
    if not model or not isinstance(model, str):
        return None
    provider = entry.get("provider") or "openai"
    if provider not in _VALID_PROVIDERS:
        return None
    resolved: Dict[str, Any] = {"model": model, "provider": provider}
    for k in ("base_url", "api_key_ref", "thinking_effort"):
        v = entry.get(k)
        if v:
            resolved[k] = v
    return resolved


def resolve_model_for(
    node_key: Optional[str], region: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Resolve the ``{model, provider, ...}`` a functional block should use.

    Priority:
      1. **task-level override** (``_TASK_FN_OVERRIDES`` contextvar, from
         task.config["llm_overrides"]) — honoured INDEPENDENT of the global flag,
         so Phase C can A/B one node on one task while everyone else stays default.
      2. **global map** — only when ENABLE_PER_FUNCTION_LLM_ROUTING is ON; read
         directly from ``_flag_override_cache`` (P0-1: the ``__getattribute__``
         hook only honours ``ENABLE_``-prefixed overrides, so ``settings.X`` would
         never see the front-end edit), falling back to the startup default cache.

    Returns ``None`` (→ caller uses self.model/self.provider) in every non-routing
    path, so flag-OFF + no task override is byte-for-byte legacy. Per-entry
    validation is defensive and NEVER raises — a bad edit must not crash a round.
    ``region`` reserved for future per-region overrides.
    """
    try:
        if not node_key:
            return None
        # 1. task-level override (contextvar) — independent of the global flag.
        task_ov = _TASK_FN_OVERRIDES.get()
        if isinstance(task_ov, dict) and node_key in task_ov:
            resolved = _validate_model_entry(task_ov.get(node_key))
            if resolved is not None:
                return resolved
            # malformed task entry → fall through to the global map (don't break)
        # 2. global map (flag-gated). An override (even malformed) means the
        # front-end intends to REPLACE the startup map → honour iff well-formed
        # dict, else None (NOT the superseded startup map). Startup default is
        # used only when there is NO override key at all.
        if not getattr(settings, "ENABLE_PER_FUNCTION_LLM_ROUTING", False):
            return None
        from backend.config import _flag_override_cache, _LLM_FUNCTION_MODEL_MAP_CACHE
        override = _flag_override_cache.get("LLM_FUNCTION_MODEL_MAP")
        model_map = override if override is not None else _LLM_FUNCTION_MODEL_MAP_CACHE
        if not isinstance(model_map, dict):
            return None
        return _validate_model_entry(model_map.get(node_key))
    except Exception as e:  # noqa: BLE001 — routing must never break a call
        logger.warning(f"[resolve_model_for] node_key={node_key!r} failed, using default | {e}")
        return None


# Anthropic reasoning models that reject `temperature` at the API layer.
# Same pattern as OpenAI's o-series — reasoning is opaque, so the param is
# deprecated and a 400 is returned if passed. Match by prefix so dated /
# context-variant ids (e.g. "claude-opus-4-7[1m]", "claude-opus-4-7-20260301")
# are covered.
_ANTHROPIC_NO_TEMPERATURE_PREFIXES: Tuple[str, ...] = ("claude-opus-4-7",)

# Anthropic reasoning models that support extended thinking. Same prefix
# pattern as the no-temperature list; kept separate because the two
# capabilities are technically independent (e.g. a future opus could still
# accept temperature without thinking, or vice versa).
_ANTHROPIC_THINKING_PREFIXES: Tuple[str, ...] = ("claude-opus-4-7",)

# Reasoning-effort tier → thinking budget_tokens. 1024 is Anthropic's hard
# minimum (per /thinking docs). Tier names match Anthropic's model capability
# metadata (low/medium/high/max) plus our intermediate "xhigh" between high
# and max (mirrors OpenAI o-series x-high reasoning_effort).
_ANTHROPIC_THINKING_BUDGETS: Dict[str, int] = {
    "low":    1024,
    "medium": 4096,
    "high":   16384,
    "xhigh":  32000,
    "max":    64000,   # Anthropic official top tier — budget < max_tokens still applies
}

# Tier name aliases — "auto" is the user-friendly name for Anthropic's
# `thinking.type=adaptive` (model self-allocates budget).
_ANTHROPIC_EFFORT_ALIASES: Dict[str, str] = {
    "auto": "adaptive",
}


def _anthropic_supports_temperature(model: str) -> bool:
    return not any(model.startswith(p) for p in _ANTHROPIC_NO_TEMPERATURE_PREFIXES)


def _anthropic_supports_thinking(model: str) -> bool:
    return any(model.startswith(p) for p in _ANTHROPIC_THINKING_PREFIXES)


class LLMResponse(BaseModel):
    """Standard LLM response wrapper."""
    content: str
    parsed: Optional[Dict] = None
    model: str
    tokens_used: int = 0
    latency_ms: int = 0
    success: bool = True
    error: Optional[str] = None
    
    def to_protocol_response(self) -> LLMResponseProtocol:
        """Convert to protocol response type."""
        return LLMResponseProtocol(
            content=self.content,
            parsed=self.parsed,
            model=self.model,
            tokens_used=self.tokens_used,
            latency_ms=self.latency_ms,
            success=self.success,
            error=self.error,
        )


class LLMService:
    """
    Unified LLM Service implementing LLMProtocol.
    
    Features:
    - Automatic retries with exponential backoff
    - JSON cleaning (markdown removal)
    - Token tracking
    - Structured logging
    - Credential caching with invalidation support
    
    This class implements the LLMProtocol interface, allowing for
    easy mocking in tests and dependency injection.
    """
    
    def __init__(
        self,
        api_key: str = None,
        base_url: str = None,
        model: str = None,
        provider: str = None,
    ):
        # W5: provider switch (openai-compat vs anthropic). Provider is
        # selected from settings.LLM_PROVIDER ("openai" | "anthropic"); the
        # corresponding api_key/model override applies. The opposite
        # provider's credentials are still loaded as fallback.
        self.provider = (provider or getattr(settings, 'LLM_PROVIDER', 'openai')).lower()

        # OpenAI-compatible (Qwen/DeepSeek/etc.) — always set up so caller can
        # fall back per-call by passing provider="openai".
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.base_url = base_url or settings.OPENAI_BASE_URL
        self.openai_model = model if (model and self.provider == 'openai') else getattr(
            settings, 'OPENAI_MODEL', 'deepseek-chat'
        )

        # Anthropic (W5)
        self.anthropic_api_key = getattr(settings, 'ANTHROPIC_API_KEY', '') or ''
        self.anthropic_model = model if (model and self.provider == 'anthropic') else getattr(
            settings, 'ANTHROPIC_MODEL', 'claude-haiku-4-5'
        )
        # Optional endpoint override (proxy / mirror). The constructor's
        # `base_url` doubles as the anthropic override when provider=anthropic;
        # otherwise we fall back to settings.ANTHROPIC_BASE_URL ("" → SDK default).
        self.anthropic_base_url = (
            base_url if (base_url and self.provider == 'anthropic')
            else (getattr(settings, 'ANTHROPIC_BASE_URL', '') or '')
        )
        # Extended-thinking reasoning effort (opus-4-7 family). Settings-driven
        # so call() signature stays Protocol-stable. Normalize to lowercase;
        # unknown values fall back to "xhigh" at use-time.
        self.anthropic_thinking_effort = (
            getattr(settings, 'ANTHROPIC_THINKING_EFFORT', 'xhigh') or 'xhigh'
        ).strip().lower()

        # Active model for self.model (back-compat with downstream readers)
        self.model = (
            self.anthropic_model if self.provider == 'anthropic' else self.openai_model
        )

        self._credentials_lock = asyncio.Lock()
        self._credentials_loaded = False

        # Per-(provider, endpoint, key) client cache for per-call model routing
        # (PR2). Construction-default clients are seeded below; a routed call to
        # a different endpoint/provider lazily builds + caches its own via
        # _get_client — so a default-openai instance can still reach anthropic
        # without the old "anthropic_client is None" hard-crash (P0-2).
        self._client_cache: Dict[Tuple[str, str, str], Any] = {}

        self.client = self._build_openai_client(self.api_key, self.base_url)

        # Lazy-init anthropic client only when provider=anthropic; per-call
        # routing to anthropic from a default-openai instance builds it on
        # demand via _get_client.
        self.anthropic_client = None
        if self.provider == 'anthropic':
            self.anthropic_client = self._build_anthropic_client(
                self.anthropic_api_key, self.anthropic_base_url
            )

        # Pre-resolve thinking-tier display name for logging only.
        _thinking_tag = (
            self.anthropic_thinking_effort
            if (self.provider == 'anthropic' and _anthropic_supports_thinking(self.model))
            else 'n/a'
        )
        logger.info(
            f"[LLMService] Initialized | provider={self.provider} model={self.model} "
            f"openai_base_url={self.base_url} "
            f"anthropic_base_url={self.anthropic_base_url or '<sdk default>'} "
            f"thinking={_thinking_tag}"
        )

    # ------------------------------------------------------------------
    # Client factory + per-(provider, endpoint, key) cache (PR2)
    # ------------------------------------------------------------------
    def _build_openai_client(self, api_key: str, base_url: str):
        # 2026-05-21: explicit timeout — without it a dead socket hangs the
        # event loop forever (no timeout fires). max_retries=0: SDK retry off,
        # the @retry decorator + LLM_API_CIRCUIT own the retry/backoff policy.
        return openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=httpx.Timeout(settings.LLM_CALL_TIMEOUT_SEC, connect=10.0),
            max_retries=0,
        )

    def _build_anthropic_client(self, api_key: str, base_url: str):
        """Build an AsyncAnthropic client. Raises on an unreachable target
        (SDK missing / empty key) — the caller converts that into a runtime
        fallback to the default model rather than crashing the round (P0-2)."""
        if not _ANTHROPIC_AVAILABLE:
            raise RuntimeError(
                "anthropic provider requested but the `anthropic` SDK is not "
                "installed. Run: pip install anthropic>=0.40"
            )
        if not api_key:
            raise RuntimeError(
                "anthropic provider requested but ANTHROPIC_API_KEY is empty"
            )
        # Only pass base_url when explicitly overridden — keeps the SDK default
        # (https://api.anthropic.com) when the override is "".
        anthropic_kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            anthropic_kwargs["base_url"] = base_url
        # explicit timeout (thinking streams can run minutes) + max_retries=0
        # so the @retry/circuit layer owns retry policy.
        anthropic_kwargs["timeout"] = settings.LLM_STREAM_TIMEOUT_SEC
        anthropic_kwargs["max_retries"] = 0
        # Present requests as the Claude CLI. default_headers is merged last in
        # the SDK so these win over the built-in UA. Empty settings → omitted.
        extra_headers: Dict[str, str] = {}
        ua = (getattr(settings, 'ANTHROPIC_USER_AGENT', '') or '').strip()
        if ua:
            extra_headers["User-Agent"] = ua
        x_app = (getattr(settings, 'ANTHROPIC_X_APP', '') or '').strip()
        if x_app:
            extra_headers["x-app"] = x_app
        if extra_headers:
            anthropic_kwargs["default_headers"] = extra_headers
        return anthropic.AsyncAnthropic(**anthropic_kwargs)

    @staticmethod
    def _client_cache_key(provider: str, base_url: str, api_key: str) -> Tuple[str, str, str]:
        # sha256-prefix the key so the cache key never holds plaintext creds.
        import hashlib
        h = hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:16]
        return (provider, base_url or "", h)

    def _get_client(self, provider: str, base_url: Optional[str] = None,
                    api_key_ref: Optional[str] = None):
        """Return a cached SDK client for (provider, endpoint, key).

        Call AFTER ``_ensure_credentials_loaded`` so self.api_key/base_url are
        the resolved (DB-or-env) values. ``api_key_ref`` (per-entry credential
        override) is reserved for PR5; until then routing reuses the
        construction-loaded key for that provider.
        """
        if provider == "anthropic":
            api_key = self.anthropic_api_key
            burl = base_url or self.anthropic_base_url
            ck = self._client_cache_key("anthropic", burl, api_key)
            client = self._client_cache.get(ck)
            if client is None:
                client = self._build_anthropic_client(api_key, burl)
                self._client_cache[ck] = client
            return client
        # openai-compat
        api_key = self.api_key
        burl = base_url or self.base_url
        ck = self._client_cache_key("openai", burl, api_key)
        client = self._client_cache.get(ck)
        if client is None:
            client = self._build_openai_client(api_key, burl)
            self._client_cache[ck] = client
        return client

    def clear_client_cache(self):
        """Drop cached routed clients — called when credentials change so a new
        key isn't shadowed by a client built with the old one."""
        self._client_cache = {}

    async def _ensure_credentials_loaded(self):
        if self._credentials_loaded:
            return

        async with self._credentials_lock:
            if self._credentials_loaded:
                return

            try:
                from backend.database import AsyncSessionLocal
                from backend.services.credentials_service import CredentialsService, CredentialKey

                async with AsyncSessionLocal() as db:
                    service = CredentialsService(db)
                    db_api_key = await service.get_credential(CredentialKey.OPENAI_API_KEY, fallback_env="OPENAI_API_KEY")
                    db_base_url = await service.get_credential(CredentialKey.OPENAI_BASE_URL, fallback_env="OPENAI_BASE_URL")
                    db_model = await service.get_credential(CredentialKey.OPENAI_MODEL, fallback_env="OPENAI_MODEL")

                if db_api_key:
                    self.api_key = db_api_key
                if db_base_url:
                    self.base_url = db_base_url
                if db_model:
                    self.model = db_model

                self.client = openai.AsyncOpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    timeout=httpx.Timeout(settings.LLM_CALL_TIMEOUT_SEC, connect=10.0),
                    max_retries=0,
                )
            except Exception as e:
                logger.warning(f"[LLMService] Failed to load DB credentials, using settings/env | error={e}")
            finally:
                self._credentials_loaded = True

    def invalidate_credentials_cache(self):
        self._credentials_loaded = False
        # Creds changed → drop routed clients built with the old key (PR2). The
        # FastAPI router calls this on credential edit; the Celery worker has no
        # such hook, so routed clients there rely on process recycle / restart.
        self.clear_client_cache()

    def _resolve_effort(
        self,
        node_key: Optional[str],
        thinking_effort: Optional[str],
    ) -> str:
        """Resolve the effective thinking_effort tier for this call.

        Priority (high → low):
          1. explicit `thinking_effort` arg
          2. settings.THINKING_EFFORT_OVERRIDES[node_key]  (kill-switch gated)
          3. self.anthropic_thinking_effort  (service instance default)
          4. 'xhigh'                          (final safety net)
        """
        candidates = [thinking_effort]
        if node_key and getattr(settings, 'ENABLE_PER_NODE_THINKING_EFFORT', True):
            overrides = getattr(settings, 'THINKING_EFFORT_OVERRIDES', None) or {}
            candidates.append(overrides.get(node_key))
        candidates.append(self.anthropic_thinking_effort)
        candidates.append('xhigh')
        for c in candidates:
            if c:
                return c.strip().lower()
        return 'xhigh'

    def _emit_metrics(
        self,
        node_key: Optional[str],
        effort: str,
        tokens: int,
        latency_ms: int,
        success: bool,
    ) -> None:
        """Push one per-call sample to metrics_tracker (best-effort)."""
        try:
            from backend.metrics_tracker import record_llm_call
            record_llm_call(
                node_key=node_key or "_unspecified",
                effort=effort,
                tokens=tokens,
                latency_ms=latency_ms,
                success=success,
            )
        except Exception:
            # metrics 累加失败不影响 LLM 主流程 — 这是侧效输出
            pass

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((openai.APIConnectionError, openai.RateLimitError))
    )
    async def call(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        json_mode: bool = True,
        max_tokens: int = 4096,
        *,
        node_key: Optional[str] = None,
        thinking_effort: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        _allow_fallback: bool = True,
    ) -> LLMResponse:
        """
        Make an LLM call with automatic retries and logging.

        Args:
            system_prompt: System message
            user_prompt: User message
            temperature: Sampling temperature
            json_mode: Whether to request JSON output
            max_tokens: Maximum response tokens
            node_key: Optional node identifier; Service consults
                settings.THINKING_EFFORT_OVERRIDES to pick per-node thinking
                effort. See `_resolve_effort` for full priority chain.
            thinking_effort: Optional explicit effort override; highest
                priority in the resolve chain (above node_key table lookup).

        Returns:
            LLMResponse with content and metadata
        """
        start_time = time.time()
        call_id = f"{int(start_time * 1000) % 100000}"

        # Per-call routing target (PR2). Explicit model/provider args win; else
        # consult the per-node map (resolve_model_for); else the construction
        # default. routed=None on the flag-OFF / no-match path → eff == default
        # → byte-for-byte legacy.
        routed = resolve_model_for(node_key) if (model is None and provider is None) else None
        eff_provider = (provider or (routed or {}).get("provider") or self.provider).lower()
        # Fall back to self.model when the provider-specific attr isn't set (e.g.
        # a test double built via __new__ that only sets .model / .provider).
        if eff_provider == "anthropic":
            eff_model = model or (routed or {}).get("model") or getattr(self, "anthropic_model", None) or self.model
        else:
            eff_model = model or (routed or {}).get("model") or getattr(self, "openai_model", None) or self.model
        eff_base_url = (routed or {}).get("base_url")
        eff_api_key_ref = (routed or {}).get("api_key_ref")
        # Entry-level thinking_effort override wins over the thinking_effort arg.
        eff_thinking = (routed or {}).get("thinking_effort") or thinking_effort

        # Per-scope circuit (2026-05-31 gap-1): keyed by (provider, endpoint,
        # model), so a 5xx storm on ONE model only fast-fails THAT model — other
        # nodes' models keep flowing (no cross-model brown-out).
        eff_scope = _llm_circuit_scope(eff_provider, eff_base_url, eff_model)
        routed_was_used = (eff_model != self.model) or (eff_provider != self.provider)

        # Phase 4 PR0 (Sprint 0, 2026-05-19): fast-fail when THIS target's circuit
        # is OPEN (provider hammering 5xx/timeout → don't burn another round-trip).
        # Soft-fail: Redis blip → is_open()=False → traffic flows.
        # gap-1: a ROUTED (non-default) target whose circuit is open does NOT
        # hard-fail — it falls back to the construction default ONCE (the default
        # rides its OWN, independent scope circuit), so the P1#6 fallback is no
        # longer gated by the routed model's outage. The fallback re-enters with
        # _allow_fallback=False → a default-target circuit-open is a genuine
        # fast-fail (no infinite recursion).
        if getattr(settings, "ENABLE_LLM_API_CIRCUIT", True) and _get_llm_circuit(eff_scope).is_open():
            if _allow_fallback and routed_was_used:
                logger.warning(
                    f"[LLMService] routed circuit OPEN scope={eff_scope} — falling "
                    f"back to default {self.provider}/{self.model} | id={call_id} "
                    f"node={node_key or '-'}"
                )
                return await self.call(
                    system_prompt, user_prompt, temperature=temperature,
                    json_mode=json_mode, max_tokens=max_tokens, node_key=node_key,
                    thinking_effort=thinking_effort,
                    model=self.model, provider=self.provider, _allow_fallback=False,
                )
            logger.warning(
                f"[LLMService] LLM circuit OPEN scope={eff_scope} — fast-fail | "
                f"id={call_id} node={node_key or '-'} (callers should treat as transient)"
            )
            return LLMResponse(
                content="",
                parsed=None,
                model=eff_model,
                tokens_used=0,
                latency_ms=0,
                success=False,
                error="llm_api_circuit_open",
            )

        # Resolve per-call effort. Only the anthropic branch consumes it; for an
        # openai-routed model it's computed for logging but ignored at request
        # time (thinking only fires under _anthropic_supports_thinking(eff_model)).
        effort_active = self._resolve_effort(node_key, eff_thinking)

        logger.debug(
            f"[LLMService] Call started | id={call_id} json_mode={json_mode} "
            f"node={node_key or '-'} effort={effort_active}"
        )

        # qwen / DashScope json_mode compat (2026-05-20): the DashScope
        # OpenAI-compatible endpoint HARD-REQUIRES the literal word "json"
        # somewhere in the messages whenever response_format=json_object is set
        # — otherwise it 400s ("'messages' must contain the word 'json' ... to
        # use 'response_format' of type 'json_object'"). OpenAI/Anthropic don't
        # enforce this, so prompts weren't guaranteed to include it. Inject a
        # minimal instruction when json_mode is on and neither prompt mentions
        # json. Harmless for every provider (it's just a clarifying directive).
        if json_mode and "json" not in (system_prompt + " " + user_prompt).lower():
            user_prompt = (
                f"{user_prompt}\n\nRespond with a single valid JSON object."
            )

        # JSON-mode parse retry: 1 extra attempt on JSONDecodeError. LLMs
        # occasionally truncate mid-string (provider hiccup / network abort);
        # cheap to reissue. Connection-level retries are handled by the
        # @retry decorator above. Non-json calls skip the loop.
        parse_attempts_max = 2 if json_mode else 1
        parse_error: Optional[json.JSONDecodeError] = None
        parsed = None
        content = ""
        tokens_used = 0
        finish_reason: Optional[str] = None

        try:
            await self._ensure_credentials_loaded()

            # Select the client for the effective target (after creds load so
            # self.api_key/base_url are resolved). Construction default (same
            # provider, no endpoint/key override) reuses self.client /
            # self.anthropic_client unchanged → byte-for-byte legacy; anything
            # else builds + caches a dedicated client (P0-2: openai→anthropic).
            if eff_provider == self.provider and not eff_base_url and not eff_api_key_ref:
                active_client = self.anthropic_client if eff_provider == "anthropic" else self.client
            else:
                active_client = self._get_client(eff_provider, eff_base_url, eff_api_key_ref)

            for parse_attempt in range(parse_attempts_max):
                # W5: Anthropic provider uses messages.create + system prompt with
                # cache_control. JSON mode is enforced by prompt instructions
                # (Anthropic doesn't have a response_format flag; the existing
                # prompts already say "Output Schema: JSON ...").
                if eff_provider == 'anthropic':
                    # Reasoning models (opus-4-7 family) reject `temperature`;
                    # only send it when the model still accepts it.
                    anth_kwargs: Dict[str, Any] = {
                        "model": eff_model,
                        "max_tokens": max_tokens,
                        "system": [{
                            "type": "text",
                            "text": system_prompt,
                            "cache_control": {"type": "ephemeral"},
                        }],
                        "messages": [{"role": "user", "content": user_prompt}],
                    }
                    if _anthropic_supports_temperature(eff_model):
                        anth_kwargs["temperature"] = temperature

                    # Extended thinking — opus-4-7 family only; caller's max_tokens
                    # is preserved as the *output* budget (thinking adds on top).
                    thinking_enabled = False
                    # `effort_active` already resolved via the three-tier priority
                    # chain above; alias-normalize "auto" → "adaptive" so downstream
                    # branches only need canonical tier names.
                    effort = _ANTHROPIC_EFFORT_ALIASES.get(effort_active, effort_active)
                    if (
                        _anthropic_supports_thinking(eff_model)
                        and effort
                        and effort != 'disabled'
                    ):
                        thinking_enabled = True
                        if effort == 'adaptive':
                            anth_kwargs['thinking'] = {
                                "type": "adaptive",
                                "display": "omitted",
                            }
                        else:
                            budget = _ANTHROPIC_THINKING_BUDGETS.get(
                                effort, _ANTHROPIC_THINKING_BUDGETS['xhigh']
                            )
                            # Spec: 1024 <= budget_tokens < max_tokens. Bump
                            # max_tokens so the original `max_tokens` arg is
                            # honored as output budget on top of thinking.
                            anth_kwargs['max_tokens'] = budget + max(max_tokens, 1024)
                            anth_kwargs['thinking'] = {
                                "type": "enabled",
                                "budget_tokens": budget,
                                "display": "omitted",
                            }

                    # The SDK forces streaming when total expected output exceeds
                    # its 10-minute non-streaming budget — that's always the case
                    # with thinking enabled at medium+ effort. Use the stream
                    # context manager and aggregate to the same final Message
                    # shape so the downstream code path stays identical.
                    if thinking_enabled:
                        async with active_client.messages.stream(**anth_kwargs) as stream:
                            # Hard deadline: stream aggregation can otherwise hang
                            # forever on a dead socket (no client timeout fires).
                            resp = await asyncio.wait_for(
                                stream.get_final_message(),
                                timeout=settings.LLM_STREAM_TIMEOUT_SEC,
                            )
                    else:
                        resp = await asyncio.wait_for(
                            active_client.messages.create(**anth_kwargs),
                            timeout=settings.LLM_CALL_TIMEOUT_SEC,
                        )
                    # Extract text from the first content block (TextBlock)
                    content = ""
                    for block in resp.content:
                        if getattr(block, "type", "") == "text":
                            content = block.text
                            break
                    if not content:
                        raise ValueError("Empty content in Anthropic response")
                    # Capture finish reason for diagnostic logging
                    finish_reason = getattr(resp, "stop_reason", None)
                    # Token accounting (input + output, log cache hit ratio).
                    # Accumulate across parse retries so the final tokens_used
                    # reflects total cost.
                    u = resp.usage
                    tokens_used += (u.input_tokens or 0) + (u.output_tokens or 0)
                    cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
                    cache_create = getattr(u, "cache_creation_input_tokens", 0) or 0
                    if cache_read or cache_create:
                        logger.debug(
                            f"[LLMService] Anthropic cache | id={call_id} "
                            f"input={u.input_tokens} cache_read={cache_read} "
                            f"cache_create={cache_create}"
                        )
                else:
                    # Hard deadline (asyncio.wait_for): ultimate backstop when the
                    # client/httpx timeout fails to fire — the 2026-05-21 zombie
                    # had the loop parked in select on this very await.
                    response = await asyncio.wait_for(
                        active_client.chat.completions.create(
                            model=eff_model,
                            messages=[
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt}
                            ],
                            temperature=temperature,
                            max_tokens=max_tokens,
                            response_format={"type": "json_object"} if json_mode else None
                        ),
                        timeout=settings.LLM_CALL_TIMEOUT_SEC,
                    )

                    # Defensive: handle empty/malformed responses
                    choices = getattr(response, "choices", None)
                    if not response or not choices:
                        status = getattr(response, "status", None)
                        msg = getattr(response, "msg", None)
                        extra = f" | status={status} msg={msg}" if status or msg else ""
                        raise ValueError(f"Empty response from LLM API{extra}")

                    if len(choices) == 0:
                        raise ValueError("Empty choices from LLM API")

                    message = response.choices[0].message
                    if not message:
                        raise ValueError("No message in LLM response")

                    content = message.content or ""
                    finish_reason = getattr(response.choices[0], "finish_reason", None)
                    if json_mode and not content.strip():
                        reasoning_content = getattr(message, "reasoning_content", None)
                        extra = f"finish_reason={finish_reason}" if finish_reason else ""
                        if reasoning_content:
                            extra = (extra + " | reasoning_content_present=True").strip()
                        raise ValueError(f"Empty content in LLM response ({extra})")
                    tokens_used += response.usage.total_tokens if response.usage else 0

                # Parse JSON if requested; retry once on JSONDecodeError.
                if not json_mode:
                    parse_error = None
                    break

                try:
                    parsed = json.loads(self._clean_json(content))
                    parse_error = None
                    break
                except json.JSONDecodeError as e:
                    parse_error = e
                    is_last = (parse_attempt + 1) >= parse_attempts_max
                    prefix = (
                        "JSON parse failed (final)"
                        if is_last
                        else f"JSON parse failed, retry {parse_attempt + 1}/{parse_attempts_max - 1}"
                    )
                    logger.warning(
                        f"[LLMService] {prefix} | id={call_id} node={node_key or '-'} "
                        f"len={len(content)} finish={finish_reason!r} "
                        f"head={content[:60]!r} error={e}"
                    )
                    if not is_last:
                        await asyncio.sleep(0.5)

            latency_ms = int((time.time() - start_time) * 1000)
            # Phase 4 PR0: LLM API call reached this point → provider returned
            # SOMETHING (content may be unparseable JSON, but the HTTP round-
            # trip succeeded). Reset fail counter + clear circuit if probing.
            # Provider-outage circuit cares about *transport-level* health,
            # not content quality. JSON parse failure stays a soft-failure
            # on the LLMResponse, but the circuit goes back to CLOSED.
            try:
                _llm_record_success(eff_scope)
            except Exception:
                pass
            # json_mode + parse_error = soft failure (content returned but
            # unparseable). success=False so callers can branch on .success
            # without re-checking .parsed.
            success_final = parse_error is None if json_mode else True

            if success_final:
                logger.info(
                    f"[LLMService] Call success | id={call_id} "
                    f"node={node_key or '-'} effort={effort_active} "
                    f"tokens={tokens_used} latency={latency_ms}ms"
                )
            # parse-fail warning already emitted inside the loop.
            self._emit_metrics(node_key, effort_active, tokens_used, latency_ms, success=success_final)

            # G2 Phase A (2026-05-19): record per-call cost telemetry into the
            # active round's contextvar accumulator (drained by mining_agent
            # at round exit via cost_tracker.flush_round_async). No-op when
            # ENABLE_COST_TELEMETRY=False or no active round context —
            # tracker is the recorder of last resort, never raises.
            try:
                from backend.cost_tracker import record_llm_call as _cost_record
                _cost_record(
                    model=eff_model,
                    provider=eff_provider,
                    effort=effort_active,
                    node_key=node_key,
                    tokens_total=tokens_used,
                    latency_ms=latency_ms,
                    success=success_final,
                    error_kind=("parse_error" if (json_mode and parse_error) else None),
                    call_id=call_id,
                )
            except Exception:
                pass

            return LLMResponse(
                content=content,
                parsed=parsed,
                model=eff_model,
                tokens_used=tokens_used,
                latency_ms=latency_ms,
                success=success_final,
                error=(f"JSON parse failed: {parse_error}" if parse_error else None),
            )

        except Exception as e:
            latency_ms = int((time.time() - start_time) * 1000)
            logger.error(
                f"[LLMService] Call failed | id={call_id} "
                f"node={node_key or '-'} effort={effort_active} "
                f"model={eff_provider}/{eff_model} error={e}"
            )
            # Phase 4 PR0: only API-level failures (5xx/timeout/connection)
            # increment the fail counter — JSON parse / ValueError / arg
            # errors are content issues and shouldn't trip the circuit.
            is_api_failure = False
            try:
                is_api_failure = _llm_error_is_api_failure(e)
                if is_api_failure:
                    _llm_record_fail(eff_scope, error_kind=type(e).__name__)
            except Exception:
                pass

            # Runtime fallback (P1#6): a ROUTED (non-default) model that fails at
            # the API level (provider down / model unavailable / endpoint key bad,
            # incl. an anthropic target whose client build raised) falls back to
            # the construction default ONCE. The recursive call passes explicit
            # model/provider=default → routed=None there → no second fallback, so
            # no infinite recursion. The routed model's failure was already
            # counted above for circuit/telemetry honesty. (`routed_was_used` was
            # computed once near the circuit check above.)
            if _allow_fallback and routed_was_used and is_api_failure:
                logger.warning(
                    f"[LLMService] routed {eff_provider}/{eff_model} failed "
                    f"({type(e).__name__}); falling back to default "
                    f"{self.provider}/{self.model} | id={call_id} node={node_key or '-'}"
                )
                return await self.call(
                    system_prompt, user_prompt, temperature=temperature,
                    json_mode=json_mode, max_tokens=max_tokens, node_key=node_key,
                    thinking_effort=thinking_effort,
                    model=self.model, provider=self.provider, _allow_fallback=False,
                )

            self._emit_metrics(node_key, effort_active, 0, latency_ms, success=False)

            # G2 Phase A: still record failed calls (0 tokens, success=False,
            # error_kind=exception class). Useful for the /ops/cost/telemetry
            # to surface failure-rate-per-node alongside cost — provider
            # outages currently invisible to operators.
            try:
                from backend.cost_tracker import record_llm_call as _cost_record
                _cost_record(
                    model=eff_model,
                    provider=eff_provider,
                    effort=effort_active,
                    node_key=node_key,
                    tokens_total=0,
                    latency_ms=latency_ms,
                    success=False,
                    error_kind=type(e).__name__[:40],
                    call_id=call_id,
                )
            except Exception:
                pass

            return LLMResponse(
                content="",
                model=eff_model,
                latency_ms=latency_ms,
                success=False,
                error=str(e)
            )
    
    async def call_with_schema(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: Type[BaseModel],
        temperature: float = 0.7
    ) -> tuple[Optional[BaseModel], LLMResponse]:
        """
        Call LLM and validate response against a Pydantic schema.
        
        Args:
            system_prompt: System message
            user_prompt: User message
            schema: Pydantic model class to validate against
            temperature: Sampling temperature
            
        Returns:
            Tuple of (parsed model or None, raw response)
        """
        response = await self.call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            json_mode=True
        )
        
        if not response.success or not response.parsed:
            return None, response
        
        try:
            validated = schema.model_validate(response.parsed)
            return validated, response
        except Exception as e:
            logger.warning(f"[LLMService] Schema validation failed | error={e}")
            return None, response
    
    def _clean_json(self, content: str) -> str:
        """Remove markdown blocks + trim text trailing the JSON object.

        OpenAI/Qwen with response_format=json_object guarantee pure JSON;
        Anthropic Claude doesn't have such a flag, so it occasionally emits
        natural-language commentary after the JSON object. We extract the
        first complete JSON object/array by brace-matching with string-aware
        escape handling.
        """
        content = content.strip()

        # Strip markdown fences
        if content.startswith('```json'):
            content = content[7:]
        elif content.startswith('```'):
            content = content[3:]
        if content.endswith('```'):
            content = content[:-3]
        content = content.strip()

        if not content or content[0] not in ('{', '['):
            return content

        opener = content[0]
        closer = '}' if opener == '{' else ']'
        depth = 0
        in_string = False
        escape = False
        for i, ch in enumerate(content):
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return content[: i + 1]
        # Unbalanced — return as-is so json.loads raises a clear error
        return content


# Singleton instance for reuse
_llm_service: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    """Get or create singleton LLM service."""
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service
