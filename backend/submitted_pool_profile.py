"""Submitted-pool pillar coverage profile — Phase A of orthogonality-steered
exploration (docs/orthogonality_steered_exploration_plan_2026-06-05.md).

At a mining session's start, aggregate the REGION's already-submitted alphas into
a pillar-coverage profile (which economic mechanisms are over/under-represented,
their realised Sharpe, over-used fields). The profile is injected as a SOFT NUDGE
into the hypothesis prompt (negative knowledge: "you've over-covered momentum;
explore orthogonal value/quality") so the LLM steers generation toward portfolio-
orthogonal mechanisms — the discovery side of the execution-limited bottleneck.

Pure-ish + zero new infra: one short SQL read + the rule-based ``infer_pillar``
(no LLM, no BRAIN, no Alembic — pillar is computed on the fly from expression).
The module NEVER decides the flag (``ENABLE_ORTHOGONAL_PROMPT_STEERING`` is checked
at the call site). Defensive: any failure → EMPTY profile → ``render_profile_block``
returns "" → the caller's prompt stays byte-for-byte legacy.

Honesty (plan §5): the submitted pool is small (~13) and survivor-biased, so the
rendered block frames coverage neutrally ("explore other"), tags sample sizes, and
the orthogonal target is the highest-mean-Sharpe NON-dominant pillar (e.g. value
@ sh 2.25 vs the dominant momentum) — pointing at orthogonal AND promising, not
just "away from the biggest". Own session per call (F1 / idle-in-txn contract).
"""
from __future__ import annotations

import logging
import statistics
from collections import Counter, defaultdict
from typing import Any, Awaitable, Callable, Dict, List

logger = logging.getLogger(__name__)

_EMPTY: Dict[str, Any] = {"region": None, "n_total": 0, "pillars": {}, "top_fields": []}


async def compute_submitted_pool_profile(
    session_factory: Callable[[], Any],
    region: str,
    *,
    max_alphas: int = 200,
) -> Dict[str, Any]:
    """Aggregate ``region``'s submitted alphas into a pillar-coverage profile.

    Opens its OWN short-lived session (never shares the producer's) so the read
    transaction can't pin a lock across the long mining run (idle-in-txn lesson).

    Returns ``{"region", "n_total", "pillars": {pillar: {"n","mean_sharpe"}},
    "top_fields": [...]}``. On ANY error → empty (n_total=0) so the caller skips
    injection (byte-for-byte legacy).
    """
    from sqlalchemy import text as _text
    from backend.pillar_classifier import infer_pillar
    try:
        from backend.agents.hierarchical_rag import extract_fields_for_rag
    except Exception:  # noqa: BLE001 — field extraction is best-effort
        extract_fields_for_rag = None  # type: ignore[assignment]

    try:
        async with session_factory() as db:
            rows = (await db.execute(_text(
                """
                SELECT expression, is_sharpe
                FROM alphas
                WHERE date_submitted IS NOT NULL AND region = :region
                  AND expression IS NOT NULL
                ORDER BY date_submitted DESC
                LIMIT :lim
                """
            ), {"region": region, "lim": int(max_alphas)})).all()
    except Exception as ex:  # noqa: BLE001
        logger.warning("[pool-profile] query failed (non-fatal): %s", ex)
        return dict(_EMPTY, region=region)

    if not rows:
        return dict(_EMPTY, region=region)

    sharpe_by_pillar: Dict[str, List[float]] = defaultdict(list)
    field_counts: Counter = Counter()
    for expr, sharpe in rows:
        try:
            pillar = infer_pillar(expression=expr or "")
        except Exception:  # noqa: BLE001
            pillar = "other"
        sharpe_by_pillar[pillar].append(float(sharpe) if sharpe is not None else 0.0)
        if extract_fields_for_rag is not None:
            try:
                for f in set(extract_fields_for_rag(expr or "")):
                    field_counts[f] += 1
            except Exception:  # noqa: BLE001
                pass

    pillars = {
        p: {"n": len(shs), "mean_sharpe": round(statistics.mean(shs), 2) if shs else 0.0}
        for p, shs in sharpe_by_pillar.items()
    }
    return {
        "region": region,
        "n_total": len(rows),
        "pillars": pillars,
        "top_fields": [f for f, _ in field_counts.most_common(6)],
    }


def render_profile_block(profile: Dict[str, Any], *, min_pillar_n: int = 2) -> str:
    """Render the profile as a compact (~≤120-token) SOFT-NUDGE prompt block.

    Returns "" when the profile is empty (n_total==0) → caller skips injection
    (byte-for-byte legacy). Neutral framing ("explore other", not "X is bad") to
    avoid survivor-bias dogma; tags sample sizes; the orthogonal target is the
    highest-mean-Sharpe pillar that is NOT the dominant (most-covered) one — i.e.
    orthogonal AND promising. Pillars with n<min_pillar_n are still surfaceable as
    explore-targets (low coverage) but never asserted as "well-covered".
    """
    n = int(profile.get("n_total", 0) or 0)
    pillars: Dict[str, Any] = profile.get("pillars", {}) or {}
    if n == 0 or not pillars:
        return ""

    ranked = sorted(pillars.items(), key=lambda kv: -int(kv[1].get("n", 0)))
    dominant_p, dominant_d = ranked[0]
    region = profile.get("region", "") or ""

    parts: List[str] = [
        f"已提交组合(region {region}, N={n}, 小样本→软参考,仅供探索方向):"
    ]
    # Dominant (well-covered) — only assert if it genuinely dominates (n>=min).
    if int(dominant_d.get("n", 0)) >= min_pillar_n:
        parts.append(
            f"最多覆盖机制 = {dominant_p}({dominant_d['n']}/{n}, sh{dominant_d['mean_sharpe']})."
        )
    # Orthogonal target = highest mean_sharpe among NON-dominant pillars
    # (orthogonal AND promising — e.g. value@2.25 vs dominant momentum@1.68).
    others = [(p, d) for p, d in pillars.items() if p != dominant_p]
    if others:
        tgt_p, tgt_d = max(others, key=lambda kv: float(kv[1].get("mean_sharpe", 0.0)))
        parts.append(
            f"欠覆盖但值得探索 = {tgt_p}(仅 {tgt_d['n']}/{n}, sh{tgt_d['mean_sharpe']})."
        )
    top_fields = profile.get("top_fields") or []
    if top_fields:
        parts.append(f"高频字段: {', '.join(top_fields[:5])}.")
    parts.append(
        "→ 倾向提出与上述最多覆盖机制/高频字段正交的假设(探索其它机制),"
        "而非复制最多的那一类。"
    )
    return " ".join(parts)
