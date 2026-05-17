"""FeatureFlagService — runtime ENABLE_* flag store backing /ops/feature-flags.

Source: docs/alphagbm_skills_research_2026-05-15.md, ops dashboard plan §1.4.

Architecture
------------
Three layers, in priority order on read::

    settings.ENABLE_X  →  __getattribute__ hook in backend/config.py  →
        _flag_override_cache (module-level dict, refreshed every 60s) →
            DB row in `feature_flag_overrides` (durable source of truth)
                (Redis hash `aiac:feature_flags:v1` is *only* a short-lived
                 cross-process invalidation hint; DB is authoritative)

Read fallback: if Redis is down we still serve from DB. If DB is also down
we fall back to env defaults (the hook's super().__getattribute__ path).
The system NEVER crashes on a flag read.

Whitelist
---------
Only flags listed in :data:`SUPPORTED_FLAGS` may be overridden. The keys
must match the attribute names on :class:`backend.config.Settings` exactly,
otherwise the override is silently ignored on read. New flags must be
added here AND in ``Settings``; we don't auto-discover to avoid letting
the ops console flip arbitrary settings (e.g. SHARPE_MIN).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select

from backend.models.config import FeatureFlagAudit, FeatureFlagOverride
from backend.services.base import BaseService, transactional

logger = logging.getLogger("services.feature_flag")


# ---------------------------------------------------------------------------
# Whitelist + types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FlagSpec:
    """Description of a single overridable flag."""
    name: str
    flag_type: str              # one of FLAG_TYPES
    group: str                  # used by frontend to group the table
    description: str


FLAG_TYPES = ("bool", "int", "float", "str", "json")


# Source of truth for what /ops/feature-flags is allowed to flip. Keep
# alphabetically grouped by P-tier so the rendered table stays stable.
SUPPORTED_FLAGS: Dict[str, FlagSpec] = {
    # --- P0 ---
    "ENABLE_SIGNAL_CONTROL_DUAL_RUN": FlagSpec(
        name="ENABLE_SIGNAL_CONTROL_DUAL_RUN",
        flag_type="bool",
        group="P0",
        description="信号-对照双跑 (额外消耗 BRAIN 模拟配额;评估归因更准)",
    ),
    # --- P1 ---
    "ENABLE_GRADED_SCORE": FlagSpec(
        name="ENABLE_GRADED_SCORE",
        flag_type="bool",
        group="P1",
        description="百分位归一化评分 (5 档 A-E)",
    ),
    "ENABLE_ROBUSTNESS_CHECK": FlagSpec(
        name="ENABLE_ROBUSTNESS_CHECK",
        flag_type="bool",
        group="P1",
        description="What-if 参数扰动鲁棒性门 (增加 ~N 次 simulate)",
    ),
    # --- P2-A 宏观叙事 ---
    "ENABLE_MACRO_NARRATIVE_GUIDANCE": FlagSpec(
        name="ENABLE_MACRO_NARRATIVE_GUIDANCE",
        flag_type="bool",
        group="P2-A",
        description="LLM prompt 注入 macro narrative 段 (引导 economic-mechanism 生成)",
    ),
    "ENABLE_MACRO_NARRATIVE_EXTRACT": FlagSpec(
        name="ENABLE_MACRO_NARRATIVE_EXTRACT",
        flag_type="bool",
        group="P2-A",
        description="每日 10:00 SH LLM 批生成长尾 narrative (消耗 token)",
    ),
    # --- P2-B 五支柱平衡 ---
    "ENABLE_PILLAR_AWARE_SELECTION": FlagSpec(
        name="ENABLE_PILLAR_AWARE_SELECTION",
        flag_type="bool",
        group="P2-B",
        description="hypothesis 节点根据 deficit 给出 pillar nudge",
    ),
    # --- P2-C 市场体制 ---
    "ENABLE_REGIME_INFERENCE": FlagSpec(
        name="ENABLE_REGIME_INFERENCE",
        flag_type="bool",
        group="P2-C",
        description="每日 10:30 SH 推断 regime + 写 Redis cache",
    ),
    "ENABLE_REGIME_AWARE_THRESHOLDS": FlagSpec(
        name="ENABLE_REGIME_AWARE_THRESHOLDS",
        flag_type="bool",
        group="P2-C",
        description="按 regime 倍率应用 sharpe/fitness/turnover 阈值",
    ),
    "ENABLE_STYLE_PRESET_GUIDANCE": FlagSpec(
        name="ENABLE_STYLE_PRESET_GUIDANCE",
        flag_type="bool",
        group="P2-C",
        description="hypothesis 节点注入 regime style preset 投资哲学",
    ),
    # --- P2-D 负向知识 ---
    "ENABLE_NEGATIVE_KNOWLEDGE_NUDGE": FlagSpec(
        name="ENABLE_NEGATIVE_KNOWLEDGE_NUDGE",
        flag_type="bool",
        group="P2-D",
        description="hypothesis prompt 加近期 top pitfalls 警告段",
    ),
    # --- P3-Brain 角色切换 ---
    "ENABLE_BRAIN_CONSULTANT_MODE": FlagSpec(
        name="ENABLE_BRAIN_CONSULTANT_MODE",
        flag_type="bool",
        group="P3-Brain",
        description="BRAIN Consultant 模式 — 解锁 multi-sim/PROD-corr/全球 region/Sharpe≥1.58。仅在收到 BRAIN 升级邮件后翻。",
    ),
    # --- Phase 0 R1a ---
    "ENABLE_R1A_HOOK": FlagSpec(
        name="ENABLE_R1A_HOOK",
        flag_type="bool",
        group="Phase0-R1a",
        description="启用 enhance_existing_node_evaluate shim,把 AttributionType 写入 alpha.metrics 供 Phase 1 R2/Q7 bandit arm-set 反证。≥200 触发观察期门槛。",
    ),
}


# Redis hash key + TTL — only used to bump cross-process refreshers; DB is
# authoritative.
REDIS_FLAGS_KEY = "aiac:feature_flags:v1"
REDIS_FLAGS_BUMP_KEY = "aiac:feature_flags:bump"
REDIS_FLAGS_TTL = 86400  # 24h


# ---------------------------------------------------------------------------
# Read/write models exposed to router
# ---------------------------------------------------------------------------

@dataclass
class FlagState:
    """Effective state of one flag returned to /ops/flags."""
    name: str
    flag_type: str
    group: str
    description: str
    env_default: Any                  # value from Settings before override
    override_value: Optional[Any]     # decoded DB value, or None if no override
    effective_value: Any              # what callers actually see
    source: str                       # "env" | "runtime-override" | "default"
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# Module-level cache — single source of truth lives in backend/config.py
# ---------------------------------------------------------------------------
# Re-exported here so call sites can keep importing from this service module
# without knowing about the config-internal implementation. The
# Settings.__getattribute__ hook reads the same dict, so write-through here
# is visible to settings.ENABLE_X immediately in the same process.
from backend.config import _flag_override_cache  # noqa: E402  (intentional late import)


def _decode_value(raw: str, flag_type: str) -> Any:
    """Decode a JSON-encoded `flag_value` string per declared type."""
    parsed = json.loads(raw)
    if flag_type == "bool":
        return bool(parsed)
    if flag_type == "int":
        return int(parsed)
    if flag_type == "float":
        return float(parsed)
    if flag_type == "str":
        return str(parsed)
    return parsed  # json — keep as-is


def _encode_value(value: Any, flag_type: str) -> str:
    """JSON-encode a value, validating it matches the declared type.

    Note bool is a subclass of int in Python — guard int/float against
    accidentally accepting True/False (would silently encode as JSON `true`
    and decode back as 1, drifting behaviour without an error).
    """
    if flag_type == "bool" and not isinstance(value, bool):
        raise ValueError(f"expected bool, got {type(value).__name__}")
    if flag_type == "int" and (isinstance(value, bool) or not isinstance(value, int)):
        raise ValueError(f"expected int, got {type(value).__name__}")
    if flag_type == "float" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise ValueError(f"expected number, got {type(value).__name__}")
    if flag_type == "str" and not isinstance(value, str):
        raise ValueError(f"expected str, got {type(value).__name__}")
    # json — anything serializable
    return json.dumps(value)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class FeatureFlagService(BaseService):
    """DB-backed feature flag store with Redis cross-process invalidation.

    The router layer (backend/routers/ops.py) constructs one of these per
    request. Background refreshers (lifespan + worker_process_init) load
    overrides directly via :meth:`load_overrides_into_cache` without going
    through the router.
    """

    # ---- read -------------------------------------------------------------

    async def list_all(self) -> List[FlagState]:
        """Return effective state for every supported flag.

        This is the only call site that reads `Settings` env defaults
        directly — we want the raw env value, not the post-override one,
        so the UI can show both sides side-by-side.
        """
        from backend.config import settings  # lazy to avoid import cycle

        # Pull all overrides in a single query keyed by name
        rows = (await self.db.execute(select(FeatureFlagOverride))).scalars().all()
        overrides_by_name = {r.flag_name: r for r in rows}

        out: List[FlagState] = []
        for spec in SUPPORTED_FLAGS.values():
            # Read env default by going around our own __getattribute__ hook
            env_default = object.__getattribute__(settings, spec.name) \
                if hasattr(settings, spec.name) else None

            row = overrides_by_name.get(spec.name)
            if row is not None:
                try:
                    decoded = _decode_value(row.flag_value, spec.flag_type)
                    out.append(FlagState(
                        name=spec.name,
                        flag_type=spec.flag_type,
                        group=spec.group,
                        description=spec.description,
                        env_default=env_default,
                        override_value=decoded,
                        effective_value=decoded,
                        source="runtime-override",
                        updated_at=row.updated_at,
                        updated_by=row.updated_by,
                        note=row.note,
                    ))
                    continue
                except Exception as ex:
                    logger.warning(
                        "[feature_flag] decode failed for %s (%s) — falling back to env default: %s",
                        spec.name, row.flag_value, ex,
                    )

            out.append(FlagState(
                name=spec.name,
                flag_type=spec.flag_type,
                group=spec.group,
                description=spec.description,
                env_default=env_default,
                override_value=None,
                effective_value=env_default,
                source="env" if env_default is not None else "default",
            ))
        return out

    async def get_one(self, name: str) -> Optional[FlagState]:
        """Return a single flag's FlagState (env_default + override + updated_at/by).

        Used by /ops/brain/role-state to fetch the last-switched timestamp for
        ENABLE_BRAIN_CONSULTANT_MODE. O(1) — direct row query, no full table scan.
        Returns None when name is not in SUPPORTED_FLAGS.
        """
        spec = SUPPORTED_FLAGS.get(name)
        if spec is None:
            return None
        from backend.config import settings  # lazy
        env_default = object.__getattribute__(settings, name) if hasattr(settings, name) else None
        row = (await self.db.execute(
            select(FeatureFlagOverride).where(FeatureFlagOverride.flag_name == name)
        )).scalar_one_or_none()
        if row is None:
            return FlagState(
                name=name, flag_type=spec.flag_type, group=spec.group,
                description=spec.description, env_default=env_default,
                override_value=None, effective_value=env_default,
                source="env" if env_default is not None else "default",
            )
        try:
            decoded = _decode_value(row.flag_value, spec.flag_type)
        except Exception as ex:
            logger.warning(
                "[feature_flag] get_one decode failed for %s — falling back to env default: %s",
                name, ex,
            )
            return FlagState(
                name=name, flag_type=spec.flag_type, group=spec.group,
                description=spec.description, env_default=env_default,
                override_value=None, effective_value=env_default,
                source="env" if env_default is not None else "default",
            )
        return FlagState(
            name=name, flag_type=spec.flag_type, group=spec.group,
            description=spec.description, env_default=env_default,
            override_value=decoded, effective_value=decoded,
            source="runtime-override",
            updated_at=row.updated_at, updated_by=row.updated_by, note=row.note,
        )

    async def load_overrides_into_cache(self) -> Dict[str, Any]:
        """Pull every override row, decode, and replace ``_flag_override_cache``.

        Returns the new cache contents (mainly for diagnostics / tests).
        Call sites: lifespan startup, the 60s refresher loop, and the
        /ops/flags/refresh-all endpoint.

        DB outage tolerance: on failure we log + leave the existing cache
        in place. We never raise — the caller is the refresher loop and a
        crash there would silently kill the timer.
        """
        try:
            rows = (await self.db.execute(select(FeatureFlagOverride))).scalars().all()
        except Exception as ex:
            logger.warning("[feature_flag] cache refresh — DB read failed: %s", ex)
            return dict(_flag_override_cache)

        new_cache: Dict[str, Any] = {}
        for row in rows:
            spec = SUPPORTED_FLAGS.get(row.flag_name)
            if spec is None:
                # whitelist drift — orphan override row; ignore on read but
                # log so ops can clean it up
                logger.warning(
                    "[feature_flag] orphan override for unknown flag %r — ignoring",
                    row.flag_name,
                )
                continue
            try:
                new_cache[row.flag_name] = _decode_value(row.flag_value, spec.flag_type)
            except Exception as ex:
                logger.warning(
                    "[feature_flag] decode failed for %s — ignoring: %s",
                    row.flag_name, ex,
                )

        # Atomic replace — concurrent readers see either old or new dict,
        # never a half-built one.
        _flag_override_cache.clear()
        _flag_override_cache.update(new_cache)
        return dict(new_cache)

    async def list_audit(self, limit: int = 50) -> List[FeatureFlagAudit]:
        """Most recent flip / clear records for the audit Drawer."""
        stmt = (
            select(FeatureFlagAudit)
            .order_by(desc(FeatureFlagAudit.created_at))
            .limit(min(max(limit, 1), 500))
        )
        return list((await self.db.execute(stmt)).scalars().all())

    # ---- write ------------------------------------------------------------

    @transactional
    async def set(
        self,
        name: str,
        value: Any,
        *,
        actor: str = "ops_console",
        note: Optional[str] = None,
    ) -> FlagState:
        """Set an override. Whitelist + type-check before write.

        The audit row is written in the same transaction as the UPSERT so
        either both succeed or neither does — there is no half-flipped
        state in the DB.
        """
        spec = SUPPORTED_FLAGS.get(name)
        if spec is None:
            raise ValueError(f"flag {name!r} is not in SUPPORTED_FLAGS whitelist")

        encoded = _encode_value(value, spec.flag_type)

        # SELECT-then-INSERT/UPDATE keeps this dialect-agnostic so the
        # in-memory aiosqlite test fixture works without a Postgres ON
        # CONFLICT special case. The unique index on flag_name still
        # protects us from duplicate inserts under concurrent writes —
        # a racing INSERT will fail with IntegrityError and the @transactional
        # decorator rolls back; the caller can retry.
        existing = (await self.db.execute(
            select(FeatureFlagOverride).where(FeatureFlagOverride.flag_name == name)
        )).scalar_one_or_none()
        old_encoded = existing.flag_value if existing else None

        if existing is None:
            self.db.add(FeatureFlagOverride(
                flag_name=name,
                flag_value=encoded,
                flag_type=spec.flag_type,
                updated_by=actor,
                note=note,
            ))
        else:
            existing.flag_value = encoded
            existing.flag_type = spec.flag_type
            existing.updated_by = actor
            existing.note = note

        self.db.add(FeatureFlagAudit(
            flag_name=name,
            old_value=old_encoded,
            new_value=encoded,
            action="set",
            actor=actor,
            note=note,
        ))

        # Local cache write-through — request thread sees new value
        # immediately even before the next refresher tick.
        _flag_override_cache[name] = value

        # Cross-process invalidation hint (best-effort)
        self._bump_redis_async_safe()

        return FlagState(
            name=name,
            flag_type=spec.flag_type,
            group=spec.group,
            description=spec.description,
            env_default=self._env_default(name),
            override_value=value,
            effective_value=value,
            source="runtime-override",
            updated_at=datetime.utcnow(),
            updated_by=actor,
            note=note,
        )

    @transactional
    async def clear_override(
        self,
        name: str,
        *,
        actor: str = "ops_console",
        note: Optional[str] = None,
    ) -> FlagState:
        """Remove the override row → next read falls back to env default."""
        spec = SUPPORTED_FLAGS.get(name)
        if spec is None:
            raise ValueError(f"flag {name!r} is not in SUPPORTED_FLAGS whitelist")

        existing = (await self.db.execute(
            select(FeatureFlagOverride).where(FeatureFlagOverride.flag_name == name)
        )).scalar_one_or_none()

        old_encoded = existing.flag_value if existing else None
        if existing is not None:
            await self.db.delete(existing)

        # Audit even on no-op clear — operator's intent is to reset
        self.db.add(FeatureFlagAudit(
            flag_name=name,
            old_value=old_encoded,
            new_value=json.dumps(None),
            action="clear",
            actor=actor,
            note=note,
        ))

        _flag_override_cache.pop(name, None)
        self._bump_redis_async_safe()

        env_default = self._env_default(name)
        return FlagState(
            name=name,
            flag_type=spec.flag_type,
            group=spec.group,
            description=spec.description,
            env_default=env_default,
            override_value=None,
            effective_value=env_default,
            source="env" if env_default is not None else "default",
        )

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _env_default(name: str) -> Any:
        """Read settings via object.__getattribute__ to bypass our own hook."""
        from backend.config import settings  # lazy
        try:
            return object.__getattribute__(settings, name)
        except AttributeError:
            return None

    @staticmethod
    def _bump_redis_async_safe() -> None:
        """Best-effort write of a bump key + delete of the hash key.

        Other processes' refreshers see the bumped value and re-pull from
        DB on their next tick. We never raise — Redis is a hint layer
        only; DB has already been written.
        """
        try:
            from backend.tasks.redis_pool import get_redis_client  # lazy
            cli = get_redis_client()
            cli.delete(REDIS_FLAGS_KEY)
            cli.set(REDIS_FLAGS_BUMP_KEY, str(datetime.utcnow().timestamp()), ex=REDIS_FLAGS_TTL)
        except Exception as ex:
            logger.debug("[feature_flag] redis bump failed (ignored): %s", ex)
