"""Field-coverage exploration ledger refresh (PR-A, 2026-06-09).

Populates the per-(field, universe, delay) mining-ledger columns added by
migration r2b7f4c9a1e3 on ``datafield_cell_stats``, from the live ``alphas``
table. Drives the orthogonal-breadth field bandit (PR-B scheduler). Gated on
ENABLE_FIELD_SCREENING (default OFF) — no-op + zero cost in 止损.

Per (field_id, universe, delay) over the window:
  - times_mined / distinct_alphas = # alphas whose expression uses the field
  - signal_p90    = p90 IS Sharpe of those alphas (dense quality)
  - band_pass_count = # of those that are can_submit (cleared the band)
  - orthogonality = 1 − mean(self_corr) of those, ONLY when ≥3 self_corr samples
    (else NULL). **PR-C: this IS a field_score reward term** (de-crowding), NOT
    informational. ⚠️ self_corr only on band-passing candidates → ~2.1% coverage
    → most fields NULL → reward leans on novelty + downstream gate (ROI question).
  - last_mined    = most recent alpha's created_at

Field usage is detected by tokenising each expression and matching tokens
against the known field_id set (O(alphas × tokens), O(1) set lookup) — far
cheaper than 8k × LIKE scans. Pure token helper is unit-testable w/o DB.

口径 = IS / local (BRAIN OS hidden). Idempotent: full overwrite per refresh.
"""
from __future__ import annotations

import re
import statistics as _st
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Set, Tuple

from loguru import logger

from backend.celery_app import celery_app
from backend.tasks import run_async

# alpha-expression token = identifier (field ids are [a-z0-9_]); split on the rest.
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def extract_field_tokens(expression: str, field_ids: Set[str]) -> Set[str]:
    """Field ids referenced in an expression (token ∈ known field_id set).
    Pure — the unit-test seam (no DB)."""
    if not expression:
        return set()
    return {tok for tok in _TOKEN_RE.findall(expression) if tok in field_ids}


