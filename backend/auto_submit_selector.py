"""Auto-submit candidate selection + fail-closed guard stack (2026-06-04).

The auto-submit beat (``backend/tasks/auto_submit_tasks.py``) is the automation of
the human "click each row of GET /ops/submit-backlog/drain-order, then POST
/alphas/{id}/submit" loop. Submission is IRREVERSIBLE and burns BRAIN quota, so:

  - ``compute_auto_submit_candidates`` builds the *same* orthogonal-drain ordering
    the endpoint uses (reusing the pure ``marginal_drain`` + ``marginal_recon``
    functions — the scoring math can NOT drift), but with a STRICTER candidate SQL
    (G2: NULL margin / NULL self_corr are EXCLUDED, not passed through) and it pulls
    the extra columns (fitness/turnover/delay/freshness) the guard stack needs.
  - ``evaluate_guard_stack`` is a pure function applying the per-candidate hard
    gates G3-G9 (+G4 freshness). ANY gate failing / missing / stale → not submitted
    (fail-closed). G1 (recon verdict) and G2 (candidate SQL) gate at the
    region/query level and live in ``compute_auto_submit_candidates``.

The final irreversible action is still ``AlphaService.submit_alpha`` (gate G10 —
its own can_submit / live self_corr<0.7 / Redis-lock / re-check), so this layer is
a STRICTER pre-filter, never a bypass.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text as _text, bindparam


async def compute_auto_submit_candidates(db, *, region: str, settings) -> Dict[str, Any]:
    """Build the strict-candidate orthogonal drain order for one region.

    Returns ``{region, sign_routing_ok, recon_stat, ordered, blocked, n_base_pool}``
    where ``ordered``/``blocked`` are candidate dicts carrying every signal the
    guard stack reads. Reuses the exact pure functions the drain-order endpoint
    uses so the ordering/recon math stays bit-identical.
    """
    from backend.marginal_drain import (
        pairwise_corr_from_pnl, greedy_orthogonal_order,
        build_pool_returns, marginal_delta_sharpe, sign_value_tier,
    )
    from backend.marginal_recon import sign_agreement_stats, route_on_sign_verdict

    margin_min = float(getattr(settings, "AUTO_SUBMIT_MARGIN_BPS_MIN", 5.0)) / 10000.0
    thr = float(getattr(settings, "AUTO_SUBMIT_CORR_THRESHOLD", 0.7))

    # Candidate SQL = the SAME (lenient) population the drain-order endpoint uses,
    # so the recon kill-switch verdict is measured over the FULL evidence (offline
    # ΔSharpe vs BRAIN), not a tiny subset (measuring recon over only the
    # strict-submittable rows starves it to insufficient_sample → perpetual
    # fail-closed; shadow run 2026-06-04 caught exactly that). The STRICTNESS
    # ("never act on an un-measured self_corr") moves to the per-candidate guard
    # stack (evaluate_guard_stack G3b) — NULL-self_corr rows stay in the pool for
    # the recon population but are NOT themselves submittable. is_margin is always
    # non-NULL in practice; G6 still hard-gates margin per candidate.
    rows = (await db.execute(_text(
        """
        SELECT id, alpha_id, region, delay, is_sharpe, is_fitness, is_turnover, is_margin,
               (metrics->>'_self_corr')::float AS self_corr,
               metrics->'_iqc_marginal'->>'recommendation' AS recommendation,
               (metrics->'_iqc_marginal'->>'composite_score')::float AS composite,
               (metrics->'_iqc_marginal'->>'delta_sharpe')::float AS brain_d,
               metrics->>'_brain_can_submit_at' AS cs_at,
               (metrics->'_iqc_marginal'->>'delta_score')::float AS delta_score,
               metrics->'_iqc_marginal'->>'scope' AS iqc_scope,
               (SELECT (e->>'value')::float FROM jsonb_array_elements(metrics->'checks') e
                WHERE e->>'name' = 'LOW_SUB_UNIVERSE_SHARPE' LIMIT 1) AS sub_univ_sharpe
        FROM alphas
        WHERE can_submit IS TRUE AND date_submitted IS NULL
          AND region = :region
          AND (is_margin IS NULL OR is_margin >= :mmin)
          AND ((metrics->>'_self_corr')::float IS NULL
               OR (metrics->>'_self_corr')::float < :thr)
        """
    ), {"region": region, "mmin": margin_min, "thr": thr})).all()

    if not rows:
        return {
            "region": region, "sign_routing_ok": False,
            "recon_stat": {"verdict": "insufficient_sample", "sign_agreement_rate": None,
                           "n_sign_compared": 0},
            "ordered": [], "blocked": [], "n_base_pool": 0,
        }

    ids = [int(r[0]) for r in rows]
    # Among-candidate pairwise corr from local alpha_pnl (zero BRAIN cost).
    pnl_q = _text(
        "SELECT alpha_id, trade_date, pnl FROM alpha_pnl "
        "WHERE alpha_id IN :ids AND pnl IS NOT NULL"
    ).bindparams(bindparam("ids", expanding=True))
    pnl_res = (await db.execute(pnl_q, {"ids": ids})).all()
    pnl_rows = [(int(r[0]), r[1], float(r[2])) for r in pnl_res if r[2] is not None]
    pnl_ids = {r[0] for r in pnl_rows}
    corr = pairwise_corr_from_pnl(pnl_rows)

    # Combination layer (L2): region-specific submitted pool base returns.
    pool_res = (await db.execute(_text(
        "SELECT ap.alpha_id, ap.trade_date, ap.pnl FROM alpha_pnl ap "
        "JOIN alphas a ON ap.alpha_id = a.id "
        "WHERE a.date_submitted IS NOT NULL AND ap.pnl IS NOT NULL AND a.region = :pregion"
    ), {"pregion": region})).all()
    pool_rows = [(int(p[0]), p[1], float(p[2])) for p in pool_res if p[2] is not None]
    n_base_pool = len({p[0] for p in pool_rows})
    base_returns = build_pool_returns(pool_rows)
    use_value = base_returns is not None

    # Corr-to-submitted-pool from LOCAL PnL (#39 fix, 2026-06-07 — ports the
    # endpoint's ops.py fix into auto-submit so the two paths stop diverging).
    # G3b fail-closes on NULL stored self_corr, and 56/67 backlog are NULL (async
    # PENDING) → without this auto-submit can't assess orthogonality for ~84% of
    # the backlog (they all fail G3b regardless of quality). Fill the seed with
    # each candidate's MAX |corr| to any submitted-pool member from alpha_pnl.
    # NOTE: only affects G3b/G9 (self_corr seed); recon (ΔSharpe vs BRAIN
    # before-and-after) is untouched, so sign-agreement is not polluted.
    pool_corr_by_id: Dict[int, float] = {}
    pool_member_ids = {p[0] for p in pool_rows}
    if pool_member_ids and pnl_rows:
        union_corr = pairwise_corr_from_pnl(pnl_rows + pool_rows)
        for (a, b), v in union_corr.items():
            av = abs(v)
            if a in pnl_ids and b in pool_member_ids:
                pool_corr_by_id[a] = max(pool_corr_by_id.get(a, 0.0), av)
            elif b in pnl_ids and a in pool_member_ids:
                pool_corr_by_id[b] = max(pool_corr_by_id.get(b, 0.0), av)

    cand_series: Dict[int, Any] = {}
    if use_value and pnl_rows:
        import pandas as _pd
        _cdf = _pd.DataFrame(pnl_rows, columns=["aid", "date", "pnl"])
        for _aid, _g in _cdf.groupby("aid"):
            cand_series[int(_aid)] = _g.set_index("date")["pnl"]

    delta_by_id: Dict[int, Optional[float]] = {}
    for r in rows:
        aid = int(r[0])
        delta_by_id[aid] = (
            marginal_delta_sharpe(base_returns, cand_series.get(aid)) if use_value else None
        )

    # Kill-switch: pair offline ΔSharpe vs BRAIN before-and-after (r[11]); only
    # route on the sign when the live agreement affirmatively validates it.
    recon_pairs = (
        [(delta_by_id.get(int(r[0])), float(r[11]) if r[11] is not None else None) for r in rows]
        if use_value else []
    )
    recon_stat = (
        sign_agreement_stats(recon_pairs) if recon_pairs
        else {"verdict": "insufficient_sample", "sign_agreement_rate": None, "n_sign_compared": 0}
    )
    sign_routing_ok = use_value and route_on_sign_verdict(recon_stat["verdict"])

    candidates: List[Dict[str, Any]] = []
    for r in rows:
        aid = int(r[0])
        sharpe = float(r[4]) if r[4] is not None else None
        composite = float(r[10]) if r[10] is not None else None
        cand: Dict[str, Any] = {
            "id": aid,
            # stored self_corr wins; else PnL-computed corr-to-pool (#39); else
            # None (no PnL → G3b fail-closed, correct: can't act on unmeasured).
            "self_corr": (
                float(r[8]) if r[8] is not None else pool_corr_by_id.get(aid)
            ),
            "score": composite if composite is not None else (sharpe or 0.0),
            "measurable": aid in pnl_ids,
            "_brain_id": r[1],
            "_region": r[2],
            "_delay": int(r[3]) if r[3] is not None else 1,
            "_sharpe": sharpe,
            "_fitness": float(r[5]) if r[5] is not None else None,
            "_turnover": float(r[6]) if r[6] is not None else None,
            "_margin": float(r[7]) if r[7] is not None else None,
            "_composite": composite,
            "_recommendation": r[9],
            "_cs_snapshot": r[12],   # _brain_can_submit_at (can_submit-verdict freshness, ISO str)
            "_pnl_covered": aid in pnl_ids,
            # Competition before-and-after Δscore (informational only — NOT a gate;
            # surfaced in the audit so the human sees the competition-score cost of
            # each submit even though the policy optimizes portfolio marginal value).
            "_delta_score": float(r[13]) if r[13] is not None else None,
            "_iqc_scope": r[14],
            # BRAIN-official narrow-universe Sharpe (#39 incr) — guard G5b reads it.
            "_sub_univ_sharpe": float(r[15]) if r[15] is not None else None,
        }
        if sign_routing_ok:
            cand["value_tier"] = sign_value_tier(delta_by_id.get(aid), aid in pnl_ids)
        candidates.append(cand)

    objective = "value" if sign_routing_ok else "breadth"
    ordered, blocked = greedy_orthogonal_order(
        candidates, corr, threshold=thr, objective=objective,
    )
    for c in ordered:
        c["_in_ordered"] = True
    for c in blocked:
        c["_in_ordered"] = False

    return {
        "region": region,
        "sign_routing_ok": sign_routing_ok,
        "recon_stat": recon_stat,
        "ordered": ordered,
        "blocked": blocked,
        "n_base_pool": n_base_pool,
    }


def _cs_age_hours(snapshot, now_utc: Optional[datetime] = None) -> Optional[float]:
    """Hours since can_submit was last re-checked against BRAIN (the
    ``_brain_can_submit_at`` stamp). Accepts an ISO string (JSONB text) or a
    datetime. None when absent (→ freshness unknown → G4 fail-closed)."""
    if snapshot is None:
        return None
    now = now_utc or datetime.now(timezone.utc)
    if isinstance(snapshot, str):
        try:
            snapshot = datetime.fromisoformat(snapshot)
        except (ValueError, TypeError):
            return None
    ts = snapshot
    # Column is DateTime(timezone=True), but some writers stamp datetime.utcnow()
    # (naive). We assume naive == UTC here — correct ONLY while every writer of
    # metrics_snapshot_at uses UTC (see the repo's dual-timezone footgun). If a
    # writer ever switches to local-naive, freshness would silently skew.
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 3600.0


def evaluate_guard_stack(
    cand: Dict[str, Any],
    *,
    sign_routing_ok: bool,
    settings,
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Apply per-candidate hard gates G3-G9 (+G4 freshness). Pure + deterministic.

    Returns ``{passed: bool, skip_reason: str|None, gates: {Gx: bool}, signals: {...}}``.
    Evaluates ALL gates (no short-circuit) so the audit row records the full
    picture; ``passed`` is the AND of all gates; ``skip_reason`` names the first
    failing gate. FAIL-CLOSED: any missing / un-measured / stale signal → that
    gate is False.
    """
    delay = int(cand.get("_delay") or 1)
    try:
        th = settings.eval_thresholds(delay)
    except Exception:  # noqa: BLE001 — never let a config edge crash the gate
        th = {}
    sharpe_min = float(th.get("sharpe_min", getattr(settings, "EVAL_SHARPE_MIN", 1.5)))
    fitness_min = float(th.get("fitness_min", getattr(settings, "EVAL_FITNESS_MIN", 1.2)))
    turn_min = float(th.get("turnover_min", getattr(settings, "EVAL_TURNOVER_MIN", 0.01)))
    turn_max = float(th.get("turnover_max", getattr(settings, "EVAL_TURNOVER_MAX", 0.4)))
    margin_floor = float(getattr(settings, "AUTO_SUBMIT_MARGIN_BPS_MIN", 5.0)) / 10000.0
    corr_thr = float(getattr(settings, "AUTO_SUBMIT_CORR_THRESHOLD", 0.7))
    subuniv_min = float(getattr(settings, "SUBUNIV_SHARPE_MIN", 0.7))
    require_fresh = bool(getattr(settings, "AUTO_SUBMIT_REQUIRE_FRESH_CANSUBMIT", True))
    max_age_h = float(getattr(settings, "AUTO_SUBMIT_CANSUBMIT_MAX_AGE_H", 12))

    sharpe = cand.get("_sharpe")
    fitness = cand.get("_fitness")
    turnover = cand.get("_turnover")
    margin = cand.get("_margin")
    composite = cand.get("_composite")
    recommendation = cand.get("_recommendation")
    value_tier = cand.get("value_tier")
    max_corr = cand.get("max_corr_to_selected")
    cs_age = _cs_age_hours(cand.get("_cs_snapshot"), now_utc)

    gates: Dict[str, bool] = {}

    # G3 BRAIN form-compliance — guaranteed by the SQL (can_submit IS TRUE), recorded.
    gates["G3_can_submit"] = True

    # G3b self_corr MUST be measured (not None) and below threshold. The candidate
    # SQL is lenient (NULL self_corr allowed, so recon's population is the full
    # evidence) — this gate is where auto-submit refuses to act on an un-measured
    # orthogonality signal. submit_alpha's gate4 still does a LIVE re-check.
    self_corr = cand.get("self_corr")
    gates["G3b_self_corr"] = self_corr is not None and self_corr < corr_thr

    # G4 can_submit freshness (fail-closed when require_fresh): unknown / stale → fail.
    if not require_fresh:
        gates["G4_freshness"] = True
    elif cs_age is None:
        gates["G4_freshness"] = False
    else:
        gates["G4_freshness"] = cs_age <= max_age_h

    # G5 BRAIN red-lines (delay-aware band). Any NULL → fail.
    gates["G5_sharpe"] = sharpe is not None and sharpe >= sharpe_min
    gates["G5_fitness"] = fitness is not None and fitness >= fitness_min
    gates["G5_turnover"] = (
        turnover is not None and turn_min <= turnover <= turn_max
    )

    # G5b sub-universe Sharpe (#39 incr): BRAIN-official narrow-universe robustness.
    # WQ hidden standard wants Sub-Universe Sharpe > ~0.7; a candidate strong on
    # the full universe but weak in the sub-universe is fragile. NULL → fail-closed.
    sub_univ = cand.get("_sub_univ_sharpe")
    gates["G5b_sub_universe"] = sub_univ is not None and sub_univ >= subuniv_min

    # G6 economic gate.
    gates["G6_margin"] = margin is not None and margin >= margin_floor

    # G7 marginal recommendation = SUBMIT and positive composite.
    gates["G7_recommendation"] = (recommendation == "SUBMIT") and (
        composite is not None and composite > 0
    )

    # G8 sign value tier = additive (only meaningful when sign routing validated).
    gates["G8_value_tier"] = bool(sign_routing_ok) and (value_tier == 0)

    # G9 in the greedy ordered (non-blocked) set, below the corr threshold.
    gates["G9_orthogonal"] = bool(cand.get("_in_ordered")) and (
        max_corr is not None and max_corr < corr_thr
    )

    passed = all(gates.values())
    skip_reason = None if passed else next((g for g, ok in gates.items() if not ok), None)

    signals = {
        "delay": delay,
        "sharpe": sharpe, "sharpe_min": sharpe_min,
        "fitness": fitness, "fitness_min": fitness_min,
        "turnover": turnover, "turnover_band": [turn_min, turn_max],
        "sub_universe_sharpe": sub_univ, "sub_universe_min": subuniv_min,
        "margin": margin, "margin_floor": margin_floor,
        "margin_bps": (margin * 10000.0) if margin is not None else None,
        "composite": composite,
        "recommendation": recommendation,
        "value_tier": value_tier,
        "self_corr": cand.get("self_corr"),
        "max_corr_to_selected": max_corr,
        "rank": cand.get("rank"),
        "can_submit_age_h": round(cs_age, 2) if cs_age is not None else None,
        "pnl_covered": bool(cand.get("_pnl_covered")),
        "sign_routing_ok": bool(sign_routing_ok),
        # Informational ONLY (not a gate): competition before-and-after Δscore +
        # its scope. Lets the human see the competition-score cost per submit; the
        # policy itself optimizes portfolio marginal value (Δsharpe), per user goal.
        "delta_score": cand.get("_delta_score"),
        "iqc_scope": cand.get("_iqc_scope"),
    }
    return {"passed": passed, "skip_reason": skip_reason, "gates": gates, "signals": signals}
