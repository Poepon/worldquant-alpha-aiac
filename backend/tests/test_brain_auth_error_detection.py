"""V-22.7 (2026-05-12) — BrainAdapter auth-error detection.

Spike on task 530 showed 3 consecutive simulate rounds returning 0 alphas
because BRAIN returned non-401 status codes with the body
"Incorrect authentication credentials" — bypassing the old status-only
re-auth trigger in `_request`. V-22.7 broadens `_is_auth_error` to also
match this body marker so the re-auth path fires regardless of status.
"""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from backend.adapters.brain_adapter import BrainAdapter


def _make_response(status: int, body: str = "", headers: dict | None = None) -> httpx.Response:
    """Construct a real httpx.Response for predicate testing without network."""
    return httpx.Response(
        status_code=status,
        content=body.encode("utf-8"),
        headers=headers or {},
    )


class TestIsAuthError:
    """V-22.7: _is_auth_error must catch 401 AND any response whose body
    contains the 'Incorrect authentication credentials' marker."""

    @pytest.fixture
    def adapter(self):
        # Bypass network/credential loading — only need the predicate
        with patch.object(BrainAdapter, "__init__", lambda self, **kw: None):
            return BrainAdapter()

    def test_401_status_is_auth_error(self, adapter):
        resp = _make_response(401, '{"detail":"Unauthorized"}')
        assert adapter._is_auth_error(resp) is True

    def test_body_marker_with_403_is_auth_error(self, adapter):
        # Real BRAIN observation: 403 with auth-credentials body
        body = '{"detail":"Incorrect authentication credentials."}'
        resp = _make_response(403, body)
        assert adapter._is_auth_error(resp) is True

    def test_body_marker_with_200_is_auth_error(self, adapter):
        """Edge case: BRAIN occasionally returns 200 with auth-error body.
        Caught by the short-body branch."""
        body = '{"detail":"Incorrect authentication credentials."}'
        resp = _make_response(200, body)
        assert adapter._is_auth_error(resp) is True

    def test_body_marker_with_400_is_auth_error(self, adapter):
        body = '{"detail":"Incorrect authentication credentials."}'
        resp = _make_response(400, body)
        assert adapter._is_auth_error(resp) is True

    def test_normal_200_is_not_auth_error(self, adapter):
        resp = _make_response(200, '{"id":"abc123","status":"PENDING"}')
        assert adapter._is_auth_error(resp) is False

    def test_normal_500_is_not_auth_error(self, adapter):
        resp = _make_response(500, '{"detail":"Internal server error"}')
        assert adapter._is_auth_error(resp) is False

    def test_403_without_auth_body_is_not_auth_error(self, adapter):
        """403 for non-auth reasons (e.g. multi-sim denied) must not
        trigger re-auth."""
        resp = _make_response(403, '{"detail":"Multi-simulation requires Consultant tier"}')
        assert adapter._is_auth_error(resp) is False

    def test_429_rate_limit_is_not_auth_error(self, adapter):
        resp = _make_response(429, '{"detail":"Too Many Requests"}')
        assert adapter._is_auth_error(resp) is False

    def test_empty_body_2xx_is_not_auth_error(self, adapter):
        resp = _make_response(202, "")
        assert adapter._is_auth_error(resp) is False

    def test_large_2xx_body_not_scanned(self, adapter):
        """Don't scan multi-KB success bodies — auth-error bodies are tiny."""
        big_body = "x" * 4096 + "Incorrect authentication credentials" + "y" * 4096
        resp = _make_response(200, big_body)
        # Body > 2048B AND status < 400 → skip body scan
        assert adapter._is_auth_error(resp) is False