def _p90(xs: List[float]):
    xs = sorted(v for v in xs if v is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    # nearest-rank p90
    k = max(0, min(len(xs) - 1, int(round(0.9 * (len(xs) - 1)))))
    return xs[k]


@celery_app.task(name="backend.tasks.run_field_ledger_refresh")
def run_field_ledger_refresh() -> Dict[str, Any]:
    """Beat/manual-triggered field-ledger refresh. Never raises."""
    try:
        from backend.config import settings
    except Exception as ex:  # noqa: BLE001
        return {"updated_cells": 0, "error": str(ex)[:200]}
    if not bool(getattr(settings, "ENABLE_FIELD_SCREENING", False)):
        return {"updated_cells": 0, "skipped_reason": "flag_off"}
    try:
        return run_async(_refresh_async(
            window_days=int(getattr(settings, "FIELD_LEDGER_WINDOW_DAYS", 0)),
        ))
    except Exception as ex:  # noqa: BLE001
        logger.error(f"[field-ledger] refresh failed: {ex}")
        return {"updated_cells": 0, "error": str(ex)[:200]}


async def _compute_local_pool_corr(db, *, corr_window_days: int = 730,
                                   min_overlap: int = 60) -> Dict[int, float]:
    """{alpha_pk → max |corr| of its local PnL vs ANY submitted-pool alpha}, from
    the local ``alpha_pnl`` table (zero BRAIN calls). PR-C2: widens orthogonality
    coverage from band-pass ``_self_corr`` (~2.1%) to local-PnL (~25.6%); pool is
    100% PnL-covered. orthogonality_contribution = 1 − max_corr.

    Returns {} on any failure (caller falls back to metrics._self_corr). Bounded to
    a recent window (default 2y) to cap the pivot. corrwith over the (small) pool is
    pairwise-complete + vectorised."""
    from sqlalchemy import text as _text
    try:
        import pandas as pd
        import numpy as np
    except Exception:  # noqa: BLE001
        return {}
    pool_ids = [int(r[0]) for r in (await db.execute(_text(
        "SELECT id FROM alphas WHERE date_submitted IS NOT NULL"))).all()]
    if not pool_ids:
        return {}
    # NOTE: alpha_pnl holds the FROZEN backtest series (e.g. 2019-2023), NOT recent
    # calendar dates → window must be relative to MAX(trade_date), not now() (a
    # now()-relative window filters everything out).
    rows = (await db.execute(_text(
        "SELECT alpha_id, trade_date, pnl FROM alpha_pnl "
        "WHERE trade_date > (SELECT MAX(trade_date) FROM alpha_pnl) - make_interval(days => :d)"
    ), {"d": int(corr_window_days)})).all()
    if not rows:
        return {}
    df = pd.DataFrame([(int(a), d, float(p) if p is not None else None) for a, d, p in rows],
                      columns=["aid", "date", "pnl"])
    piv = df.pivot_table(index="date", columns="aid", values="pnl")
    # min_overlap guard: drop series with too few observations → a corr from a
    # handful of common dates is spurious (small-sample noise the design warns of).
    enough = piv.notna().sum(axis=0) >= int(min_overlap)
    piv = piv.loc[:, enough[enough].index]
    pool_cols = [c for c in pool_ids if c in piv.columns]
    if not pool_cols:
        return {}
    cand_cols = [c for c in piv.columns if c not in pool_cols]
    if not cand_cols:
        return {}
    cand_df = piv[cand_cols]
    out: Dict[int, float] = {}
    with np.errstate(invalid="ignore", divide="ignore"):  # constant-PnL cols → NaN corr (filtered)
        for pc in pool_cols:
            cw = cand_df.corrwith(piv[pc])  # pairwise-complete corr vs this pool alpha
            for aid, v in cw.items():
                if pd.notna(v):
                    out[int(aid)] = max(out.get(int(aid), 0.0), abs(float(v)))
    return out


async def _refresh_async(*, window_days: int = 0, session_factory=None) -> Dict[str, Any]:
    from sqlalchemy import select, text
    from backend.models import Alpha, DataField

    if session_factory is None:
        from backend.database import AsyncSessionLocal as session_factory  # noqa: N813

    async with session_factory() as db:
        # field_id → datafield.id (for the cell update join)
        fid_rows = (await db.execute(select(DataField.field_id, DataField.id))).all()
        field_ids: Set[str] = {r[0] for r in fid_rows}
        # field_id → list of datafield.id (a field_id can repeat across datasets)
        fid_to_refs: Dict[str, List[int]] = {}
        for fid, did in fid_rows:
            fid_to_refs.setdefault(fid, []).append(did)

        # PR-C2: local-PnL correlation to the submitted pool (zero BRAIN) — widens
        # orthogonality coverage ~6x over band-pass _self_corr. {alpha_pk: max_corr}.
        local_corr: Dict[int, float] = {}
        try:
            local_corr = await _compute_local_pool_corr(db)
        except Exception as ex:  # noqa: BLE001 — corr failure must not break the ledger
            logger.warning(f"[field-ledger] local pool corr failed (fallback _self_corr): {ex}")

        stmt = select(
            Alpha.id, Alpha.expression, Alpha.universe, Alpha.delay, Alpha.metrics,
            Alpha.can_submit, Alpha.created_at,
        ).where(Alpha.expression.isnot(None))
        if window_days and window_days > 0:
            lo = (datetime.now(timezone.utc) - timedelta(days=window_days)).replace(tzinfo=None)
            stmt = stmt.where(Alpha.created_at > lo)

        # accumulate per (field_id, universe, delay)
        acc: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
        n_alphas = 0
        for apk, expr, universe, delay, metrics, can_submit, created in (await db.execute(stmt)).all():
            n_alphas += 1
            toks = extract_field_tokens(expr, field_ids)
            if not toks:
                continue
            m = metrics if isinstance(metrics, dict) else {}
            sh = m.get("sharpe")
            # orthogonality source: local-PnL corr (PR-C2, 25.6% cov) preferred,
            # else band-pass BRAIN _self_corr (precise but ~2.1% cov).
            sc = local_corr.get(int(apk)) if apk is not None and int(apk) in local_corr else m.get("_self_corr")
            uni = universe or "TOP3000"
            dly = int(delay) if delay is not None else 1
            for fid in toks:
                b = acc.setdefault((fid, uni, dly), {
                    "n": 0, "sh": [], "pass": 0, "sc": [], "last": None})
                b["n"] += 1
                if isinstance(sh, (int, float)):
                    b["sh"].append(float(sh))
                if can_submit is True:
                    b["pass"] += 1
                if isinstance(sc, (int, float)):
                    b["sc"].append(float(sc))
                if created and (b["last"] is None or created > b["last"]):
                    b["last"] = created

        # write back to datafield_cell_stats (per matching cell)
        # orthogonality only TRUSTED with ≥ _MIN_ORTH_SAMPLES self_corr observations —
        # else NULL → field_selector.orthogonality_credible returns the optimistic
        # prior (PR-C credibility horizon; distinct_alphas counts ALL alphas, but the
        # orthogonality estimate is only as good as its self_corr sample count).
        _MIN_ORTH_SAMPLES = 3
        updated = 0
        for (fid, uni, dly), b in acc.items():
            sig = _p90(b["sh"])
            orth = (1.0 - _st.mean(b["sc"])) if len(b["sc"]) >= _MIN_ORTH_SAMPLES else None
            for ref in fid_to_refs.get(fid, []):
                res = await db.execute(text(
                    "UPDATE datafield_cell_stats SET "
                    "times_mined=:n, distinct_alphas=:n, signal_p90=:sig, "
                    "band_pass_count=:bp, orthogonality=:orth, last_mined=:last "
                    "WHERE datafield_ref=:ref AND universe=:uni AND delay=:dly"
                ), {"n": b["n"], "sig": sig, "bp": b["pass"], "orth": orth,
                    "last": b["last"], "ref": ref, "uni": uni, "dly": dly})
                updated += res.rowcount or 0
        await db.commit()

    logger.info(f"[field-ledger] alphas={n_alphas} field-cells_acc={len(acc)} cells_updated={updated}")
    return {"updated_cells": updated, "field_cells": len(acc), "alphas_scanned": n_alphas}
