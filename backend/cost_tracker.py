"""G2 Phase A — per-LLM-call cost tracker (2026-05-19).

Light-wiring (per [[feedback_light_wiring_deferred_gate]]) telemetry layer
for capturing every LLMService.call into the llm_call_log table.

Design:
  * Per-round contextvar carrying (task_id, run_id, round_idx, dataset_id,
    pillar) — set by mining_agent at round entry, cleared at round exit.
  * Per-round in-memory deque accumulating each LLM call's raw stats
    (model, provider, effort, prompt/completion tokens, latency, success).
  * Single batched INSERT at round end via ``flush_round_async`` —
    typical round has 6-20 LLM calls so we trade per-call DB latency for
    one bulk write at the round boundary.
  * Hot-path entry point ``record_llm_call`` is sync, allocation-light,
    and exception-safe — a tracker bug must NEVER break the LLM call path
    (matches metrics_tracker.record_llm_call contract).
  * Flag-gated: when settings.ENABLE_COST_TELEMETRY is False, ``record_*``
    no-ops in ~10ns and ``flush_round_async`` skips the DB roundtrip.

Outside-of-round calls (sync jobs, ops scripts) still get a row with
task_id/run_id/round_idx=NULL — useful for the macro narrative LLM batch
+ R5 LLM judge sweep that drives daily cost without a mining round.
"""

from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass, field
from typing import List, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings


# ---------------------------------------------------------------------------
# Contextvar — set at round entry, cleared at round exit
# ---------------------------------------------------------------------------


@dataclass
class RoundContext:
    """Identifies the mining round currently consuming LLM calls.

    All fields except ``calls`` are immutable for the lifetime of the round.
    ``calls`` is the in-memory accumulator drained by ``flush_round_async``.
    """

    task_id: Optional[int] = None
    run_id: Optional[int] = None
    round_idx: Optional[int] = None
    dataset_id: Optional[str] = None
    pillar: Optional[str] = None
    calls: List["LLMCallStats"] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)


@dataclass
class LLMCallStats:
    """One LLM call's raw stats (cost_usd derived at flush time)."""

    model: str
    provider: Optional[str]
    effort: Optional[str]
    node_key: Optional[str]
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    tokens_total: int
    latency_ms: Optional[int]
    success: bool
    error_kind: Optional[str]
    call_id: Optional[str]


_round_ctx: contextvars.ContextVar[Optional[RoundContext]] = contextvars.ContextVar(
    "_g2_cost_round_ctx", default=None,
)


# ---------------------------------------------------------------------------
# Context lifecycle
# ---------------------------------------------------------------------------


def begin_round(
    *,
    task_id: Optional[int] = None,
    run_id: Optional[int] = None,
    round_idx: Optional[int] = None,
    dataset_id: Optional[str] = None,
    pillar: Optional[str] = None,
) -> Optional[contextvars.Token]:
    """Set the active round context. Returns the token from
    ContextVar.set() — pass it to ``end_round`` to restore the prior value.

    Always returns a token (even when ENABLE_COST_TELEMETRY=False) so the
    caller's try/finally pattern stays consistent.
    """
    ctx = RoundContext(
        task_id=task_id,
        run_id=run_id,
        round_idx=round_idx,
        dataset_id=dataset_id,
        pillar=pillar,
    )
    return _round_ctx.set(ctx)


def end_round(token: Optional[contextvars.Token]) -> None:
    """Restore the prior round context. Safe to call with None (no-op)."""
    if token is not None:
        try:
            _round_ctx.reset(token)
        except (ValueError, LookupError):
            # Reset on a different context — rare in async edge cases; silent
            # no-op so we don't poison the caller's finally block.
            pass


def get_round_context() -> Optional[RoundContext]:
    return _round_ctx.get()


# ---------------------------------------------------------------------------
# Hot-path recorder — sync, allocation-light, exception-safe
# ---------------------------------------------------------------------------


