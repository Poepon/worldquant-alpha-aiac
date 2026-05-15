"""Alpha Library Health Check Service (P1-C, first half).

来源: docs/alphagbm_skills_research_2026-05-15.md P1 skill `health-check`.

Read-only periodic health audit of the PASS / PASS_PROVISIONAL alpha library.
Produces a payload dict that the daily Celery task wrapper persists to
``docs/alpha_health_check/<asia-shanghai-date>.json``. Three signal classes
(stale / drift / orphan) feed a 0-100 score + 5-band classification +
recommended action string. Pure read path — never mutates ``quality_status``
or KB rows. Hard mutation lives in:

  - ``backend/tasks/sync_tasks.py:_refresh_os_alpha_metrics``  (demote on drift)
  - ``backend/tasks/refresh_tasks.py:refresh_kb_referenced_alphas``  (KB soft-disable)

Architecture decisions (recorded in plan
``C:\\Users\\Administrator\\.claude\\plans\\enumerated-enchanting-knuth.md``):

- Pure-function helpers (`classify_stale`, `classify_drift_*`, `classify_orphan`,
  `compute_health_score`, `to_band`, `recommend`) are unit-testable without DB.
- ``AlphaHealthService.run_full_check()`` orchestrates DB IO + per-alpha
  evaluation. Caller must construct ``BaselineProvider`` (with a pre-built
  ``category_resolver``) and pass it in — letting the service patch the
  provider's private ``_category_resolver`` after construction does not work
  because ``BaselineProvider.__init__`` defaults it to a no-op lambda, so a
  ``getattr(..., None)`` check would never fire.
- File-name generation uses Asia/Shanghai local date from inside
  ``_build_payload`` (``payload['report_date']``), so the wrapper writes
  ``<sh-date>.json`` without needing freezegun for tests — tests inject
  ``now_utc`` directly via ``run_full_check(now_utc=...)``.
- ``zoneinfo`` is intentionally avoided: Python's ``zoneinfo`` reads OS tz on
  Linux/macOS but requires the ``tzdata`` PyPI package on Windows
  (raises ``ZoneInfoNotFoundError`` otherwise). Shanghai has no DST so a
  fixed UTC+8 offset is exactly equivalent and has zero runtime dependency.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.config import settings  # SF6: top-level import, never inside fns

# SH timezone: fixed offset replacement for zoneinfo (no Windows tzdata dep).
SH_TZ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _safe_num(v) -> Optional[float]:
    """Single-value flavour of ``_safe_metric``.

    None / NaN / inf / bool / non-numeric → None. ``bool`` rejected because
    ``isinstance(True, int) is True`` would otherwise turn True/False into
    1.0/0.0 (the same trap covered by ``backend/tests/unit/test_safe_metric.py``).
    """
    if v is None or isinstance(v, bool):
        return None
    if not isinstance(v, (int, float)):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return float(v)


def _to_utc_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """naive datetime → utc-aware (DB sometimes stores naive); aware → unchanged."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return _to_utc_aware(dt).isoformat() if dt else None


# ---------------------------------------------------------------------------
# Stale classification
# ---------------------------------------------------------------------------

def classify_stale(
    metrics_snapshot_at: Optional[datetime], now_utc: datetime,
) -> Dict[str, Any]:
    """Classify staleness based on ``alpha.metrics_snapshot_at`` age.

    Returns dict ``{stale_days, stale_severity, reason}``. ``None`` snapshot
    → red ``never_refreshed`` (NULL means BRAIN GET /alphas/{id} has not yet
    run for this row, a real data-quality concern).
    """
    if metrics_snapshot_at is None:
        return {"stale_days": None, "stale_severity": "red",
                "reason": "never_refreshed"}
    snap = _to_utc_aware(metrics_snapshot_at)
    days = max(0.0, (now_utc - snap).total_seconds() / 86400.0)
    if days <= settings.STALE_YELLOW_DAYS:
        sev = "green"
    elif days <= settings.STALE_ORANGE_DAYS:
        sev = "yellow"
    elif days <= settings.STALE_RED_DAYS:
        sev = "orange"
    else:
        sev = "red"
    return {
        "stale_days": round(days, 1),
        "stale_severity": sev,
        "reason": None if sev == "green" else f"stale_{int(days)}d",
    }


