"""Hypothesis prompt-context enrichers (Phase 1a-E, four-pool decoupling).

The 8 inline "nudge" blocks that used to live inside ``node_hypothesis``
(generation.py) are extracted here as ``PromptContextEnricher`` strategies +
a ``HypothesisEnricherOrchestrator``. Each enricher is the verbatim body of one
block; the orchestrator runs the enabled ones in SOURCE ORDER (P2-B first — its
``pillar_hint`` feeds G8/R8-v3/G10) over a SINGLE lazily-opened shared session,
and owns the per-enricher non-fatal try/except + rollback. The result is a
``HypothesisEnrichment`` accumulator that ``node_hypothesis`` splats into
``PromptContext`` + uses for the state mutations and post-LLM hypothesis stamps.

Behaviour is byte-for-byte preserved vs the inline blocks:
- Flag gating lives in the orchestrator (``getattr(settings, flag, False)``);
  a disabled enricher never runs → its ``acc`` fields stay at their legacy
  defaults (``None`` / ``[]`` / ``""``) → ``build_hypothesis_prompt`` renders the
  empty-string legacy splice.
- The lazy shared session means the all-flags-OFF legacy path opens ZERO db
  sessions (matching the inline code, which skipped every block).
- Inner redis/cache try/excepts are kept verbatim; the block-level error
  handling moves to the orchestrator (same outcome: field stays default).
- ORTH keeps its own ``AsyncSessionLocal`` factory call (it hands the factory to
  ``compute_submitted_pool_profile``, which manages its own session).

Session note (Phase 1a-E review): ``resolve_db(config)`` returns the workflow's
INJECTED session when one is present (e.g. the ONESHOT path) and otherwise
self-opens an ``AsyncSessionLocal`` (the live pipeline path). So G8 / R8-v3 /
G10 — which used to open their OWN fresh ``AsyncSessionLocal`` — now share the
one session with P2-B/P2-D/P2-A. All eight enrichers are PURE READS, so this is
read-equivalent on the happy path. The orchestrator's ``shared.rollback()``
after a failed enricher therefore recovers (un-poisons) the shared/injected
transaction — intentional and strictly safer than the inline blocks (which left
a failed P2-B/P2-D/P2-A on the injected session aborted). Do NOT "restore"
per-enricher ``AsyncSessionLocal`` thinking it is required; ``shared.close()``
correctly leaves an injected session open for its owner.

This module imports ONLY nodes/base (resolve_db) + config/models + stdlib; it
must NEVER import generation.py (cycle). Services are lazy-imported inside
``enrich`` exactly as the inline blocks did (backend.tasks ↔ backend.agents
cycle). ``node_hypothesis`` imports the orchestrator from here.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from loguru import logger
from langchain_core.runnables import RunnableConfig

from sqlalchemy import select as _sa_select, func as _sa_func

from backend.config import settings as _gen_settings
from backend.models import Alpha, Hypothesis
from backend.agents.graph.nodes.base import resolve_db
from backend.agents.graph.state import MiningState

_NODE = "HYPOTHESIS"  # log tag parity with node_hypothesis


# =============================================================================
# Accumulator + shared-session holder
# =============================================================================

@dataclass
class HypothesisEnrichment:
    """Outputs of the 8 enrichers — defaults == legacy (flag-OFF) values.

    node_hypothesis reads these to (a) populate PromptContext, (b) apply state
    mutations, (c) stamp the post-LLM hypothesis. Defaults match the inline
    blocks' flag-OFF locals so an absent/disabled enricher = byte-for-byte legacy.
    """
    # → PromptContext
    pillar_hint: Optional[str] = None
    neg_kb_pitfalls: List[Dict] = field(default_factory=list)
    macro_narratives: List[Dict] = field(default_factory=list)
    style_preset: Optional[Dict] = None
    cross_task_hyps: List[Dict] = field(default_factory=list)
    cognitive_layer_block: str = ""
    cognitive_layer_id: str = ""
    distilled_logic_block: str = ""
    orth_steer_block: str = ""
    # → state mutations (node writes onto MiningState after run)
    g8_referenced_ids: List[int] = field(default_factory=list)
    cognitive_layer_id_used: str = ""
    g10_injected_entries_n: int = 0
    # → post-LLM hypothesis stamps (node stamps after the LLM returns)
    neg_kb_keys_seen: List[str] = field(default_factory=list)
    macro_keys_seen: List[str] = field(default_factory=list)
    p2c_regime: Optional[str] = None


class _SharedSession:
    """Lazily opens ONE db session (via resolve_db) shared across enrichers.

    Opened only on first ``get()`` so the all-flags-OFF legacy path opens no
    session at all. ``rollback`` clears an aborted shared transaction after a
    failed enricher so the next one isn't poisoned. ``close`` releases it.
    """

    def __init__(self, config: RunnableConfig):
        self._config = config
        self._cm = None
        self._db = None

    async def get(self):
        if self._db is None:
            self._cm = resolve_db(self._config)
            self._db = await self._cm.__aenter__()
        return self._db

    async def rollback(self) -> None:
        if self._db is not None:
            try:
                await self._db.rollback()
            except Exception:  # noqa: BLE001 — rollback failure must not break the round
                pass

    async def close(self) -> None:
        if self._cm is not None:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self._cm = None
            self._db = None


# =============================================================================
# Enrichers — one per former inline block (verbatim body, writes to `acc`)
# =============================================================================

class PillarAwareEnricher:
    """P2-B (2026-05-15): Five Pillars balance nudge → acc.pillar_hint."""
    key = "P2-B"
    enable_flag = "ENABLE_PILLAR_AWARE_SELECTION"

    async def enrich(self, state: MiningState, config, shared: _SharedSession,
                     acc: HypothesisEnrichment) -> None:
        # M9 fix: Redis 60s TTL cache keyed by (region, utc-date) so the
        # per-round JOIN doesn't fire on every node_hypothesis invocation.
        from backend.tasks.redis_pool import get_redis_client
        _p2b_today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _p2b_cache_key = f"aiac:pillar_deficit:{state.region}:{_p2b_today}"
        _p2b_redis = None
        try:
            _p2b_redis = get_redis_client()
        except Exception:
            _p2b_redis = None
        counts = None
        if _p2b_redis is not None:
            try:
                _p2b_cached = _p2b_redis.get(_p2b_cache_key)
                if _p2b_cached is not None:
                    counts = json.loads(_p2b_cached)
            except Exception:
                counts = None

        if counts is None:
            # M3 fix: LEFT JOIN from Alpha — legacy alphas where
            # hypothesis_id IS NULL land in the "unknown" bucket (excluded from
            # share computation). alphas.created_at is naive UTC.
            _p2b_cutoff = (
                datetime.now(timezone.utc) - timedelta(days=7)
            ).replace(tzinfo=None)
            _p2b_db = await shared.get()
            _p2b_stmt = (
                _sa_select(
                    Hypothesis.pillar,
                    _sa_func.count(Alpha.id),
                )
                .select_from(Alpha)
                .outerjoin(
                    Hypothesis,
                    Alpha.hypothesis_id == Hypothesis.id,
                )
                .where(
                    Alpha.region == state.region,
                    Alpha.created_at >= _p2b_cutoff,
                )
                .group_by(Hypothesis.pillar)
            )
            _p2b_rows = (await _p2b_db.execute(_p2b_stmt)).all()
            counts = {(p or "unknown"): int(c) for p, c in _p2b_rows}
            if _p2b_redis is not None:
                try:
                    _p2b_redis.setex(
                        _p2b_cache_key, 60, json.dumps(counts),
                    )
                except Exception:
                    pass  # cache failure must not break the node

        # Compute pillar deficits — "unknown" (legacy NULL) excluded from the
        # denominator. Threshold is deficit relative to target.
        _p2b_target = getattr(
            _gen_settings, "PILLAR_TARGET_DISTRIBUTION", {},
        ) or {}
        pillared_total = sum(
            c for p, c in counts.items() if p in _p2b_target
        ) or 1
        shares = {
            p: counts.get(p, 0) / pillared_total for p in _p2b_target
        }
        deficits = {
            p: max(0.0, _p2b_target[p] - shares.get(p, 0.0))
            for p in _p2b_target
        }
        if deficits:
            top_pillar, top_def = max(
                deficits.items(), key=lambda kv: kv[1],
            )
            _p2b_skew_t = float(getattr(
                _gen_settings, "PILLAR_BALANCE_SKEW_THRESHOLD", 0.4,
            ))
            # Trigger when the deficit exceeds threshold * target.
            if top_def > _p2b_skew_t * _p2b_target.get(top_pillar, 0.2):
                acc.pillar_hint = top_pillar
                logger.info(
                    f"[{_NODE}] P2-B pillar nudge | shares={shares} "
                    f"hint={top_pillar} deficit={top_def:.3f}"
                )


class NegativeKnowledgeEnricher:
    """P2-D (2026-05-15): negative-knowledge nudge → acc.neg_kb_pitfalls."""
    key = "P2-D"
    enable_flag = "ENABLE_NEGATIVE_KNOWLEDGE_NUDGE"

    async def enrich(self, state: MiningState, config, shared: _SharedSession,
                     acc: HypothesisEnrichment) -> None:
        from backend.tasks.redis_pool import get_redis_client
        from backend.services.negative_knowledge_service import (
            NegativeKnowledgeService,
        )
        _p2d_today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _p2d_cache_key = (
            f"aiac:neg_knowledge:{state.region}:{_p2d_today}"
        )
        _p2d_redis = None
        try:
            _p2d_redis = get_redis_client()
        except Exception:
            _p2d_redis = None
        cached_pitfalls = None
        if _p2d_redis is not None:
            try:
                _p2d_cached = _p2d_redis.get(_p2d_cache_key)
                if _p2d_cached is not None:
                    cached_pitfalls = json.loads(_p2d_cached)
            except Exception:
                cached_pitfalls = None

        if cached_pitfalls is None:
            _top_k = int(getattr(
                _gen_settings, "NEGATIVE_KNOWLEDGE_TOP_K", 5,
            ))
            _min_fc = int(getattr(
                _gen_settings, "NEGATIVE_KNOWLEDGE_MIN_FAIL_COUNT", 3,
            ))
            _p2d_db = await shared.get()
            _nks = NegativeKnowledgeService(_p2d_db)
            cached_pitfalls = await _nks.fetch_top_pitfalls(
                region=state.region,
                limit=_top_k,
                min_fail_count=_min_fc,
            )
            if _p2d_redis is not None:
                try:
                    _p2d_redis.setex(
                        _p2d_cache_key, 300,
                        json.dumps(cached_pitfalls, default=str),
                    )
                except Exception:
                    pass  # cache failure must not break the node

        acc.neg_kb_pitfalls = list(cached_pitfalls or [])
        acc.neg_kb_keys_seen = [
            p.get("signature_key", "") for p in acc.neg_kb_pitfalls
            if isinstance(p, dict) and p.get("signature_key")
        ]
        if acc.neg_kb_pitfalls:
            logger.info(
                f"[{_NODE}] P2-D nudge | n={len(acc.neg_kb_pitfalls)} "
                f"region={state.region} "
                f"keys={acc.neg_kb_keys_seen}"
            )


class MacroNarrativeEnricher:
    """P2-A (2026-05-16): macro-narrative RAG nudge → acc.macro_narratives."""
    key = "P2-A"
    enable_flag = "ENABLE_MACRO_NARRATIVE_GUIDANCE"

    async def enrich(self, state: MiningState, config, shared: _SharedSession,
                     acc: HypothesisEnrichment) -> None:
        from backend.tasks.redis_pool import get_redis_client  # lazy (M10)
        from backend.services.macro_narrative_service import (  # lazy (M10)
            MacroNarrativeService,
        )
        _p2a_today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _p2a_cache_key = (
            f"aiac:macro_narrative:{state.dataset_id}:"
            f"{state.region}:{_p2a_today}"
        )
        _p2a_redis = None
        try:
            _p2a_redis = get_redis_client()
        except Exception:
            _p2a_redis = None
        cached = None
        if _p2a_redis is not None:
            try:
                _p2a_cached = _p2a_redis.get(_p2a_cache_key)
                if _p2a_cached is not None:
                    cached = json.loads(_p2a_cached)
            except Exception:
                cached = None

        if cached is None:
            # M7: candidate key extraction with the double-key pattern.
            # state.focused_fields elements may use field_id (Phase-1 union
            # path) OR id (distillation path) — extract both.
            _candidate_keys: List[str] = []
            for f in (state.focused_fields or state.fields or [])[:10]:
                if isinstance(f, dict):
                    fid = f.get("field_id") or f.get("id")
                    if fid:
                        _candidate_keys.append(str(fid))
            _ttl = int(getattr(
                _gen_settings, "MACRO_NARRATIVE_CACHE_TTL_SECONDS", 600,
            ))
            _top_k = int(getattr(
                _gen_settings, "MACRO_NARRATIVE_FIELD_TOP_K", 3,
            ))
            _p2a_db = await shared.get()
            _mns = MacroNarrativeService(_p2a_db)
            cached = await _mns.fetch_macro_narratives(
                dataset_id=state.dataset_id,
                region=state.region,
                key_fields=_candidate_keys,
                limit_field=_top_k,
                limit_dataset=1,
                limit_category=1,
            )
            if _p2a_redis is not None:
                try:
                    _p2a_redis.setex(
                        _p2a_cache_key, _ttl,
                        json.dumps(cached, default=str),
                    )
                except Exception:
                    pass

        acc.macro_narratives = list(cached or [])[:5]
        acc.macro_keys_seen = [
            (n.get("field_id") or n.get("dataset_category") or "")
            for n in acc.macro_narratives
            if isinstance(n, dict)
        ]
        if acc.macro_narratives:
            logger.info(
                f"[{_NODE}] P2-A macro nudge | "
                f"n={len(acc.macro_narratives)} "
                f"dataset={state.dataset_id} region={state.region} "
                f"keys={acc.macro_keys_seen}"
            )


class StylePresetEnricher:
    """P2-C (2026-05-16): regime-aware style preset injection → acc.style_preset.

    The preset is injected into ``strategy`` by mining_agent BEFORE the workflow
    starts; here we just read it out of config["configurable"]["strategy"].
    No db session.
    """
    key = "P2-C"
    enable_flag = "ENABLE_STYLE_PRESET_GUIDANCE"

    async def enrich(self, state: MiningState, config, shared: _SharedSession,
                     acc: HypothesisEnrichment) -> None:
        _strat_blob = (
            (config.get("configurable", {}) or {}).get("strategy", {})
            if config else {}
        )
        if isinstance(_strat_blob, dict):
            acc.p2c_regime = _strat_blob.get("regime")
            _preset_blob = _strat_blob.get("style_preset")
            if acc.p2c_regime and isinstance(_preset_blob, dict):
                acc.style_preset = dict(_preset_blob)
                logger.info(
                    f"[{_NODE}] P2-C style preset attached | "
                    f"regime={acc.p2c_regime}"
                )


class HypothesisForestEnricher:
    """G8 Phase A (2026-05-19): cross-task hypothesis-forest → acc.cross_task_hyps.

    Reads acc.pillar_hint (set by P2-B) as the forest filter — orchestrator runs
    P2-B first.
    """
    key = "G8"
    enable_flag = "ENABLE_HYPOTHESIS_FOREST_REUSE"

    async def enrich(self, state: MiningState, config, shared: _SharedSession,
                     acc: HypothesisEnrichment) -> None:
        from backend.services.hypothesis_service import (
            HypothesisService as _G8HypService,
        )
        _g8_min_pass = int(getattr(
            _gen_settings, "HYPOTHESIS_FOREST_MIN_PASS_COUNT", 2,
        ))
        _g8_min_sharpe = float(getattr(
            _gen_settings, "HYPOTHESIS_FOREST_MIN_SHARPE_AVG", 1.0,
        ))
        _g8_top_k = int(getattr(
            _gen_settings, "HYPOTHESIS_FOREST_TOP_K", 5,
        ))
        _g8_variant = (
            (config.get("configurable", {}) or {}).get("experiment_variant")
            if config else None
        )
        _g8_db = await shared.get()
        _g8_svc = _G8HypService(_g8_db)
        _g8_rows = await _g8_svc.fetch_cross_task_promoted(
            region=state.region,
            pillar=acc.pillar_hint,
            experiment_variant=_g8_variant,
            min_pass_count=_g8_min_pass,
            min_sharpe_avg=_g8_min_sharpe,
            limit=_g8_top_k,
        )
        acc.cross_task_hyps = [
            {
                "hypothesis_id": h.id,
                "statement": h.statement,
                "rationale": h.rationale or "",
                "pillar": h.pillar,
                "sharpe_avg": h.sharpe_avg,
                "pass_count": h.pass_count,
                "alpha_count": h.alpha_count,
            }
            for h in _g8_rows
        ]
        if acc.cross_task_hyps:
            logger.info(
                f"[{_NODE}] G8 forest reference | n={len(acc.cross_task_hyps)} "
                f"region={state.region} pillar_filter={acc.pillar_hint} "
                f"ids={[h['hypothesis_id'] for h in acc.cross_task_hyps]}"
            )
            # Expose referenced ids so evaluation.py can stamp
            # alpha.metrics["_hypothesis_forest_reference"]=True. node_hypothesis
            # writes acc.g8_referenced_ids onto state after the run.
            try:
                acc.g8_referenced_ids = [
                    int(h["hypothesis_id"]) for h in acc.cross_task_hyps
                ]
            except Exception:  # noqa: BLE001 — state assign never breaks round
                pass


class CognitiveLayerEnricher:
    """B5 R8-v3 (Sprint 3, 2026-05-20): cognitive-layer selection.

    → acc.cognitive_layer_block / cognitive_layer_id / cognitive_layer_id_used.
    Reads acc.pillar_hint (P2-B). Bandit/deficit_aware modes load posterior from
    cognitive_layer_bandit_state via the shared session.
    """
    key = "R8-v3"
    enable_flag = "ENABLE_COGNITIVE_LAYER_PROMPT"

    async def enrich(self, state: MiningState, config, shared: _SharedSession,
                     acc: HypothesisEnrichment) -> None:
        from backend.services import cognitive_layer_service as _r8v3_svc
        _r8v3_strategy = str(getattr(
            _gen_settings, "COGNITIVE_LAYER_SELECT_MODE", "round_robin",
        ))
        # F8 review fix: MiningState has no round_index. Use experiment_trace
        # length as a monotonic proxy + task_id hash entropy.
        experiment_trace = (
            (config.get("configurable", {}) or {}).get("strategy", {}) or {}
        ).get("experiment_trace", []) if config else []
        _r8v3_trace_len = int(len(experiment_trace) if experiment_trace else 0)
        _r8v3_task_seed = int(abs(hash(str(getattr(state, "task_id", "") or ""))) % 7)
        _r8v3_round = _r8v3_trace_len + _r8v3_task_seed
        # Tier E E1: load per-layer Beta-Bernoulli posterior so bandit/
        # deficit_aware sample real reward; round_robin ignores stats.
        _r8v3_stats: Dict[str, Any] = {}
        if _r8v3_strategy in ("bandit", "deficit_aware"):
            try:
                from backend.models.cognitive_layer_bandit import (
                    CognitiveLayerBanditState as _CLBandit,
                )
                from sqlalchemy import select as _bsel
                _bandit_db = await shared.get()
                _brows = (await _bandit_db.execute(_bsel(_CLBandit))).scalars().all()
                _r8v3_stats = {
                    r.layer_id: _r8v3_svc.BanditArmStats(
                        layer_id=r.layer_id,
                        pass_count=int(r.pass_count or 0),
                        fail_count=int(r.fail_count or 0),
                    )
                    for r in _brows
                }
            except Exception as _bandit_ex:  # noqa: BLE001
                logger.debug(
                    f"[{_NODE}] R8-v3 bandit state load failed "
                    f"(uniform prior): {_bandit_ex}"
                )
                _r8v3_stats = {}
        _r8v3_layer = _r8v3_svc.select_layer(
            strategy=_r8v3_strategy,
            stats=_r8v3_stats,
            round_index=_r8v3_round,
            pillar_hint=acc.pillar_hint,
        )
        if _r8v3_layer is not None:
            acc.cognitive_layer_block = _r8v3_svc.build_cognitive_layer_block(_r8v3_layer)
            acc.cognitive_layer_id = _r8v3_layer.layer_id
            acc.cognitive_layer_id_used = _r8v3_layer.layer_id
            logger.info(
                f"[{_NODE}] R8-v3 cognitive layer | "
                f"strategy={_r8v3_strategy} layer={acc.cognitive_layer_id} "
                f"pillar_hint={acc.pillar_hint}"
            )


class DistilledLogicEnricher:
    """A5.2 G10 PR2 (Sprint 4, 2026-05-20): distilled-logic injection.

    → acc.distilled_logic_block / g10_injected_entries_n. Reads acc.pillar_hint.
    """
    key = "G10"
    enable_flag = "ENABLE_G10_LOGIC_INJECT"

    async def enrich(self, state: MiningState, config, shared: _SharedSession,
                     acc: HypothesisEnrichment) -> None:
        from backend.services.logic_distill_service import (
            fetch_active_logic_entries as _g10_fetch,
            build_distilled_logic_block as _g10_render,
        )
        _g10_top_k = int(getattr(_gen_settings, "G10_LOGIC_INJECT_TOP_K", 5))
        _g10_db = await shared.get()
        _g10_entries = await _g10_fetch(
            _g10_db,
            region=state.region,
            pillar=acc.pillar_hint,
            limit=_g10_top_k,
        )
        if _g10_entries:
            acc.distilled_logic_block = _g10_render(_g10_entries, max_entries=_g10_top_k)
            acc.g10_injected_entries_n = len(_g10_entries)
            logger.info(
                f"[{_NODE}] G10 inject | n={acc.g10_injected_entries_n} "
                f"region={state.region} pillar_filter={acc.pillar_hint}"
            )


class OrthogonalitySteeringEnricher:
    """Orthogonality-steered exploration Phase A (2026-06-05) → acc.orth_steer_block.

    SPECIAL: passes the AsyncSessionLocal FACTORY to compute_submitted_pool_profile
    (which manages its own session) — does NOT use the orchestrator's shared db.
    """
    key = "ORTH"
    enable_flag = "ENABLE_ORTHOGONAL_PROMPT_STEERING"

    async def enrich(self, state: MiningState, config, shared: _SharedSession,
                     acc: HypothesisEnrichment) -> None:
        from backend.submitted_pool_profile import (
            compute_submitted_pool_profile,
            render_profile_block,
        )
        from backend.database import AsyncSessionLocal
        _orth_prof = await compute_submitted_pool_profile(
            AsyncSessionLocal, state.region)
        acc.orth_steer_block = render_profile_block(_orth_prof)
        if acc.orth_steer_block:
            logger.info(
                "[%s] orthogonality nudge injected | region=%s pillars=%s "
                "block_chars=%d",
                _NODE, state.region,
                list((_orth_prof.get("pillars") or {}).keys()),
                len(acc.orth_steer_block),
            )


# Source order (matches the former inline block order). P2-B MUST precede
# G8/R8-v3/G10 (they filter by acc.pillar_hint). The rest are independent.
_ENRICHERS: List[Any] = [
    PillarAwareEnricher(),
    NegativeKnowledgeEnricher(),
    MacroNarrativeEnricher(),
    StylePresetEnricher(),
    HypothesisForestEnricher(),
    CognitiveLayerEnricher(),
    DistilledLogicEnricher(),
    OrthogonalitySteeringEnricher(),
]


# =============================================================================
# Orchestrator
# =============================================================================

class HypothesisEnricherOrchestrator:
    """Runs the enabled enrichers in source order over one shared session.

    Per-enricher flag gate + non-fatal try/except + rollback (clears an aborted
    shared transaction so the next enricher isn't poisoned). The shared session
    is lazily opened (zero sessions when all flags OFF) and closed in finally.
    """

    def __init__(self, enrichers: Optional[List[Any]] = None):
        self._enrichers = enrichers if enrichers is not None else _ENRICHERS

    async def run(self, state: MiningState, config: RunnableConfig) -> HypothesisEnrichment:
        acc = HypothesisEnrichment()
        shared = _SharedSession(config)
        try:
            for e in self._enrichers:
                if not bool(getattr(_gen_settings, e.enable_flag, False)):
                    continue
                try:
                    await e.enrich(state, config, shared, acc)
                except Exception as ex:  # noqa: BLE001 — nudge failure is non-fatal
                    logger.warning(
                        f"[{_NODE}] enricher {e.key} failed (non-fatal): {ex}"
                    )
                    await shared.rollback()
        finally:
            await shared.close()
        return acc
