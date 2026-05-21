"""Phase 4 Sprint 1 A3 — flat-F4 cross-region quota service.

Plan: docs/phase4_a_b_plan_v5_2026-05-19.md §6.3

Pattern: Millennium 320 pods multi-strategy / Citadel 5 业务线并行 — AIAC
production data shows region distribution heavily biased to USA. Without
a quota guard, every new flat-session POST defaults to USA TOP3000 and
deepens the bias.

This module exposes two pure functions:
  - ``compute_region_share(db, lookback_days)`` — current per-region share
    of active mining_tasks
  - ``check_quota(new_region, share_now, quota)`` — would adding one new
    task in `new_region` push that region over its quota?

The router (start_flat_session) imports both and applies the
ENFORCE=True/False policy. Service stays decision-free (it doesn't know
about flag state); router decides 400 vs warn.

Soft-fail philosophy:
  Every helper swallows DB errors and returns "no info". Router treats
  "no info" as "skip the check" + log warn. Quota is an *advisory* layer,
  NOT a critical correctness gate — if DB is having a blip, accept the
  POST rather than block the operator.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from sqlalchemy import func, select

logger = logging.getLogger("services.flat_region_quota")


# Statuses we count as "active" — drives the share denominator. Choosing
# RUNNING + PAUSED captures everything that holds a region slot from the
# Millennium-style 5%-rule perspective. COMPLETED / STOPPED tasks have
# released their slot.
_ACTIVE_STATUSES = ("RUNNING", "PAUSED", "PENDING")


async def compute_region_share(
    db,
    *,
    lookback_days: int = 30,
) -> Dict[str, Dict[str, float]]:
    """Return ``{region: {"count": int, "share": float}}`` for tasks
    created in the last ``lookback_days`` days with status in active set.

    Includes ``__total__`` key with the absolute task count so the caller
    can guard against div-by-zero. Soft-fail returns empty dict on any
    DB error.
    """
    try:
        from backend.models import MiningTask

        cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, lookback_days))
        # cutoff TZ-naive so the comparison works against `created_at`
        # columns that vary across dialects.
        cutoff_naive = cutoff.replace(tzinfo=None)

        stmt = (
            select(MiningTask.region, func.count(MiningTask.id))
            .where(
                MiningTask.status.in_(_ACTIVE_STATUSES),
                MiningTask.created_at >= cutoff_naive,
            )
            .group_by(MiningTask.region)
        )
        rows = (await db.execute(stmt)).all()
        counts = {(r or "UNKNOWN"): int(c) for r, c in rows}
        total = sum(counts.values())
        out: Dict[str, Dict[str, float]] = {}
        if total <= 0:
            return {"__total__": {"count": 0, "share": 0.0}}
        for region, n in counts.items():
            out[region] = {"count": n, "share": float(n) / float(total)}
        out["__total__"] = {"count": total, "share": 1.0}
        return out
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[flat_region_quota] compute_region_share failed (soft-fail empty): %s",
            ex,
        )
        return {}


def check_quota(
    *,
    new_region: str,
    current_share: Dict[str, Dict[str, float]],
    quota: Dict[str, float],
) -> Dict[str, object]:
    """Compute whether adding ONE new task in ``new_region`` would push
    that region's share above its configured quota.

    Returns a decision dict::

        {
            "would_exceed": bool,
            "new_region": str,
            "projected_share": float,   # post-add share
            "quota": float,              # configured quota for region (1.0 if absent)
            "current_count": int,
            "projected_count": int,
            "projected_total": int,
            "skip_reason": Optional[str],  # e.g. "no_share_data" if we couldn't compute
        }

    When ``current_share`` is empty (compute_region_share soft-failed) we
    return ``would_exceed=False`` with skip_reason='no_share_data' so the
    router treats it as "skip the check" rather than blocking the POST.
    """
    if not current_share:
        return {
            "would_exceed": False,
            "new_region": new_region,
            "projected_share": 0.0,
            "quota": float(quota.get(new_region, 1.0)),
            "current_count": 0,
            "projected_count": 1,
            "projected_total": 1,
            "skip_reason": "no_share_data",
        }
    total_entry = current_share.get("__total__", {})
    current_total = int(total_entry.get("count", 0) or 0)
    region_entry = current_share.get(new_region, {})
    current_region_count = int(region_entry.get("count", 0) or 0)
    projected_region_count = current_region_count + 1
    projected_total = current_total + 1
    projected_share = (
        float(projected_region_count) / float(projected_total)
        if projected_total > 0
        else 0.0
    )
    # quota.get(region, 1.0) — missing region means no cap (e.g. for an
    # experiment_variant region operator hasn't tagged in QUOTA yet)
    region_quota = float(quota.get(new_region, 1.0))
    would_exceed = projected_share > region_quota
    return {
        "would_exceed": would_exceed,
        "new_region": new_region,
        "projected_share": projected_share,
        "quota": region_quota,
        "current_count": current_region_count,
        "projected_count": projected_region_count,
        "projected_total": projected_total,
        "skip_reason": None,
    }


def build_distribution_summary(
    share: Dict[str, Dict[str, float]],
    quota: Dict[str, float],
) -> Dict[str, object]:
    """Build the /ops/flat-region/distribution payload — per-region share
    + quota + status flag (ok / warn / exceeded).

    Used by the ops dashboard to surface the over-quota chips. Pure
    function (no DB) so it composes with whatever future caller wants
    to build the same view from cached data.
    """
    regions_view = []
    # Iterate quota keys first so the response is stable + includes regions
    # with 0 active tasks (chip shows "0% / quota 20%").
    seen_regions = set()
    total = int(share.get("__total__", {}).get("count", 0) or 0)
    for region, region_quota in sorted(quota.items()):
        entry = share.get(region, {})
        count = int(entry.get("count", 0) or 0)
        share_val = float(entry.get("share", 0.0) or 0.0)
        if share_val > region_quota:
            status = "exceeded"
        elif share_val >= region_quota * 0.9:  # within 10% of cap → warn
            status = "warn"
        else:
            status = "ok"
        regions_view.append({
            "region": region,
            "count": count,
            "share": share_val,
            "quota": float(region_quota),
            "status": status,
        })
        seen_regions.add(region)
    # Include any region present in share that has NO quota row — surfaces
    # config drift (operator forgot to add HKG to QUOTA after activating).
    for region in sorted(share.keys()):
        if region in seen_regions or region == "__total__":
            continue
        entry = share[region]
        regions_view.append({
            "region": region,
            "count": int(entry.get("count", 0) or 0),
            "share": float(entry.get("share", 0.0) or 0.0),
            "quota": None,  # not in QUOTA — uncapped
            "status": "no_quota",
        })
    return {
        "total_active_tasks": total,
        "regions": regions_view,
    }


__all__ = [
    "compute_region_share",
    "check_quota",
    "build_distribution_summary",
]
