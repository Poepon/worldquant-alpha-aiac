"""G2 Phase A unit tests for cost_tracker (2026-05-19).

Coverage:
  - begin_round / end_round contextvar lifecycle
  - record_llm_call appends to active round, no-op when no context
  - record_llm_call is a no-op when ENABLE_COST_TELEMETRY=False (flag-off
    invariant; same byte-for-byte legacy pattern as P2-A/B/C/D)
  - record_llm_call never raises on bad input (exception-safety contract)
  - derive_cost_usd pricing lookup: exact match, longest-prefix match,
    unknown model → None, zero tokens → None
  - flush_round_async no-op when flag OFF / no context / empty calls
  - flush_round_async passes the expected ORM rows to db.add_all and
    commits (mock-DB style; the dedicated-log tables use BigInteger PK
    which aiosqlite doesn't autoincrement, so a real PG integration test
    covers the actual INSERT path)
  - flush_round_async clears the deque after success
  - flush_round_async is exception-safe: DB failure rolls back, clears
    deque, never re-raises
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend import cost_tracker as ct
from backend.config import settings
from backend.models import LLMCallLog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_round_ctx():
    """Each test starts with a clean contextvar slate."""
    token = ct._round_ctx.set(None)
    yield
    try:
        ct._round_ctx.reset(token)
    except (ValueError, LookupError):
        pass


@pytest.fixture
def _flag_on(monkeypatch):
    """Force ENABLE_COST_TELEMETRY=True on the active Settings instance."""
    monkeypatch.setattr(settings, "ENABLE_COST_TELEMETRY", True)
    yield


# ---------------------------------------------------------------------------
# Contextvar lifecycle
# ---------------------------------------------------------------------------


def test_begin_round_sets_context(_flag_on):
    tok = ct.begin_round(task_id=1, run_id=2, round_idx=3, dataset_id="pv1", pillar="momentum")
    try:
        ctx = ct.get_round_context()
        assert ctx is not None
        assert ctx.task_id == 1
        assert ctx.run_id == 2
        assert ctx.round_idx == 3
        assert ctx.dataset_id == "pv1"
        assert ctx.pillar == "momentum"
        assert ctx.calls == []
    finally:
        ct.end_round(tok)
    assert ct.get_round_context() is None


def test_end_round_with_none_token_is_safe():
    # Common pattern: callers always pass the begin_round return, but if a
    # bug supplies None we must not poison the contextvar.
    ct.end_round(None)


# ---------------------------------------------------------------------------
# record_llm_call
# ---------------------------------------------------------------------------


def test_record_llm_call_appends_to_active_round(_flag_on):
    tok = ct.begin_round(task_id=10, run_id=20, round_idx=1)
    try:
        ct.record_llm_call(
            model="deepseek-chat",
            provider="openai",
            node_key="hypothesis",
            tokens_total=1500,
            latency_ms=420,
            success=True,
        )
        ctx = ct.get_round_context()
        assert ctx is not None
        assert len(ctx.calls) == 1
        c = ctx.calls[0]
        assert c.model == "deepseek-chat"
        assert c.provider == "openai"
        assert c.node_key == "hypothesis"
        assert c.tokens_total == 1500
        assert c.latency_ms == 420
        assert c.success is True
    finally:
        ct.end_round(tok)


def test_record_llm_call_noop_without_context(_flag_on):
    # No begin_round called → no-op (sync jobs / ops scripts path)
    ct.record_llm_call(model="deepseek-chat", tokens_total=100)
    # Doesn't raise; no context to assert against.


def test_record_llm_call_noop_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_COST_TELEMETRY", False)
    tok = ct.begin_round(task_id=1, round_idx=1)
    try:
        ct.record_llm_call(model="deepseek-chat", tokens_total=1000)
        ctx = ct.get_round_context()
        assert ctx is not None
        # Flag OFF → call is ignored (contract: byte-for-byte legacy + zero
        # observable behaviour change beyond the begin/end syscalls)
        assert ctx.calls == []
    finally:
        ct.end_round(tok)


def test_record_llm_call_never_raises(_flag_on):
    tok = ct.begin_round(task_id=1, round_idx=1)
    try:
        # Intentionally bad input — model=None, tokens=None.
        # Sync recorder must not raise.
        ct.record_llm_call(model=None, tokens_total=None)  # type: ignore[arg-type]
    finally:
        ct.end_round(tok)


# ---------------------------------------------------------------------------
# derive_cost_usd pricing
# ---------------------------------------------------------------------------


def test_derive_cost_usd_exact_match():
    # deepseek-chat blended rate 0.00027 / 1k
    cost = ct.derive_cost_usd("deepseek-chat", 10_000)
    assert cost is not None
    assert cost == pytest.approx(0.00027 * 10, rel=1e-6)  # 10k tokens × 0.00027/1k


def test_derive_cost_usd_longest_prefix_match():
    # Made-up "deepseek-chat-v9" should match the deepseek-chat prefix entry
    cost = ct.derive_cost_usd("deepseek-chat-v9", 1000)
    assert cost is not None
    assert cost == pytest.approx(0.00027, rel=1e-6)


def test_derive_cost_usd_unknown_model_returns_none():
    cost = ct.derive_cost_usd("gpt-4-unknown", 1000)
    assert cost is None


def test_derive_cost_usd_zero_tokens_returns_none():
    assert ct.derive_cost_usd("deepseek-chat", 0) is None
    assert ct.derive_cost_usd("deepseek-chat", -5) is None


def test_derive_cost_usd_haiku_4_5():
    # Anthropic claude-haiku-4-5: 0.00125 / 1k
    cost = ct.derive_cost_usd("claude-haiku-4-5", 4000)
    assert cost == pytest.approx(0.00125 * 4, rel=1e-6)


# ---------------------------------------------------------------------------
# flush_round_async
# ---------------------------------------------------------------------------


def _make_mock_db(commit_raises: Exception | None = None) -> MagicMock:
    """Build a mock AsyncSession that captures add_all rows + records
    commit/rollback calls. Real PG INSERT path is covered by integration."""
    db = MagicMock()
    db.add_all = MagicMock()
    db.commit = AsyncMock(side_effect=commit_raises) if commit_raises else AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_flush_round_async_noop_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_COST_TELEMETRY", False)
    tok = ct.begin_round(task_id=1, round_idx=1)
    db = _make_mock_db()
    try:
        n = await ct.flush_round_async(db)
        assert n == 0
        db.add_all.assert_not_called()
        db.commit.assert_not_called()
    finally:
        ct.end_round(tok)


@pytest.mark.asyncio
async def test_flush_round_async_noop_without_context(_flag_on):
    db = _make_mock_db()
    n = await ct.flush_round_async(db)
    assert n == 0
    db.add_all.assert_not_called()


@pytest.mark.asyncio
async def test_flush_round_async_noop_when_no_calls(_flag_on):
    tok = ct.begin_round(task_id=1, round_idx=1)
    db = _make_mock_db()
    try:
        n = await ct.flush_round_async(db)
        assert n == 0
        db.add_all.assert_not_called()
    finally:
        ct.end_round(tok)


@pytest.mark.asyncio
async def test_flush_round_async_passes_correct_rows_to_db(_flag_on):
    tok = ct.begin_round(
        task_id=42, run_id=100, round_idx=5,
        dataset_id="fundamental6", pillar="value",
    )
    db = _make_mock_db()
    try:
        ct.record_llm_call(
            model="deepseek-chat", provider="openai", node_key="hypothesis",
            prompt_tokens=800, completion_tokens=200, tokens_total=1000,
            latency_ms=500, success=True, call_id="abc",
        )
        ct.record_llm_call(
            model="claude-haiku-4-5", provider="anthropic", node_key="code_gen",
            prompt_tokens=1200, completion_tokens=400, tokens_total=1600,
            latency_ms=800, success=True, effort="medium",
        )
        ctx_before = ct.get_round_context()
        assert ctx_before is not None
        assert len(ctx_before.calls) == 2

        n = await ct.flush_round_async(db)
        assert n == 2
        db.add_all.assert_called_once()
        db.commit.assert_awaited_once()

        rows = db.add_all.call_args.args[0]
        assert len(rows) == 2
        assert all(isinstance(r, LLMCallLog) for r in rows)

        r1, r2 = rows
        assert r1.task_id == 42 and r1.run_id == 100 and r1.round_idx == 5
        assert r1.dataset_id == "fundamental6" and r1.pillar == "value"
        assert r1.node_key == "hypothesis"
        assert r1.model == "deepseek-chat" and r1.provider == "openai"
        assert r1.tokens_total == 1000
        # cost = 0.00027 / 1k * 1000 = 0.00027
        assert r1.cost_usd == pytest.approx(0.00027, rel=1e-5)
        assert r1.success is True
        assert r1.call_id == "abc"

        assert r2.model == "claude-haiku-4-5" and r2.node_key == "code_gen"
        assert r2.tokens_total == 1600
        # cost = 0.00125 / 1k * 1600 = 0.002
        assert r2.cost_usd == pytest.approx(0.002, rel=1e-5)
        assert r2.effort == "medium"

        # Deque cleared
        ctx_after = ct.get_round_context()
        assert ctx_after is not None and ctx_after.calls == []
    finally:
        ct.end_round(tok)


@pytest.mark.asyncio
async def test_flush_round_async_records_failed_call_with_none_cost(_flag_on):
    tok = ct.begin_round(task_id=7, round_idx=1)
    db = _make_mock_db()
    try:
        ct.record_llm_call(
            model="some-unknown-model",
            node_key="self_correct",
            tokens_total=0,
            latency_ms=120,
            success=False,
            error_kind="APIConnectionError",
        )
        n = await ct.flush_round_async(db)
        assert n == 1
        rows = db.add_all.call_args.args[0]
        assert len(rows) == 1
        r = rows[0]
        assert r.success is False
        assert r.error_kind == "APIConnectionError"
        assert r.cost_usd is None  # unknown model AND zero tokens
        assert r.tokens_total == 0
    finally:
        ct.end_round(tok)


@pytest.mark.asyncio
async def test_flush_round_async_soft_fails_on_db_error(_flag_on):
    """A DB exception must drain the deque + log, never re-raise."""
    tok = ct.begin_round(task_id=1, round_idx=1)
    db = _make_mock_db(commit_raises=RuntimeError("connection lost"))
    try:
        ct.record_llm_call(model="deepseek-chat", tokens_total=100)

        n = await ct.flush_round_async(db)
        assert n == 0

        # rollback was called as the soft-fail path
        db.rollback.assert_awaited_once()

        # Deque must be drained even on failure (no resurrect on retry)
        ctx = ct.get_round_context()
        assert ctx is not None
        assert ctx.calls == []
    finally:
        ct.end_round(tok)
