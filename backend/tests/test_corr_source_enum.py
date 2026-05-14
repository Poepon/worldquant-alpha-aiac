"""V-27.158 — CorrSource enum unification.

The three correlation-service methods used to each carry their own ad-hoc
"where did this come from / could it be measured" vocabulary. calc_self_corr
and get_with_fallback are now unified onto CorrSource; calc_self_corr_by_window
keeps its own finer-grained per-window status set on purpose.
"""
import pytest

from backend.services.correlation_service import CorrSource, CorrelationService


def test_corr_source_str_compat():
    # StrEnum — old `== "local"` comparisons in callers stay valid.
    assert CorrSource.LOCAL == "local"
    assert CorrSource.BRAIN == "brain"
    assert CorrSource.BRAIN_PENDING == "brain_pending"
    assert CorrSource.UNKNOWN == "unknown"
    assert CorrSource.LOCAL in ("local", "brain")
    assert CorrSource.UNKNOWN != CorrSource.LOCAL
    # str() / f-string render the value (matters for log lines + JSONB).
    assert str(CorrSource.LOCAL) == "local"
    assert f"{CorrSource.UNKNOWN}" == "unknown"
    # V-27.126 added BRAIN_PENDING — members are exhaustive
    assert {s.value for s in CorrSource} == {
        "local", "brain", "brain_pending", "unknown"
    }


@pytest.mark.asyncio
async def test_calc_self_corr_unknown_on_empty_cache(tmp_path, monkeypatch):
    """calc_self_corr returns (None, CorrSource.UNKNOWN) when there's no OS
    PnL cache — the unified "not measurable" signal, no BRAIN needed."""
    import backend.services.correlation_service as cs

    # Point the cache dir at an empty tmp dir so _load_cache returns None.
    monkeypatch.setattr(cs, "CACHE_DIR", tmp_path)
    svc = CorrelationService(brain=None)

    corr, src = await svc.calc_self_corr("FAKE_ALPHA", "USA")
    assert corr is None
    assert src is CorrSource.UNKNOWN