def record_llm_call(
    *,
    model: str,
    provider: Optional[str] = None,
    effort: Optional[str] = None,
    node_key: Optional[str] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    tokens_total: int = 0,
    latency_ms: Optional[int] = None,
    success: bool = True,
    error_kind: Optional[str] = None,
    call_id: Optional[str] = None,
) -> None:
    """Append one LLM call's stats to the active round context.

    No-op when ENABLE_COST_TELEMETRY=False or no round context is active —
    in that case callers outside of a mining round (sync jobs, ops scripts)
    silently skip persistence. Phase B may add a fallback "orphan" round
    bucket; Phase A doesn't.

    NEVER raises — a tracker bug must not break the LLM hot path.
    """
    try:
        if not getattr(settings, "ENABLE_COST_TELEMETRY", False):
            return
        ctx = _round_ctx.get()
        if ctx is None:
            return
        ctx.calls.append(
            LLMCallStats(
                model=model,
                provider=provider,
                effort=effort,
                node_key=node_key,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                tokens_total=int(tokens_total or 0),
                latency_ms=latency_ms,
                success=bool(success),
                error_kind=error_kind,
                call_id=call_id,
            )
        )
    except Exception:
        # 严格不抛 — 与 metrics_tracker.record_llm_call 同契约
        pass


# ---------------------------------------------------------------------------
# Cost derivation
# ---------------------------------------------------------------------------


def _pricing_lookup(model: str) -> Optional[float]:
    """Resolve blended per-1k-token USD price by model prefix.

    Exact match wins; falls back to longest-prefix match so future
    deepseek-chat-vX rolls into the deepseek-chat entry without config
    change. Returns None when no prefix matches — caller writes
    cost_usd=NULL but tokens are still recorded for retrospective recompute.
    """
    table = getattr(settings, "LLM_PRICING_USD_PER_1K_TOKENS", None) or {}
    if not table or not model:
        return None
    if model in table:
        v = table[model]
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    # Longest-prefix match — pick the most specific entry
    best: Optional[str] = None
    for key in table.keys():
        if model.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    if best is None:
        return None
    try:
        return float(table[best])
    except (TypeError, ValueError):
        return None


def derive_cost_usd(model: str, tokens_total: int) -> Optional[float]:
    """tokens_total * (price/1000). Returns None when model has no price."""
    price = _pricing_lookup(model)
    if price is None or tokens_total <= 0:
        return None
    return round(price * tokens_total / 1000.0, 6)


# ---------------------------------------------------------------------------
# Batched flush at round boundary
# ---------------------------------------------------------------------------


async def flush_round_async(db: AsyncSession) -> int:
    """Drain the active round's accumulated calls into llm_call_log via one
    INSERT batch. Returns the number of rows written.

    Soft-fail: any DB exception is logged + rolled back, the in-memory deque
    is cleared so the next round starts fresh. Never re-raises.

    Called by mining_agent.run_evolution_loop at round exit. Safe to call
    repeatedly; clears the deque on each call.
    """
    if not getattr(settings, "ENABLE_COST_TELEMETRY", False):
        return 0
    ctx = _round_ctx.get()
    if ctx is None or not ctx.calls:
        return 0
    drained = list(ctx.calls)
    ctx.calls.clear()

    try:
        from backend.models import LLMCallLog
        rows = []
        for c in drained:
            cost = derive_cost_usd(c.model, c.tokens_total)
            rows.append(
                LLMCallLog(
                    task_id=ctx.task_id,
                    run_id=ctx.run_id,
                    round_idx=ctx.round_idx,
                    dataset_id=ctx.dataset_id,
                    pillar=ctx.pillar,
                    node_key=c.node_key,
                    model=c.model,
                    provider=c.provider,
                    effort=c.effort,
                    prompt_tokens=c.prompt_tokens,
                    completion_tokens=c.completion_tokens,
                    tokens_total=c.tokens_total,
                    cost_usd=cost,
                    latency_ms=c.latency_ms,
                    success=c.success,
                    error_kind=c.error_kind,
                    call_id=c.call_id,
                )
            )
        if rows:
            db.add_all(rows)
            await db.commit()
            logger.info(
                f"[cost_tracker] flush_round task={ctx.task_id} run={ctx.run_id} "
                f"round={ctx.round_idx} dataset={ctx.dataset_id} n={len(rows)}"
            )
        return len(rows)
    except Exception as e:
        logger.warning(
            f"[cost_tracker] flush_round failed (non-fatal): "
            f"{type(e).__name__}: {e} | dropped {len(drained)} rows"
        )
        try:
            await db.rollback()
        except Exception:
            pass
        return 0
