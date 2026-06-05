"""Pool budget counters (Phase 1b B3) — global daily sim + token ceilings.

Two Redis day-keyed counters, the single source of truth shared across the pool
(and, once the brain_adapter hook lands in B5, opt + auto-submit too):

  budget:sims:YYYYMMDD   — INCR'd ONLY after a SUCCESSFUL BRAIN POST (200/201/202
                           + Location); the brain_adapter success-branch hook is
                           B5. Pre-POST / slot-timeout / 429 / dedup-skip must
                           NOT count (the ``is_simulated != BRAIN-truth`` footgun
                           that caused 24% phantom early-stop — 终审 #6). Ceiling:
                           settings.BRAIN_DAILY_SIMULATE_LIMIT.
  budget:tokens:YYYYMMDD — LLM tokens (3-segment reserve/correct lands in B5).
                           Ceiling: settings.POOL_TOKEN_BUDGET_PER_DAY (separate
                           from MAX_TOKENS_PER_DAY, which macro owns).

Counters FAIL OPEN (redis blip → 0 → not exceeded → keep working); a runaway
backstop must not itself halt mining on a transient error. Keys auto-expire after
2 days so stale day-keys self-clean.
"""
from datetime import datetime, timezone

from loguru import logger

from backend.config import settings

_TTL_SECONDS = 2 * 24 * 3600


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _sims_key() -> str:
    return f"budget:sims:{_day()}"


def _tokens_key() -> str:
    return f"budget:tokens:{_day()}"


def _redis():
    from backend.tasks.redis_pool import get_redis_client  # lazy
    return get_redis_client()


def _get_int(key: str) -> int:
    try:
        v = _redis().get(key)
        return int(v) if v is not None else 0
    except Exception as ex:  # noqa: BLE001 — fail-open
        logger.debug(f"[pool.budget] get {key} redis error (fail-open 0): {ex}")
        return 0


def _incr(key: str, n: int) -> None:
    try:
        r = _redis()
        r.incrby(key, n)
        r.expire(key, _TTL_SECONDS)
    except Exception as ex:  # noqa: BLE001 — non-fatal
        logger.warning(f"[pool.budget] incr {key} by {n} failed (non-fatal): {ex}")


# --- sims ---
def sims_today() -> int:
    return _get_int(_sims_key())


def incr_sims(n: int = 1) -> None:
    """Count n SUCCESSFUL BRAIN sims (call ONLY after a confirmed POST success)."""
    _incr(_sims_key(), n)


def sims_budget_exceeded() -> bool:
    return sims_today() >= int(settings.BRAIN_DAILY_SIMULATE_LIMIT)


# --- tokens ---
def tokens_today() -> int:
    return _get_int(_tokens_key())


def incr_tokens(n: int) -> None:
    _incr(_tokens_key(), n)


def tokens_budget_exceeded() -> bool:
    return tokens_today() >= int(settings.POOL_TOKEN_BUDGET_PER_DAY)
