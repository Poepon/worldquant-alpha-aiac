"""Pool Phase 2 (R1a-v1) — skeleton-frequency SOFT de-prioritization nudge.

Mines recent SUCCESS_PATTERN skeletons for a region, finds the most-crowded ones
(by ``usage_count`` — record_success_pattern bumps it every time a skeleton
recurs), and renders a SOFT prompt nudge asking the LLM to PREFER novel structures
over them. This is NOT a hard forbidden list: a proven recipe is never banned (the
top skeleton is often itself a submitted winner — plan §7.0); the nudge only
de-prioritizes over-reuse to widen portfolio breadth.

Re-anchored at the live build_hypothesis_prompt injection point (PromptContext.
crowded_skeletons_block) — NOT the dead FLAT recent_dedup_skeletons.

Default OFF (ENABLE_R1A_KB_SKELETON_FREQUENCY). OFF / too few samples / no
crowding → returns "" → build_hypothesis_prompt renders byte-for-byte legacy.
Sample-size-gated (SKELETON_FREQUENCY_MIN_SAMPLES) + [:5] cap. Plan §7 Track B;
guard #12 (monitor skeleton-diversity, not just throughput) + #14 (do not promote
past OFF until the live pillar-nudge A/B reports).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Set

from loguru import logger
from sqlalchemy import select


def _row_regions(meta: Any) -> Set[str]:
    """The region(s) a SUCCESS_PATTERN row is tagged with (singular + list)."""
    if not isinstance(meta, dict):
        return set()
    out: Set[str] = set()
    r = meta.get("region")
    if r:
        out.add(r)
    for rr in (meta.get("regions") or []):
        if rr:
            out.add(rr)
    return out


def _fields_hint(meta: Any, cap: int = 3) -> str:
    """A short field/category/operator hint so the nudge is field-aware (lets the
    LLM see WHAT the crowded skeleton is built from, not just its shape)."""
    if not isinstance(meta, dict):
        return ""
    cats = meta.get("dataset_categories_used") or meta.get("dataset_categories") or []
    ops = meta.get("operator_chain") or []
    bits = [str(x) for x in (cats[:cap] if cats else ops[:cap])]
    return ", ".join(b for b in bits if b)


async def skeleton_frequency_nudge_block(
    db_session, *, region: str, dataset_id: Optional[str] = None,
) -> str:
    """Render the crowded-skeleton soft nudge, or "" (flag OFF / too few / none).

    Caller (node_hypothesis) should gate the DB session open on the flag too, so
    the OFF path does zero DB work; this re-checks the flag defensively."""
    from backend.config import settings

    if not bool(getattr(settings, "ENABLE_R1A_KB_SKELETON_FREQUENCY", False)):
        return ""
    try:
        from backend.models import KnowledgeEntry

        window_days = int(getattr(settings, "SKELETON_FREQUENCY_WINDOW_DAYS", 30))
        min_samples = int(getattr(settings, "SKELETON_FREQUENCY_MIN_SAMPLES", 3))
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, window_days))
        # Dialect-free: region is filtered Python-side (meta_data region shape
        # varies: 'region' scalar vs 'regions' list) so the SQL stays sqlite-safe.
        stmt = (
            select(
                KnowledgeEntry.pattern,
                KnowledgeEntry.usage_count,
                KnowledgeEntry.meta_data,
            )
            .where(
                KnowledgeEntry.entry_type == "SUCCESS_PATTERN",
                KnowledgeEntry.is_active.is_(True),
                KnowledgeEntry.updated_at >= cutoff,
            )
        )
        rows = (await db_session.execute(stmt)).all()
    except Exception as ex:  # noqa: BLE001 — instrumentation/prompt-prior; never fatal
        logger.debug(f"[r1a-skeleton-freq] query failed (soft): {ex}")
        return ""

    region_rows = [
        (pat, int(uc or 0), meta)
        for (pat, uc, meta) in rows
        if pat and ((not region) or region in _row_regions(meta))
    ]
    if len(region_rows) < min_samples:
        return ""  # too little signal — histogram would be noise

    # Crowded = a skeleton reinforced more than once. Top-5 by usage_count.
    crowded = sorted(
        (r for r in region_rows if r[1] >= 2), key=lambda r: r[1], reverse=True
    )[:5]
    if not crowded:
        return ""  # all skeletons appear once → already diverse, no nudge

    lines = []
    for pat, uc, meta in crowded:
        hint = _fields_hint(meta)
        tail = f" — e.g. {hint}" if hint else ""
        lines.append(f"- `{(pat or '')[:90]}` (~{uc}×{tail})")
    body = "\n".join(lines)
    return (
        "## Crowded Structures (soft de-prioritization)\n\n"
        "These structural skeletons are heavily reused in recent successful alphas "
        "for this region. They are PROVEN and NOT forbidden — but the orthogonal "
        "value of yet another instance is low. To widen portfolio breadth, PREFER "
        "novel structures / field combinations over these when a comparable idea "
        "exists:\n\n"
        f"{body}"
    )