# ---------------------------------------------------------------------------
# Drift classification
# ---------------------------------------------------------------------------

def _delta_pct(cur: Optional[float], base: Optional[float]) -> Optional[float]:
    if cur is None or base is None:
        return None
    if abs(base) < 1e-9:
        return None
    return (cur - base) / abs(base) * 100.0


def _drift_severity_from_sharpe(d_sharpe: Optional[float]) -> str:
    """Map sharpe delta-pct → severity band using settings.DRIFT_*_PCT thresholds.

    Threshold semantics: configured as **negative** percentages (e.g.
    DRIFT_RED_PCT=-50.0 means "down 50%"). A drop more severe than RED
    (i.e. d_sharpe <= DRIFT_RED_PCT) maps to red; smaller drops cascade
    down; a positive change is green.
    """
    if d_sharpe is None:
        return "unknown"
    if d_sharpe <= settings.DRIFT_RED_PCT:
        return "red"
    if d_sharpe <= settings.DRIFT_ORANGE_PCT:
        return "orange"
    if d_sharpe <= settings.DRIFT_YELLOW_PCT:
        return "yellow"
    return "green"


def classify_drift_from_decay(alpha) -> Optional[Dict[str, Any]]:
    """Primary drift path — compare current metrics vs ``decay_curve[0]``.

    Returns ``None`` when the curve is empty / malformed / missing the
    head sharpe so the caller can fall back to BaselineProvider.
    """
    curve = alpha.decay_curve or []
    if not curve or not isinstance(curve[0], dict):
        return None
    head = curve[0]
    base_sharpe = _safe_num(head.get("sharpe"))
    if base_sharpe is None:
        return None
    base_fitness = _safe_num(head.get("fitness"))
    base_turnover = _safe_num(head.get("turnover"))
    cur_sharpe = _safe_num(alpha.is_sharpe)
    cur_fitness = _safe_num(alpha.is_fitness)
    cur_turnover = _safe_num(alpha.is_turnover)
    d_sharpe = _delta_pct(cur_sharpe, base_sharpe)
    d_fitness = _delta_pct(cur_fitness, base_fitness)
    d_turnover = _delta_pct(cur_turnover, base_turnover)
    return {
        "baseline_source": "decay_curve_head",
        "baseline_sharpe": base_sharpe,
        "current_sharpe": cur_sharpe,
        "sharpe_delta_pct": round(d_sharpe, 1) if d_sharpe is not None else None,
        "baseline_fitness": base_fitness,
        "current_fitness": cur_fitness,
        "fitness_delta_pct": round(d_fitness, 1) if d_fitness is not None else None,
        "baseline_turnover": base_turnover,
        "current_turnover": cur_turnover,
        "turnover_delta_pct": round(d_turnover, 1) if d_turnover is not None else None,
        "severity": _drift_severity_from_sharpe(d_sharpe),
        "reason": (
            f"sharpe_down_{abs(int(d_sharpe))}pct"
            if d_sharpe is not None and d_sharpe <= -10 else None
        ),
    }


def classify_drift_from_baseline(alpha, baseline_stats) -> Dict[str, Any]:
    """Fallback drift path — compare current sharpe vs cluster ``BaselineStats``.

    Cluster baseline = (expected_signal × dataset × region) fine cell with
    fine→coarse fallback inside BaselineProvider. ``baseline_stats.usable``
    is the gate the screener uses.
    """
    if baseline_stats is None or not baseline_stats.usable:
        return {"baseline_source": "none", "severity": "unknown",
                "reason": "no_baseline_available"}
    base_sharpe = _safe_num(baseline_stats.mean)
    cur_sharpe = _safe_num(alpha.is_sharpe)
    d_sharpe = _delta_pct(cur_sharpe, base_sharpe)
    return {
        "baseline_source": "cluster_baseline",
        "baseline_sharpe": base_sharpe,
        "current_sharpe": cur_sharpe,
        "sharpe_delta_pct": round(d_sharpe, 1) if d_sharpe is not None else None,
        "severity": _drift_severity_from_sharpe(d_sharpe),
        "reason": (
            f"sharpe_below_cluster_mean_{abs(int(d_sharpe))}pct"
            if d_sharpe is not None and d_sharpe <= -10 else None
        ),
    }


