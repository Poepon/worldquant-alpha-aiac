"""BRAIN session re-auth coalescing tests (2026-05-21).

Regression guard for the 401-thrash that stalled task 3329 ~11h: multiple
`async with BrainAdapter()` call-sites across 3 solo Celery workers + uvicorn
each ran ensure_session()'s PROACTIVE refresh as a bare authenticate(), and —
because BRAIN is single-active-session — every fresh login evicted the cookie
the other processes were using, tripping BRAIN_AUTH_CIRCUIT in a loop.

Fix 1 (brain_adapter.ensure_session): proactive refresh now routes through
_coalesced_reauth (reload-shared-session-first + fleet/intra-process serialised
re-auth), so a healthy shared session is reused READ-ONLY and only ONE process
per window actually hits /authentication.

Fix 2 (credentials_service.test_brain_credentials): "test connection" verifies
via _coalesced_reauth instead of bare authenticate(), so clicking it no longer
mints a new session and evicts the running workers'.

All mocked — no PG/Redis/network.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_brain_class_state():
    """Isolate the class-level lock + credential cache between tests.

    _get_auth_lock() lazily binds asyncio.Lock() to the current loop; pytest-
    asyncio gives each test a fresh loop, so we must drop the stale lock or it
    raises "got Future attached to a different loop". Also save/restore the
    cached creds so credential-rotation tests don't leak into siblings.
    """
    from backend.adapters.brain_adapter import BrainAdapter
    saved = (
        BrainAdapter._auth_lock,
        BrainAdapter._cached_email,
        BrainAdapter._cached_password,
        BrainAdapter._credentials_loaded,
    )
    BrainAdapter._auth_lock = None
    yield
    (
        BrainAdapter._auth_lock,
        BrainAdapter._cached_email,
        BrainAdapter._cached_password,
        BrainAdapter._credentials_loaded,
    ) = saved


# ---------------------------------------------------------------------------
# Fix 1 — ensure_session proactive refresh routes through coalesced re-auth
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_session_invalid_routes_through_coalesced_reauth():
    """An invalid session must trigger _coalesced_reauth, NOT a bare
    authenticate() (the proactive thrash gap)."""
    from backend.adapters.brain_adapter import BrainAdapter

    adapter = BrainAdapter(email="e@x.com", password="pw")  # explicit → no DB load
    adapter.client = MagicMock()
    adapter._load_session_from_redis = AsyncMock(return_value=False)  # no short-circuit
    adapter._is_session_valid = AsyncMock(return_value=False)         # invalid
    adapter._coalesced_reauth = AsyncMock(return_value=True)
    adapter.authenticate = AsyncMock(return_value=True)

    await adapter.ensure_session()

    adapter._coalesced_reauth.assert_awaited_once()
    adapter.authenticate.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_session_redis_hit_short_circuits():
    """A healthy Redis session short-circuits — neither path re-auths."""
    from backend.adapters.brain_adapter import BrainAdapter

    adapter = BrainAdapter(email="e@x.com", password="pw")
    adapter.client = MagicMock()
    adapter._load_session_from_redis = AsyncMock(return_value=True)   # cache hit
    adapter._is_session_valid = AsyncMock(return_value=True)
    adapter._coalesced_reauth = AsyncMock(return_value=True)
    adapter.authenticate = AsyncMock(return_value=True)

    await adapter.ensure_session()

    adapter._coalesced_reauth.assert_not_called()
    adapter.authenticate.assert_not_called()


# ---------------------------------------------------------------------------
# _coalesced_reauth — healthy shared session reused read-only (no eviction)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_coalesced_reauth_reuses_healthy_shared_session_without_login():
    """When the shared Redis session validates, _coalesced_reauth returns
    True WITHOUT calling _distributed_reauth/authenticate — i.e. no new login,
    so a running worker's session is never evicted."""
    from backend.adapters.brain_adapter import BrainAdapter

    adapter = BrainAdapter(email="e@x.com", password="pw")
    adapter.client = MagicMock()
    adapter._reload_and_validate_quietly = AsyncMock(return_value=True)
    adapter._distributed_reauth = AsyncMock(return_value=True)
    adapter.authenticate = AsyncMock(return_value=True)

    ok = await adapter._coalesced_reauth()

    assert ok is True
    adapter._distributed_reauth.assert_not_called()
    adapter.authenticate.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_coalesced_reauth_reauths_once():
    """12 concurrent proactive refreshes coalesce to a SINGLE real re-auth:
    the intra-process _auth_lock serialises them and the reload-first check
    lets late waiters reuse the session the first one just refreshed."""
    from backend.adapters.brain_adapter import BrainAdapter

    adapter = BrainAdapter(email="e@x.com", password="pw")
    adapter.client = MagicMock()
    state = {"authed": False}

    async def _reload_validate():
        # Healthy only AFTER the first re-auth has run.
        return state["authed"]

    async def _distributed():
        await asyncio.sleep(0.01)  # widen the race window
        state["authed"] = True
        return True

    adapter._reload_and_validate_quietly = AsyncMock(side_effect=_reload_validate)
    adapter._distributed_reauth = AsyncMock(side_effect=_distributed)

    results = await asyncio.gather(*[adapter._coalesced_reauth() for _ in range(12)])

    assert all(results)
    assert adapter._distributed_reauth.await_count == 1


