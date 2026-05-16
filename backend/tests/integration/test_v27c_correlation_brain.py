"""V-27 backlog C 段 — correlation / brain 边界完整性(Commit 1).

Covers V-27.157 / 102 / 128 / 129 / 126. All pure-mock — no DB / Redis / BRAIN.

Run:
    pytest backend/tests/integration/test_v27c_correlation_brain.py -v
"""
from __future__ import annotations

import json

import pytest

from backend.services.correlation_service import CorrelationService, CorrSource


class _FakeResp:
    def __init__(self, status_code, headers=None, body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self.text = "" if body is None else json.dumps(body)

    def json(self):
        return self._body or {}


def _make_brain_adapter():
    """A BrainAdapter instance without running __aenter__ / connecting."""
    from backend.adapters.brain_adapter import BrainAdapter
    return BrainAdapter()


# ---------------------------------------------------------------------------
# V-27.157 — _fetch_os_alpha_ids skips items missing `id`
# ---------------------------------------------------------------------------

class TestFetchOsAlphaIdsMissingId:
    @pytest.mark.asyncio
    async def test_skips_missing_id_keeps_rest(self):
        class _FakeBrain:
            async def get_user_alphas(self, limit, offset, stage):
                if offset > 0:
                    return {"results": []}
                return {
                    "results": [
                        {"id": "a1", "settings": {"region": "USA"}},
                        {"settings": {"region": "USA"}},          # missing id
                        {"id": None, "settings": {"region": "USA"}},  # null id
                        {"id": "a2", "settings": {"region": "USA"}},
                        {"id": "a3", "settings": {"region": "EUR"}},  # wrong region
                    ]
                }

        svc = CorrelationService(_FakeBrain())
        ids = await svc._fetch_os_alpha_ids("USA")
        # missing/null id skipped, wrong region filtered — no KeyError crash
        assert ids == ["a1", "a2"]


# ---------------------------------------------------------------------------
# V-27.102 — submit_alpha accepts any 2xx
# ---------------------------------------------------------------------------

class TestSubmitAlphaAccepts2xx:
    @pytest.mark.asyncio
    async def test_201_is_success(self, monkeypatch):
        ba = _make_brain_adapter()

        async def _fake_call(method, path, **kw):
            # no Retry-After → BRAIN job terminal immediately
            return _FakeResp(201, headers={}, body={"ok": True})

        monkeypatch.setattr(ba, "_safe_api_call", _fake_call)
        result = await ba.submit_alpha("test-alpha")
        assert result["success"] is True
        assert result["status_code"] == 201

    @pytest.mark.asyncio
    async def test_400_is_failure(self, monkeypatch):
        ba = _make_brain_adapter()

        async def _fake_call(method, path, **kw):
            return _FakeResp(400, headers={}, body={"error": "bad"})

        monkeypatch.setattr(ba, "_safe_api_call", _fake_call)
        result = await ba.submit_alpha("test-alpha")
        assert result["success"] is False
        assert result["status_code"] == 400


# ---------------------------------------------------------------------------
# V-27.128 — get_alpha_pnl goes through _safe_api_call (rate-limit handling)
# ---------------------------------------------------------------------------

class TestGetAlphaPnlSafeApiCall:
    @pytest.mark.asyncio
    async def test_uses_safe_api_call_not_request(self, monkeypatch):
        ba = _make_brain_adapter()
        seen: dict = {}

        async def _fake_safe(method, path, **kw):
            seen["path"] = path
            seen["method"] = method
            return _FakeResp(200, body={"records": []})

        async def _fake_request(*a, **kw):
            seen["used_request"] = True
            return _FakeResp(200)

        monkeypatch.setattr(ba, "_safe_api_call", _fake_safe)
        monkeypatch.setattr(ba, "_request", _fake_request)
        out = await ba.get_alpha_pnl("test-alpha")
        assert out == {"records": []}
        assert seen["path"] == "/alphas/test-alpha/recordsets/pnl"
        assert seen["method"] == "GET"
        # must NOT fall back to the rate-limit-unaware _request path
        assert "used_request" not in seen


# ---------------------------------------------------------------------------
# V-27.129 — _fetch_pnl_series returns an empty Series on all-failure (no raise)
# ---------------------------------------------------------------------------

class TestFetchPnlSeriesUnifiedReturn:
    @pytest.mark.asyncio
    async def test_all_exceptions_return_empty_no_raise(self, monkeypatch):
        import backend.services.correlation_service as cs

        class _FakeBrain:
            async def get_alpha_pnl(self, alpha_id):
                raise ConnectionError("boom")

        async def _no_sleep(_):
            return None

        monkeypatch.setattr(cs.asyncio, "sleep", _no_sleep)
        svc = CorrelationService(_FakeBrain())
        series = await svc._fetch_pnl_series("test-alpha", max_attempts=3)
        # V-27.129: must NOT raise — unified empty-Series return
        assert series.empty

    @pytest.mark.asyncio
    async def test_all_empty_return_empty(self, monkeypatch):
        import backend.services.correlation_service as cs

        class _FakeBrain:
            async def get_alpha_pnl(self, alpha_id):
                return {}  # empty payload, no exception

        async def _no_sleep(_):
            return None

        monkeypatch.setattr(cs.asyncio, "sleep", _no_sleep)
        svc = CorrelationService(_FakeBrain())
        series = await svc._fetch_pnl_series("test-alpha", max_attempts=3)
        assert series.empty


# ---------------------------------------------------------------------------
# V-27.126 — get_with_fallback distinguishes BRAIN_PENDING from UNKNOWN
# ---------------------------------------------------------------------------

class TestGetWithFallbackBrainPending:
    @pytest.mark.asyncio
    async def test_max_none_is_brain_pending(self):
        class _FakeBrain:
            async def check_correlation(self, alpha_id, check_type):
                return {"max": None}  # accepted, still computing

        svc = CorrelationService(_FakeBrain())
        corr, src = await svc.get_with_fallback("test-alpha", region="ZZ1")
        assert corr is None
        assert src == CorrSource.BRAIN_PENDING

    @pytest.mark.asyncio
    async def test_exception_is_unknown(self):
        class _FakeBrain:
            async def check_correlation(self, alpha_id, check_type):
                raise ConnectionError("boom")

        svc = CorrelationService(_FakeBrain())
        corr, src = await svc.get_with_fallback("test-alpha", region="ZZ1")
        assert corr is None
        assert src == CorrSource.UNKNOWN

    @pytest.mark.asyncio
    async def test_real_max_is_brain(self):
        class _FakeBrain:
            async def check_correlation(self, alpha_id, check_type):
                return {"max": 0.42}

        svc = CorrelationService(_FakeBrain())
        corr, src = await svc.get_with_fallback("test-alpha", region="ZZ1")
        assert corr == pytest.approx(0.42)
        assert src == CorrSource.BRAIN


# ---------------------------------------------------------------------------
# P3-Brain — check_correlation shape change tolerance
# ---------------------------------------------------------------------------
# 2026-05-16: BrainAdapter.check_correlation switched return shape from
# {"max": ...} to {"status_code": int, "data": {"max": ...}}. Pin the
# CorrelationService + raw-brain caller path against the new shape so a
# future re-break is caught here, not in production.

class TestCheckCorrelationShapeP3Brain:
    @pytest.mark.asyncio
    async def test_get_with_fallback_accepts_new_shape_max_present(self):
        class _NewShapeBrain:
            async def check_correlation(self, alpha_id, check_type):
                return {"status_code": 200, "data": {"max": 0.42}}

        svc = CorrelationService(_NewShapeBrain())
        corr, src = await svc.get_with_fallback("test-alpha", region="ZZ1")
        assert corr == pytest.approx(0.42)
        assert src == CorrSource.BRAIN

    @pytest.mark.asyncio
    async def test_get_with_fallback_accepts_new_shape_max_none(self):
        class _NewShapeBrain:
            async def check_correlation(self, alpha_id, check_type):
                return {"status_code": 200, "data": {"max": None}}

        svc = CorrelationService(_NewShapeBrain())
        corr, src = await svc.get_with_fallback("test-alpha", region="ZZ1")
        assert corr is None
        assert src == CorrSource.BRAIN_PENDING

    @pytest.mark.asyncio
    async def test_evaluation_prod_corr_reads_new_shape(self):
        # node_simulate / node_evaluate path: `prod_corr_result.get(...)`
        # extraction must also handle the new shape. Reproduces the same
        # branching used in agents/graph/nodes/evaluation.py:407-415.
        result = {"status_code": 200, "data": {"max": 0.37}}
        data = (
            result["data"]
            if "status_code" in result and isinstance(result.get("data"), dict)
            else result
        )
        prod_corr = float(data.get("max", 0.0) or 0.0)
        assert prod_corr == pytest.approx(0.37)

    @pytest.mark.asyncio
    async def test_real_adapter_check_correlation_returns_new_shape(self):
        # Pin the adapter contract — if anyone reverts to the bare-dict shape,
        # this test fires before correlation_service silently breaks.
        from backend.adapters.brain_adapter import BrainAdapter

        adapter = BrainAdapter()

        async def _fake_request(method, url, **kwargs):
            return _FakeResp(200, body={"max": 0.5})

        adapter._request = _fake_request
        out = await adapter.check_correlation("a-1", "PROD")
        assert set(out.keys()) == {"status_code", "data"}
        assert out["status_code"] == 200
        assert out["data"] == {"max": 0.5}