# ---------------------------------------------------------------------------
# Orphan classification
# ---------------------------------------------------------------------------

def classify_orphan(
    alpha, kb_index: Dict[int, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """KB-reference signal.

    ``kb_index[alpha.id]`` is a list of ``{kb_id, kb_is_active}`` dicts.
    Within PASS+PASS_PROVISIONAL scope an alpha is **never** orphan
    (alpha still in good standing); a real orphan only happens when a KB
    SUCCESS_PATTERN references an alpha whose quality_status has dropped
    out of PASS scope — that case lives in
    ``kb_orphans_outside_scope`` instead.
    """
    entries = kb_index.get(alpha.id, [])
    if not entries:
        return {"is_kb_referenced": False, "is_orphan": False,
                "kb_entries": [], "severity": "green"}
    active = [e for e in entries if e["kb_is_active"]]
    return {"is_kb_referenced": bool(active),
            "is_orphan": False,
            "kb_entries": entries, "severity": "green"}


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------

_SEV_PEN = {"green": 0, "yellow": 25, "orange": 55, "red": 90, "unknown": 25}


def compute_health_score(stale_info, drift_info, orphan_info) -> Dict[str, Any]:
    """Weighted 0-100 health score from per-signal severity penalties.

    Higher score = healthier. ``orphan_pen`` is 100 when ``is_orphan=True``
    (a stronger signal than mere KB reference); within in-scope alphas
    this is always 0 because ``classify_orphan`` never sets is_orphan.
    """
    stale_pen = _SEV_PEN.get(stale_info["stale_severity"], 50)
    drift_pen = _SEV_PEN.get(drift_info["severity"], 25)
    orphan_pen = 100 if orphan_info.get("is_orphan") else 0
    weighted = (
        settings.HEALTH_WEIGHT_STALE * stale_pen
        + settings.HEALTH_WEIGHT_DRIFT * drift_pen
        + settings.HEALTH_WEIGHT_ORPHAN * orphan_pen
    )
    score = max(0.0, min(100.0, 100.0 - weighted))
    return {"score": round(score, 1), "stale_pen": stale_pen,
            "drift_pen": drift_pen, "orphan_pen": orphan_pen}


def to_band(score: float) -> str:
    """5-band classification from 0-100 score.

    GREEN >= 85, YELLOW [70, 85), ORANGE [50, 70), RED [30, 50), CRITICAL < 30.
    """
    if score >= 85:
        return "GREEN"
    if score >= 70:
        return "YELLOW"
    if score >= 50:
        return "ORANGE"
    if score >= 30:
        return "RED"
    return "CRITICAL"


def recommend(band: str, signals: Dict[str, Dict[str, Any]]) -> Tuple[str, str]:
    """(action_code, reason_text) per band. Pure string output — no demote."""
    drift_reason = signals.get("drift", {}).get("reason")
    stale_reason = signals.get("stale", {}).get("reason")
    orphan_reason = signals.get("orphan", {}).get("reason")
    parts = [r for r in (drift_reason, stale_reason, orphan_reason) if r]
    reason = "; ".join(parts) if parts else "no issues"
    mapping = {
        "GREEN":    ("keep",            "metrics healthy"),
        "YELLOW":   ("monitor",         reason),
        "ORANGE":   ("review",          reason),
        "RED":      ("consider_demote", reason),
        "CRITICAL": ("investigate",     f"{reason}; manual triage needed"),
    }
    return mapping.get(band, ("review", reason))


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------

class AlphaHealthService:
    """Read-only health check. Caller invokes ``run_full_check()`` to get
    the full payload dict; the Celery task wrapper persists it as JSON.
    """

    SCOPE: Tuple[str, ...] = ("PASS", "PASS_PROVISIONAL")

    def __init__(self, db, baseline_provider=None):
        self.db = db
        self.bp = baseline_provider  # None → no cluster-baseline fallback

    # --- DB helpers ---------------------------------------------------------

    async def _build_kb_index(self) -> Dict[int, List[Dict[str, Any]]]:
        """Index ALL SUCCESS_PATTERN entries (including is_active=False) by
        referenced ``meta_data['alpha_id_ref']``. Soft-deleted rows are kept
        so the orphan detector can distinguish "kb_active_but_alpha_demoted"
        from "kb_softdel_alpha_demoted".

        The narrowing filter ``meta_data['alpha_id_ref'].isnot(None)`` keeps
        the JSONB scan tight — legacy KB rows without an alpha_id_ref can
        run to 5k+ and would force a large in-memory load otherwise.
        """
        from backend.models import KnowledgeEntry
        from sqlalchemy import select
        stmt = (
            select(
                KnowledgeEntry.id,
                KnowledgeEntry.is_active,
                KnowledgeEntry.meta_data,
            )
            .where(KnowledgeEntry.entry_type == "SUCCESS_PATTERN")
            .where(KnowledgeEntry.meta_data["alpha_id_ref"].isnot(None))
        )
        rows = (await self.db.execute(stmt)).all()
        idx: Dict[int, List[Dict[str, Any]]] = {}
        for kb_id, kb_active, md in rows:
            ref = (md or {}).get("alpha_id_ref")
            if isinstance(ref, int):
                idx.setdefault(ref, []).append(
                    {"kb_id": kb_id, "kb_is_active": bool(kb_active)}
                )
        return idx

    @classmethod
    async def build_category_resolver(
        cls, db,
    ) -> Callable[[str], Optional[str]]:
        """Build a ``dataset_id → category`` closure for BaselineProvider.

        Must select the business-key column ``DatasetMetadata.dataset_id``
        (String(100)) — **not** the int PK ``DatasetMetadata.id`` — because
        ``Alpha.dataset_id`` is also a business-key String(50). Looking up
        by int PK would always miss.

        ``(dataset_id, region, universe)`` is a UniqueConstraint on
        ``datasets``, so the same business key recurs across regions but
        ``category`` is invariant; a simple last-write-wins dict is fine.

        Async classmethod so the task wrapper can do
        ``resolver = await AlphaHealthService.build_category_resolver(db)``
        **before** constructing ``BaselineProvider(category_resolver=resolver)``
        — consistent with the existing pattern in
        ``backend/agents/graph/nodes/evaluation.py``.
        """
        from backend.models import DatasetMetadata
        from sqlalchemy import select
        try:
            stmt = select(DatasetMetadata.dataset_id, DatasetMetadata.category)
            rows = (await db.execute(stmt)).all()
            _map: Dict[str, str] = {
                r.dataset_id: r.category for r in rows if r.category
            }
            return lambda dataset_id: _map.get(dataset_id)
        except Exception as e:
            from loguru import logger
            logger.warning(
                f"[alpha_health] category_resolver build failed: {e}; "
                f"BaselineProvider will fall back to fine-only "
                f"(drift will commonly report unknown)"
            )
            return lambda _ds: None

    async def _build_expected_signal_map(self, alphas) -> Dict[int, str]:
        """Single IN-query to load all ``hypothesis_id → expected_signal``
        pairs, avoiding N+1 lookups in the per-alpha loop."""
        from backend.models import Hypothesis
        from sqlalchemy import select
        hids = {a.hypothesis_id for a in alphas if a.hypothesis_id is not None}
        if not hids:
            return {}
        stmt = select(Hypothesis.id, Hypothesis.expected_signal).where(
            Hypothesis.id.in_(hids)
        )
        return {
            r.id: r.expected_signal or "unknown"
            for r in (await self.db.execute(stmt)).all()
        }

    async def _resolve_drift(
        self, alpha, expected_signal_map: Dict[int, str],
    ) -> Dict[str, Any]:
        """Primary decay-curve path; falls back to BaselineProvider when no
        decay; degrades to ``unknown`` when no provider was injected."""
        primary = classify_drift_from_decay(alpha)
        if primary is not None:
            return primary
        if self.bp is None:
            return {"baseline_source": "none", "severity": "unknown",
                    "reason": "no_baseline_available"}
        exp_sig = (
            expected_signal_map.get(alpha.hypothesis_id)
            if alpha.hypothesis_id else None
        ) or "unknown"
        try:
            stats = await self.bp.get_baseline(
                expected_signal=exp_sig,
                dataset_id=getattr(alpha, "dataset_id", "") or "",
                region=alpha.region or "USA",
            )
        except Exception as e:
            return {"baseline_source": "error", "severity": "unknown",
                    "reason": f"baseline_lookup_failed: {type(e).__name__}"}
        return classify_drift_from_baseline(alpha, stats)

    # --- main entrypoint ----------------------------------------------------

    async def run_full_check(self, now_utc: Optional[datetime] = None) -> Dict[str, Any]:
        """Run the full health audit and return the payload dict.

        ``now_utc`` is an injection point for tests (so they don't need
        freezegun). Defaults to ``datetime.now(timezone.utc)``.
        """
        from sqlalchemy import select
        from backend.models import Alpha
        now_utc = now_utc or datetime.now(timezone.utc)

        # Step 1 — load all PASS+PROVISIONAL alphas
        alphas = (await self.db.execute(
            select(Alpha).where(Alpha.quality_status.in_(self.SCOPE))
        )).scalars().all()

        # Step 2 — build lookup tables (sequential; AsyncSession is not
        # concurrent-safe across awaits)
        kb_index = await self._build_kb_index()
        exp_sig_map = await self._build_expected_signal_map(alphas)
        # NOTE: category_resolver is constructed by the task wrapper and
        # passed into BaselineProvider before this service is built —
        # nothing else to do here.

        # Step 3 — per-alpha evaluation
        records: List[Dict[str, Any]] = []
        for a in alphas:
            stale_info = classify_stale(a.metrics_snapshot_at, now_utc)
            drift_info = await self._resolve_drift(a, exp_sig_map)
            orphan_info = classify_orphan(a, kb_index)
            score_info = compute_health_score(stale_info, drift_info, orphan_info)
            band = to_band(score_info["score"])
            action, reason = recommend(
                band,
                {"stale": stale_info, "drift": drift_info, "orphan": orphan_info},
            )
            records.append({
                "alpha_pk": a.id,
                "alpha_id": a.alpha_id,
                "region": a.region,
                "universe": a.universe,
                "quality_status": a.quality_status,
                "factor_tier": getattr(a, "factor_tier", None),
                "hypothesis_id": a.hypothesis_id,
                "date_created": _iso(a.date_created),
                "metrics_snapshot_at": _iso(a.metrics_snapshot_at),
                "health_score": score_info["score"],
                "health_band": band,
                "signals": {"stale": stale_info, "drift": drift_info,
                            "orphan": orphan_info},
                "recommended_action": action,
                "reason": reason,
            })

        # Step 4 — orphans outside scope (the real KB orphan signal)
        out_of_scope = await self._collect_orphans_outside_scope(kb_index, alphas)

        # Step 5 — assemble payload
        return self._build_payload(records, out_of_scope, alphas, exp_sig_map, now_utc)

    async def _collect_orphans_outside_scope(
        self, kb_index: Dict[int, List[Dict[str, Any]]], alphas_in_scope,
    ) -> List[Dict[str, Any]]:
        """KB references targeting alpha_pks **outside** PASS+PROV scope —
        the actual orphan signal. Distinguishes active vs soft-deleted KB
        entries via separate reason codes."""
        from sqlalchemy import select
        from backend.models import Alpha
        in_scope_ids = {a.id for a in alphas_in_scope}
        target_ids = set(kb_index.keys()) - in_scope_ids
        if not target_ids:
            return []
        rows = (await self.db.execute(
            select(Alpha.id, Alpha.alpha_id, Alpha.quality_status)
            .where(Alpha.id.in_(target_ids))
        )).all()
        result: List[Dict[str, Any]] = []
        found_ids = set()
        for r in rows:
            found_ids.add(r.id)
            entries = kb_index[r.id]
            active_entries = [e for e in entries if e["kb_is_active"]]
            result.append({
                "alpha_pk": r.id,
                "alpha_id": r.alpha_id,
                "quality_status": r.quality_status,
                "kb_entry_ids": [e["kb_id"] for e in entries],
                "kb_active_entry_ids": [e["kb_id"] for e in active_entries],
                "reason": (
                    f"kb_active_but_alpha_{r.quality_status}"
                    if active_entries else
                    f"kb_softdel_alpha_{r.quality_status}"
                ),
            })
        # KB referenced an alpha_pk that no longer exists in `alphas` at all.
        # NOTE: "MISSING" is a plan-invented sentinel string, NOT a member of
        # the `QualityStatus` enum. Downstream consumers must not parse this
        # field as an enum. See plan §NH2.
        missing_ids = target_ids - found_ids
        for mid in missing_ids:
            result.append({
                "alpha_pk": mid,
                "alpha_id": None,
                "quality_status": "MISSING",
                "kb_entry_ids": [e["kb_id"] for e in kb_index[mid]],
                "reason": "kb_references_missing_alpha",
            })
        return result

    def _build_payload(
        self, records, orphans_outside, alphas, exp_sig_map, now_utc,
    ) -> Dict[str, Any]:
        # Asia/Shanghai local date (fixed offset, no zoneinfo / tzdata dep).
        sh_now = now_utc.astimezone(SH_TZ)

        by_band = {"GREEN": 0, "YELLOW": 0, "ORANGE": 0, "RED": 0, "CRITICAL": 0}
        by_region: Dict[str, Dict[str, Any]] = {}
        for r in records:
            by_band[r["health_band"]] += 1
            reg = r["region"] or "UNKNOWN"
            by_region.setdefault(
                reg,
                {"checked": 0, "by_band": {k: 0 for k in by_band.keys()}},
            )
            by_region[reg]["checked"] += 1
            by_region[reg]["by_band"][r["health_band"]] += 1

        # Truncated alphas list — only dump records below the threshold so
        # the JSON stays grep-able. GREEN counts are still in totals.
        threshold = settings.HEALTH_SCORE_TRUNCATE_THRESHOLD
        dumped = sorted(
            [r for r in records if r["health_score"] < threshold],
            key=lambda r: r["health_score"],
        )

        # hypothesis_coverage_pct numerator/denominator must use the same
        # filter (alpha has a hypothesis_id) — otherwise mixing
        # "alpha with no link" into the numerator can drive the ratio
        # negative (e.g. 50 unlinked + 10 linked → -400%).
        hyp_total = len([a for a in alphas if a.hypothesis_id is not None])
        unknown_exp = len([
            a for a in alphas
            if a.hypothesis_id is not None
            and exp_sig_map.get(a.hypothesis_id) in (None, "unknown")
        ])

        return {
            "report_date": sh_now.strftime("%Y-%m-%d"),
            "generated_at": sh_now.isoformat(),
            "scope": list(self.SCOPE),
            "config": {
                "stale_yellow_days": settings.STALE_YELLOW_DAYS,
                "stale_orange_days": settings.STALE_ORANGE_DAYS,
                "stale_red_days": settings.STALE_RED_DAYS,
                "drift_yellow_pct": settings.DRIFT_YELLOW_PCT,
                "drift_orange_pct": settings.DRIFT_ORANGE_PCT,
                "drift_red_pct": settings.DRIFT_RED_PCT,
                "weights": {
                    "stale": settings.HEALTH_WEIGHT_STALE,
                    "drift": settings.HEALTH_WEIGHT_DRIFT,
                    "orphan": settings.HEALTH_WEIGHT_ORPHAN,
                },
                "score_truncate_threshold": threshold,
            },
            "totals": {
                "checked": len(records),
                "by_band": by_band,
                "hypothesis_coverage_pct": (
                    round((1 - unknown_exp / hyp_total) * 100, 1)
                    if hyp_total else None
                ),
            },
            "regions": by_region,
            "kb_orphans_outside_scope": orphans_outside,
            "alphas": dumped,
        }