# ---------------------------------------------------------------------------
# Fix 2 — test_brain_credentials verifies without minting a new session
# ---------------------------------------------------------------------------

def _make_creds_service(email_val: str, pw_val: str):
    from backend.services.credentials_service import CredentialsService, CredentialKey

    svc = CredentialsService.__new__(CredentialsService)  # skip __init__ (needs db/key)

    async def _get_cred(key, fallback_env=None):
        return email_val if key == CredentialKey.BRAIN_EMAIL else pw_val

    svc.get_credential = _get_cred
    return svc


@pytest.mark.asyncio
async def test_test_brain_credentials_same_creds_reuses_session():
    """Testing the CURRENT (cached) credentials must verify via _coalesced_reauth
    and NOT bare authenticate() — no new BRAIN session, no worker eviction."""
    from backend.adapters.brain_adapter import BrainAdapter

    BrainAdapter._cached_email = "shared@x.com"
    BrainAdapter._cached_password = "sharedpw"
    svc = _make_creds_service("shared@x.com", "sharedpw")

    with patch.object(BrainAdapter, "get_client", AsyncMock(return_value=MagicMock())), \
         patch.object(BrainAdapter, "_coalesced_reauth", AsyncMock(return_value=True)) as m_coal, \
         patch.object(BrainAdapter, "authenticate", AsyncMock(return_value=True)) as m_auth, \
         patch.object(BrainAdapter, "_invalidate_session_cache", AsyncMock()) as m_inv:
        res = await svc.test_brain_credentials()

    assert res["success"] is True
    m_coal.assert_awaited_once()
    m_auth.assert_not_called()
    m_inv.assert_not_called()  # same creds → no rotation drop


@pytest.mark.asyncio
async def test_test_brain_credentials_rotation_drops_stale_session():
    """Rotated credentials (differ from cache) drop the stale shared session
    first so the verify exercises the NEW credentials."""
    from backend.adapters.brain_adapter import BrainAdapter

    BrainAdapter._cached_email = "OLD@x.com"
    BrainAdapter._cached_password = "oldpw"
    svc = _make_creds_service("new@x.com", "newpw")

    with patch.object(BrainAdapter, "get_client", AsyncMock(return_value=MagicMock())), \
         patch.object(BrainAdapter, "_coalesced_reauth", AsyncMock(return_value=True)) as m_coal, \
         patch.object(BrainAdapter, "authenticate", AsyncMock(return_value=True)) as m_auth, \
         patch.object(BrainAdapter, "_invalidate_session_cache", AsyncMock()) as m_inv, \
         patch.object(BrainAdapter, "invalidate_credentials_cache", MagicMock()) as m_cred_inv:
        res = await svc.test_brain_credentials()

    assert res["success"] is True
    m_coal.assert_awaited_once()
    m_auth.assert_not_called()
    m_inv.assert_awaited_once()
    m_cred_inv.assert_called_once()
