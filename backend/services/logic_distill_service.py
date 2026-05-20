"""A5.1 G10 logic-as-asset distill service (Phase 4 Sprint 3 / plan v5 §6.12).

Weekly Sunday-03:00 SH cron walks the past 7 days' PASS alphas grouped
by (pillar, region), feeds each group to an LLM with a concise distill
prompt, and writes the resulting 1-3-sentence summary to the
``distilled_logic_library`` table. PR2 (Sprint 4) will inject these
back into the hypothesis prompt; PR1 (this module) just builds the
library.

Inspired by RD-Agent's "logic-as-asset" concept (NeurIPS 2025) and
Citadel's internal "research diary" practice — once a research lens
produces winning alphas, the *logic* (the why) becomes a reusable
asset, not just the resulting expression.

Cost controls:
  - ``LOGIC_DISTILL_MAX_COST_USD_PER_WEEK`` (default $5)
  - Top-K alphas per (pillar, region) bucket
  - Min PASS count per bucket — < min skip
  - Fallback: when LLM call fails / cost cap hit, last-week entries
    are NOT retired (operator dashboard surfaces the staleness)

Token Jaccard similarity to the prior-week entry stamped on each row
for diagnostics + future dedup (PR2).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Distill prompt (kept terse — operator can override via prompts.yaml)
# ---------------------------------------------------------------------------

_DEFAULT_DISTILL_PROMPT = """\
You are reading a list of {count} alpha expressions that recently PASSed
quality gates in the {pillar} pillar for region {region}. Distill the
common logic — what investment idea or market mechanism do they share?
Be specific: name the variables, the signs, the time scales.

Constraints:
 - Output ONLY 1-3 sentences (≤ 60 words total).
 - DO NOT quote the expressions verbatim.
 - DO NOT speculate beyond what's evident in the shared structure.

PASS alphas:
{alpha_list}

