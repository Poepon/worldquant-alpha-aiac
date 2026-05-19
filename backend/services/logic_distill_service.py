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
    """Return the Monday 00:00 of the week the `now` belongs to (UTC).

    Stable anchor so two distillations on Sunday vs Monday hit the same
    week-key (avoids dup rows when the cron runs at week boundary).
    """
    now = now or datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


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
    """Render the LLM prompt for one (pillar, region) bucket."""
    lines = [
        f"  - id={a.id} sharpe={a.sharpe:.2f}: `{a.expression}`"
        for a in alphas
    ]
    return template.format(
        count=len(alphas),
        pillar=pillar,
        region=region,
        alpha_list="\n".join(lines),
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

    for (region, pillar), bucket_alphas in buckets.items():
        if len(bucket_alphas) < min_pass_count:
            continue
        if spent_usd >= max_cost_usd:
            logger.warning(
                f"[g10] cost cap ${max_cost_usd:.2f} reached (spent ${spent_usd:.2f}); "
                f"skipping remaining {len(buckets) - len(out)} bucket(s)"
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


__all__ = [
    "AlphaSummary",
    "DistilledEntry",
    "tokenize",
    "jaccard_similarity",
    "build_distill_prompt",
    "distill_last_week_pass_alphas",
    "stamp_similarity_to_prev_week",
]