Distilled logic:"""


# ---------------------------------------------------------------------------
# Tokenization (for Jaccard similarity)
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,}")


def tokenize(text: str) -> List[str]:
    """Lower-case word tokens, drop 1-char garbage. Used for Jaccard
    similarity between distilled-logic entries."""
    if not text:
        return []
    return [m.group(0).lower() for m in _WORD_RE.finditer(text)]


def jaccard_similarity(a: Sequence[str], b: Sequence[str]) -> float:
    """Standard Jaccard: |A ∩ B| / |A ∪ B|."""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union > 0 else 0.0


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class AlphaSummary:
    """One PASS alpha condensed into prompt-friendly fields."""
    id: int
    expression: str
    sharpe: float = 0.0


@dataclass
class DistilledEntry:
    """Result of one distillation — to be written to the library table."""
    pillar: Optional[str]
    region: str
    logic_text: str
    source_alpha_ids: List[int] = field(default_factory=list)
    tokens: List[str] = field(default_factory=list)
    llm_cost_usd: float = 0.0
    llm_model: Optional[str] = None
    distilled_at_week: Optional[datetime] = None
    similarity_jaccard_to_prev_week: Optional[float] = None


# ---------------------------------------------------------------------------
# Grouping + prompt construction
# ---------------------------------------------------------------------------

def _week_anchor(now: Optional[datetime] = None) -> datetime:
    """Return the Monday 00:00 (Asia/Shanghai) of the week ``now`` belongs to.

    F2 review fix (Sprint 3 R2): the cron is scheduled at Sun 03:00 SH
    (= Sat 19:00 UTC). The prior implementation derived weekday from
    UTC, so a retry at Mon 00:30 SH (= Sun 16:30 UTC) hit weekday=6 and
    anchored to the NEXT Monday → two rows for the same SH-week with
    different distilled_at_week values, both 'active'. Use Asia/Shanghai
    so the week boundary aligns with the cron schedule and the unique
    constraint (distilled_at_week, region, pillar) actually catches
    double-fires.
    """
    try:
        from zoneinfo import ZoneInfo
        sh_tz = ZoneInfo("Asia/Shanghai")
    except Exception:
        # zoneinfo missing → fall back to UTC (drift but no crash)
        sh_tz = timezone.utc
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_sh = now_utc.astimezone(sh_tz)
    monday_sh = now_sh - timedelta(days=now_sh.weekday())
    monday_sh = monday_sh.replace(hour=0, minute=0, second=0, microsecond=0)
    # Persist as UTC so DB comparisons stay consistent (TIMESTAMP WITH TIME ZONE).
    return monday_sh.astimezone(timezone.utc)


def _group_by_pillar_region(
    alphas: Sequence[AlphaSummary],
    *,
    region_resolver: Optional[Any] = None,
    pillar_resolver: Optional[Any] = None,
) -> Dict[Tuple[str, str], List[AlphaSummary]]:
    """Bucket alphas by (pillar, region). Caller supplies pre-resolved
    region+pillar attributes on each AlphaSummary — at the DB level,
    region is a column and pillar is in metrics.

    region_resolver / pillar_resolver are optional callables of
    ``(AlphaSummary) -> str`` for tests; production passes them as
    None and the AlphaSummary is expected to carry the keys directly.
    """
    raise NotImplementedError(
        "_group_by_pillar_region is not used in PR1 — distill_last_week_pass_alphas "
        "queries DB directly with GROUP BY"
    )


def build_distill_prompt(
    *,
    pillar: str,
    region: str,
    alphas: Sequence[AlphaSummary],
    template: str = _DEFAULT_DISTILL_PROMPT,
) -> str:
    """Render the LLM prompt for one (pillar, region) bucket.

    F9 review fix (Sprint 3 R2): the prior implementation used
    ``template.format(...)`` to substitute placeholders. BRAIN
    expressions occasionally contain ``{...}`` (e.g. parametric
    operator templates), and ``.format()`` interprets those as
    placeholders → ``KeyError`` → exception bubbles up OUTSIDE the
    LLM-call try/except → kills the entire weekly distill run.
    Use explicit ``str.replace`` substitution instead.
    """
    lines = [
        f"  - id={a.id} sharpe={a.sharpe:.2f}: `{a.expression}`"
        for a in alphas
    ]
    alpha_list = "\n".join(lines)
    return (
        template
        .replace("{count}", str(len(alphas)))
        .replace("{pillar}", pillar)
        .replace("{region}", region)
        .replace("{alpha_list}", alpha_list)
    )


# ---------------------------------------------------------------------------
# Distill orchestrator
# ---------------------------------------------------------------------------

async def distill_last_week_pass_alphas(
    db: Any,
    llm: Any,
    *,
    max_cost_usd: float = 5.00,
    top_k_per_group: int = 10,
    min_pass_count: int = 3,
    lookback_days: int = 7,
    week_anchor: Optional[datetime] = None,
) -> List[DistilledEntry]:
    """Walk past `lookback_days` of PASS alphas grouped by (region, pillar),
    LLM-distill each group, return list of DistilledEntry ready to INSERT.

    Stops dispatching new LLM calls once accumulated cost exceeds
    ``max_cost_usd``. Buckets with < ``min_pass_count`` alphas are
    skipped (insufficient data to draw a pattern).

    Soft-fail: any LLM call exception is caught + logged + the bucket
    is skipped. Returned entries are write-ready; caller (cron task)
    does the INSERT in one transaction.

    Args:
        db: AsyncSession.
        llm: LLMService-shaped object with async ``call(prompt) -> dict``
            returning at least {"text": str, "cost_usd": float, "model": str}.
        max_cost_usd: hard cap on this week's distillation spend.
        top_k_per_group: take top-K alphas per bucket ordered by sharpe desc.
        min_pass_count: skip buckets with fewer PASS alphas.
        lookback_days: how many days back to walk (default 7).
        week_anchor: stable Monday-of-week anchor (UTC) for the distilled_
            at_week column. None → derive from now.

    Returns:
        List[DistilledEntry] — caller writes to distilled_logic_library.
    """
    from sqlalchemy import text as _text

    week = week_anchor or _week_anchor()
    spent_usd = 0.0
    out: List[DistilledEntry] = []

    # Group + filter via SQL (cheap; avoids loading all alphas into memory).
    # Pillar can live in metrics['pillar'] (preferred) or fall back to
    # NULL (handled by COALESCE in the SELECT).
    rows = (await db.execute(_text(f"""
        SELECT
          region,
          COALESCE(metrics->>'pillar', NULL) AS pillar,
          id,
          alpha_id,
          expression,
          COALESCE(is_sharpe, 0.0) AS sharpe
        FROM alphas
        WHERE quality_status IN ('PASS', 'PASS_PROVISIONAL')
          AND created_at > now() - (:days || ' day')::interval
          AND expression IS NOT NULL
        ORDER BY region, COALESCE(metrics->>'pillar', ''), is_sharpe DESC NULLS LAST
    """), {"days": str(int(lookback_days))})).all()

    # Bucket Python-side (handle NULL pillar uniformly)
    buckets: Dict[Tuple[str, Optional[str]], List[AlphaSummary]] = {}
    for region, pillar, alpha_id, _brain_id, expr, sharpe in rows:
        key = (str(region), pillar if pillar else None)
        if key not in buckets:
            buckets[key] = []
        if len(buckets[key]) < top_k_per_group:
            buckets[key].append(AlphaSummary(
                id=int(alpha_id),
                expression=str(expr),
                sharpe=float(sharpe or 0.0),
            ))

    _calls_made = 0
    for (region, pillar), bucket_alphas in buckets.items():
        if len(bucket_alphas) < min_pass_count:
            continue
        # D5 review fix: cost cap was checked BEFORE dispatch but updated
        # AFTER → could overshoot by one full call (a $0.50 extended-
        # thinking call on the last bucket before the cap). Project the
        # NEXT call's cost from the running average (conservative default
        # before the first call) and stop if it would cross the cap.
        _avg_cost = (spent_usd / _calls_made) if _calls_made > 0 else 0.0
        if spent_usd + _avg_cost >= max_cost_usd:
            logger.warning(
                f"[g10] cost cap ${max_cost_usd:.2f} would be crossed "
                f"(spent ${spent_usd:.4f}, est next ${_avg_cost:.4f}); "
                f"stopping with {len(out)} bucket(s) distilled"
            )
            break

        prompt = build_distill_prompt(
            pillar=pillar or "general",
            region=region,
            alphas=bucket_alphas,
        )
        try:
            resp = await llm.call(prompt)
        except Exception as e:
            logger.warning(
                f"[g10] LLM call failed for ({region}, {pillar}) bucket: {e}"
            )
            continue

        if not isinstance(resp, dict):
            logger.warning(
                f"[g10] LLM returned non-dict for ({region}, {pillar}); skip"
            )
            continue

        logic_text = (resp.get("text") or "").strip()
        if not logic_text:
            logger.warning(f"[g10] empty LLM text for ({region}, {pillar}); skip")
            continue

        cost = float(resp.get("cost_usd") or 0.0)
        spent_usd += cost
        _calls_made += 1

        out.append(DistilledEntry(
            pillar=pillar,
            region=region,
            logic_text=logic_text,
            source_alpha_ids=[a.id for a in bucket_alphas],
            tokens=tokenize(logic_text),
            llm_cost_usd=cost,
            llm_model=str(resp.get("model") or ""),
            distilled_at_week=week,
        ))

    logger.info(
        f"[g10] distilled {len(out)} bucket(s) (spent ${spent_usd:.2f} of "
        f"${max_cost_usd:.2f} cap)"
    )
    return out


# ---------------------------------------------------------------------------
# Similarity stamp helper (called by cron after the INSERT)
# ---------------------------------------------------------------------------

async def stamp_similarity_to_prev_week(
    db: Any,
    new_entries: List[DistilledEntry],
) -> None:
    """Compute Jaccard token similarity between each new entry and the
    most-recent prior entry in the same (region, pillar). Stamps
    new_entries in-place — caller flushes again.

    Soft-fail: missing prior / DB error → similarity stays None.
    """
    from sqlalchemy import text as _text

    for entry in new_entries:
        try:
            row = (await db.execute(_text("""
                SELECT tokens
                FROM distilled_logic_library
                WHERE region = :region
                  AND (
                    (pillar IS NULL AND :pillar IS NULL)
                    OR pillar = :pillar
                  )
                  AND distilled_at_week < :week
                ORDER BY distilled_at_week DESC
                LIMIT 1
            """), {
                "region": entry.region,
                "pillar": entry.pillar,
                "week": entry.distilled_at_week,
            })).first()
        except Exception:  # noqa: BLE001
            continue
        if row is None:
            continue
        prev_tokens = row[0] or []
        if not isinstance(prev_tokens, list):
            continue
        sim = jaccard_similarity(entry.tokens, prev_tokens)
        entry.similarity_jaccard_to_prev_week = sim


# ---------------------------------------------------------------------------
# A5.2 G10 PR2 (Sprint 4): refine + retrieval + prompt block
# ---------------------------------------------------------------------------

async def refine_logic_library(
    db: Any,
    *,
    similarity_threshold: float = 0.70,
    lookback_weeks: int = 4,
) -> Dict[str, int]:
    """Mark stale logic entries as retired_at = now.

    Strategy:
      For each active (retired_at IS NULL) (region, pillar) bucket,
      compare the newest entry's tokens against the next-newest entry's
      tokens via Jaccard. When similarity ≥ ``similarity_threshold``,
      the OLDER entry is redundant — set its retired_at so it stops
      appearing in retrieval. Repeat through the bucket's history up
      to ``lookback_weeks`` deep.

    Returns: {"retired": N, "checked": M} for cron log + ops telemetry.

    Soft-fail per bucket — any DB error logged + bucket skipped.
    """
    from sqlalchemy import text as _text
    retired = 0
    checked = 0

    # Bucket inventory (still-active entries) grouped by region+pillar.
    # F6 review fix (Sprint 4 R1+R2): also fetch source_alpha_ids count
    # so we only retire the OLDER entry when the NEWER one is not strictly
    # weaker (≥ source coverage). Prevents the thrash where each week's
    # distill re-creates a near-dup and refine blindly retires the
    # accumulated history — the surviving entry should be the one backed
    # by more PASS alphas, not just the newest.
    bucket_rows = (await db.execute(_text("""
        SELECT id, region, pillar, distilled_at_week, tokens, source_alpha_ids
        FROM distilled_logic_library
        WHERE retired_at IS NULL
        ORDER BY region, pillar, distilled_at_week DESC
    """))).all()

    # Group Python-side; tokens + source_alpha_ids columns are JSONB lists
    groups: Dict[Tuple[str, Optional[str]], List[tuple]] = {}
    for row_id, region, pillar, week, tokens, source_ids in bucket_rows:
        _tok = list(tokens) if isinstance(tokens, list) else []
        _src_n = len(source_ids) if isinstance(source_ids, list) else 0
        groups.setdefault((str(region), pillar), []).append(
            (int(row_id), week, _tok, _src_n)
        )

    now = datetime.now(timezone.utc)
    for (region, pillar), members in groups.items():
        if len(members) < 2:
            continue
        # members is already DESC by week. Compare newest [0] vs older [i].
        # Retire the older when (a) similar enough AND (b) the newest is
        # not weaker (≥ source coverage). When the older entry is backed
        # by MORE alphas, keep it (the newest is a thinner re-distillation).
        newest_src_n = members[0][3]
        for i in range(1, min(len(members), lookback_weeks + 1)):
            checked += 1
            sim = jaccard_similarity(members[0][2], members[i][2])
            older_src_n = members[i][3]
            if sim >= similarity_threshold and newest_src_n >= older_src_n:
                try:
                    await db.execute(
                        _text(
                            "UPDATE distilled_logic_library SET retired_at = :now "
                            "WHERE id = :id"
                        ),
                        {"now": now, "id": members[i][0]},
                    )
                    retired += 1
                except Exception as ex:  # noqa: BLE001
                    logger.warning(
                        f"[g10] refine UPDATE failed for id={members[i][0]}: {ex}"
                    )

    try:
        await db.commit()
    except Exception as ex:  # noqa: BLE001
        await db.rollback()
        logger.warning(f"[g10] refine commit failed (rolled back): {ex}")
        return {"retired": 0, "checked": checked, "error": str(ex)[:200]}

    logger.info(f"[g10] refine: retired {retired}/{checked} stale entries")
    return {"retired": retired, "checked": checked}


async def fetch_active_logic_entries(
    db: Any,
    *,
    region: str,
    pillar: Optional[str] = None,
    limit: int = 5,
) -> List[Dict]:
    """Retrieve active distilled-logic entries for hypothesis prompt
    injection. Filters retired_at IS NULL, prefers (region, pillar)
    match but soft-falls to region-only when pillar match returns < limit.

    Returns: ordered list of {logic_text, pillar, region, distilled_at_week,
    source_alpha_count, llm_model}. Newest first.
    """
    from sqlalchemy import text as _text

    out: List[Dict] = []
    if pillar:
        # Try pillar-matched first
        try:
            rows = (await db.execute(_text("""
                SELECT id, logic_text, pillar, region, distilled_at_week,
                       source_alpha_ids, llm_model
                FROM distilled_logic_library
                WHERE retired_at IS NULL
                  AND region = :region
                  AND pillar = :pillar
                ORDER BY distilled_at_week DESC
                LIMIT :limit
            """), {"region": region, "pillar": pillar, "limit": int(limit)})).all()
            out = [_row_to_dict(r) for r in rows]
        except Exception as ex:  # noqa: BLE001
            logger.warning(f"[g10] fetch (pillar-matched) failed: {ex}")
            return []

    if len(out) < limit:
        remaining = limit - len(out)
        seen_ids = [r["id"] for r in out]
        # D4 review fix: exclude already-seen ids in SQL + LIMIT to the
        # actual remaining count (was LIMIT limit*2 then Python-side
        # dedup — the *2 cap could be exhausted by already-seen rows,
        # leaving `out` short even when more eligible rows existed).
        params: Dict[str, Any] = {"region": region, "remaining": int(remaining)}
        exclude_sql = ""
        if seen_ids:
            ph = ", ".join(f":seen{i}" for i in range(len(seen_ids)))
            exclude_sql = f"AND id NOT IN ({ph})"
            for i, sid in enumerate(seen_ids):
                params[f"seen{i}"] = int(sid)
        try:
            rows = (await db.execute(_text(f"""
                SELECT id, logic_text, pillar, region, distilled_at_week,
                       source_alpha_ids, llm_model
                FROM distilled_logic_library
                WHERE retired_at IS NULL
                  AND region = :region
                  {exclude_sql}
                ORDER BY distilled_at_week DESC
                LIMIT :remaining
            """), params)).all()
            for r in rows:
                out.append(_row_to_dict(r))
                if len(out) >= limit:
                    break
        except Exception as ex:  # noqa: BLE001
            logger.warning(f"[g10] fetch (region-fallback) failed: {ex}")

    return out[:limit]


def _row_to_dict(row: tuple) -> Dict:
    row_id, logic_text, pillar, region, week, source_ids, llm_model = row
    if isinstance(source_ids, list):
        src_count = len(source_ids)
    else:
        src_count = 0
    return {
        "id": int(row_id),
        "logic_text": str(logic_text),
        "pillar": str(pillar) if pillar else None,
        "region": str(region),
        "distilled_at_week": week,
        "source_alpha_count": src_count,
        "llm_model": str(llm_model) if llm_model else None,
    }


def build_distilled_logic_block(entries: List[Dict], *, max_entries: int = 5) -> str:
    """Render the G10 distilled-logic block for hypothesis prompt injection.

    Empty input → "" so the splice site collapses to byte-for-byte
    legacy when ENABLE_G10_LOGIC_INJECT is OFF or no rows exist
    (mirrors P2-A/B/C/D + G8 + R8-v3 contract).

    F7 review fix (Sprint 4 R3): ``max_entries`` is now a parameter
    (was a hard-coded [:5]). The caller passes G10_LOGIC_INJECT_TOP_K so
    setting the flag > 5 actually renders that many entries instead of
    silently truncating at 5.
    """
    if not entries:
        return ""
    lines = [
        "## Distilled Logic — Recent PASS-Alpha Patterns (this region)",
        "",
        (
            "These are LLM-distilled summaries of common logic across "
            "alphas that recently PASSed in this region. Use as a research "
            "*prior*: extend a pattern that aligns with your hypothesis, "
            "or propose a genuinely orthogonal direction if none fit. "
            "Do NOT verbatim copy."
        ),
        "",
    ]
    for e in entries[:max_entries]:
        pillar = e.get("pillar") or "general"
        src_n = e.get("source_alpha_count", 0)
        logic = (e.get("logic_text") or "").strip()
        if not logic:
            continue
        lines.append(f"- **{pillar}** (n={src_n}): {logic}")
    return "\n".join(lines)


__all__ = [
    "AlphaSummary",
    "DistilledEntry",
    "tokenize",
    "jaccard_similarity",
    "build_distill_prompt",
    "distill_last_week_pass_alphas",
    "stamp_similarity_to_prev_week",
    "refine_logic_library",
    "fetch_active_logic_entries",
    "build_distilled_logic_block",
]
