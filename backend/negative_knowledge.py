"""Negative Knowledge — failure-pattern sedimentation (P2-D, 2026-05-15).

来源: docs/alphagbm_skills_research_2026-05-15.md skills `take-profit`
+ `health-check` — "negative knowledge 沉淀 + 状态机 → 把失败教训沉淀回 KB,
避免下一轮重复"。

Pure functions only. No DB / Celery / settings access — the service layer
binds these to ``backend.services.negative_knowledge_service`` which owns
the DB session + UPSERT. Unit-test friendly (aiosqlite-safe).

Inputs:
  - Alpha rows with metrics that contain ``_validation_findings`` (P1-E),
    ``_robustness_failed`` (P1-D), ``failed_tests`` / ``_failed_tests``
  - AlphaFailure rows with ``error_type`` (sim_error) — region resolved
    via service-layer outerjoin to the parent Alpha row
  - HypothesisRoundStats rows with ``attribution == 'hypothesis'`` joined
    to the parent Hypothesis row for ``trigger_detail`` aggregation

Outputs:
  - ``FailureSignature`` dataclass instances ready for aggregation +
    UPSERT into ``knowledge_entries`` with entry_type='FAILURE_PITFALL'
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from backend.knowledge_extraction import expression_to_skeleton


# ---------------------------------------------------------------------------
# Remediation hints — M1 corrected literals from hypothesis_health_service.py:
#   L176 "dropped_sharpe_pct" / L210 "no_pass_in_n_rounds" /
#   L262 "pass_rate_drop" / L295 "attribution_hypothesis_dominant" /
#   L323 "stale_alphas"
# Keys are prefixed ``hyp_trigger_<type>`` to namespace them away from
# static_finding / threshold / robustness / sim_error rule_ids.
# ---------------------------------------------------------------------------
_REMEDIATION_HINTS: Dict[str, str] = {
    # Hypothesis triggers (T1-T5)
    "hyp_trigger_dropped_sharpe_pct":
        "Family Sharpe dropped vs baseline — investigate dataset regime "
        "change or pillar pivot.",
    "hyp_trigger_no_pass_in_n_rounds":
        "No PASS across N consecutive rounds — pillar pivot or hypothesis "
        "abandon recommended.",
    "hyp_trigger_pass_rate_drop":
        "Pass rate dropped sharply — re-examine baseline assumptions.",
    "hyp_trigger_attribution_hypothesis_dominant":
        "Most recent failures attributed to hypothesis (not implementation) — "
        "refine thesis or abandon.",
    "hyp_trigger_stale_alphas":
        "In-scope alphas have gone stale — cohort no longer producing signal.",
}


# ---------------------------------------------------------------------------
# FailureSignature — the atomic record that flows from extractors → aggregator
# → service-layer UPSERT. All fields are JSON-serializable so meta_data can
# hold a list of these in knowledge_entries.meta_data["top_examples"].
# ---------------------------------------------------------------------------
@dataclass
class FailureSignature:
    """Canonical failure pattern. ``signature_key`` is the deduplication
    identity; UPSERTs aggregate by it.

    Categories (6 total, S3 simplification):
      - ``static_finding``   : P1-E Finding rows (rule_id in meta_data)
      - ``threshold``        : failed_tests / _failed_tests entries
      - ``robustness``       : P1-D _robustness_failed entries
      - ``sim_error``        : AlphaFailure.error_type (region may be "")
      - ``hyp_trigger``      : HypothesisRoundStats trigger_detail entries
      - ``attribution``      : HypothesisRoundStats attribution=='hypothesis'
    """

    signature_key: str            # sha1(rule_id|skeleton|region)[:16]
    rule_id: str                  # e.g. RISK_DIVIDE_BY_VOLATILE_DENOM
    skeleton: str                 # expression_to_skeleton output or "UNKNOWN"
    region: str                   # USA / EUR / "" (sim_error cross-region)
    category: str                 # see enum-ish list above
    severity: str                 # red / orange / yellow / info
    expected_signal: str          # what the failed pattern was trying to do
    remediation_hint: str         # short LLM-facing fix advice
    failure_count: int = 1
    top_examples: List[Dict[str, Any]] = field(default_factory=list)
    first_seen_at: Optional[str] = None  # ISO-8601 UTC
    last_seen_at: Optional[str] = None   # ISO-8601 UTC


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
def _skeletonize(expression: Optional[str]) -> str:
    """Wrap knowledge_extraction.expression_to_skeleton with hard-fail
    safety. Empty / unparseable → ``"UNKNOWN"`` (filtered out by
    fetch_top_pitfalls S1 guard)."""
    if not expression or not isinstance(expression, str):
        return "UNKNOWN"
    try:
        sk = expression_to_skeleton(expression.strip(), max_depth=3)
        return sk or "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def compute_signature_key(rule_id: str, skeleton: str, region: str) -> str:
    """Stable identity hash. 16 chars sha1 is enough — collisions are
    cheap (an extra row gets merged into the wrong sig, but only if two
    different (rule_id, skeleton, region) tuples collide which is
    astronomically rare for sha1[:16])."""
    raw = f"{rule_id or ''}|{skeleton or ''}|{region or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _now_iso(now_utc: Optional[datetime] = None) -> str:
    """ISO-8601 UTC; helper to keep tests deterministic via injection."""
    dt = now_utc or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _hint_for(rule_id: str, fallback: str = "") -> str:
    """Look up remediation hint by rule_id; for static/threshold/robustness/
    sim_error rule_ids we use the rule_id itself as the message so the LLM
    sees a meaningful pointer even without a curated hint."""
    if rule_id in _REMEDIATION_HINTS:
        return _REMEDIATION_HINTS[rule_id]
    return fallback or f"Pattern flagged by {rule_id}"


def _make_example(alpha_id: Any, expression: str, when: str) -> Dict[str, Any]:
    """Build a single ``top_examples`` entry. Expression truncated to 240
    chars to keep meta_data row small."""
    return {
        "alpha_id": str(alpha_id) if alpha_id is not None else "",
        "expression": (expression or "")[:240],
        "at": when,
    }


def _merge_examples(
    old: List[Dict[str, Any]],
    new: List[Dict[str, Any]],
    *,
    keep: int = 5,
) -> List[Dict[str, Any]]:
    """S6 shared reservoir helper — used by both ``aggregate_signatures`` and
    ``upsert_pitfalls`` so the eviction rule is identical across the two
    paths.

    Strategy: keep the FIRST 3 (oldest survivors — preserves the historical
    canary) + the MOST-RECENT 2 (newest — preserves "is it still firing").
    Dedup by (alpha_id, expression[:60]) tuple.
    """
    seen = set()
    merged: List[Dict[str, Any]] = []
    for ex in (old or []) + (new or []):
        if not isinstance(ex, dict):
            continue
        key = (
            str(ex.get("alpha_id", "")),
            str(ex.get("expression", ""))[:60],
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(ex)

    if len(merged) <= keep:
        return merged

    # First 3 oldest + last 2 newest. Order by ``at`` ascending if present.
    def _at(e: Dict[str, Any]) -> str:
        return str(e.get("at", "")) or ""

    merged_sorted = sorted(merged, key=_at)
    first_k = min(3, max(0, keep - 2))
    last_k = keep - first_k
    return merged_sorted[:first_k] + merged_sorted[-last_k:]


# ---------------------------------------------------------------------------
# Extractors — one per data source
# ---------------------------------------------------------------------------
def _ensure_metrics(alpha: Any) -> Dict[str, Any]:
    m = getattr(alpha, "metrics", None) or {}
    return m if isinstance(m, dict) else {}


def extract_failures_from_alpha(
    alpha: Any,
    hypothesis: Optional[Any] = None,
    now_utc: Optional[datetime] = None,
) -> List[FailureSignature]:
    """Pull static / threshold / robustness signatures off an Alpha row.

    Dedups within the row by signature_key so a single alpha with three
    findings for the same (rule_id, skeleton, region) yields ONE
    FailureSignature with failure_count=1 (not 3) — counts are incremented
    at upsert time across alphas, not within an alpha.
    """
    when = _now_iso(now_utc)
    region = getattr(alpha, "region", "") or ""
    expr = getattr(alpha, "expression", "") or ""
    skeleton = _skeletonize(expr)
    alpha_id = getattr(alpha, "alpha_id", None) or getattr(alpha, "id", None)

    metrics = _ensure_metrics(alpha)
    out: List[FailureSignature] = []
    seen_keys: set = set()

    # 1) P1-E _validation_findings → static_finding category (S3: all
    #    under one category; rule_id distinguishes RISK_* / STATIC_*)
    findings = metrics.get("_validation_findings") or []
    if isinstance(findings, list):
        for f in findings:
            if not isinstance(f, dict):
                continue
            rule_id = str(f.get("rule_id") or f.get("code") or "").strip()
            if not rule_id:
                continue
            severity = str(f.get("severity") or "info").lower()
            expected = str(f.get("expected_signal") or f.get("message") or "")
            key = compute_signature_key(rule_id, skeleton, region)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(FailureSignature(
                signature_key=key,
                rule_id=rule_id,
                skeleton=skeleton,
                region=region,
                category="static_finding",
                severity=severity,
                expected_signal=expected[:240],
                remediation_hint=_hint_for(rule_id, expected[:200]),
                failure_count=1,
                top_examples=[_make_example(alpha_id, expr, when)],
                first_seen_at=when,
                last_seen_at=when,
            ))

    # 2) failed_tests / _failed_tests → threshold category
    for ft_key in ("failed_tests", "_failed_tests"):
        ft = metrics.get(ft_key) or []
        if not isinstance(ft, list):
            continue
        for item in ft:
            # Item shapes vary; accept str (rule name) or dict
            if isinstance(item, str):
                rule_id = f"threshold:{item.strip().lower()}"
                expected = item
                severity = "orange"
            elif isinstance(item, dict):
                rule_id_raw = (
                    item.get("rule") or item.get("name") or
                    item.get("test") or item.get("metric") or ""
                )
                if not rule_id_raw:
                    continue
                rule_id = f"threshold:{str(rule_id_raw).strip().lower()}"
                expected = str(item.get("message") or item.get("expected") or "")
                severity = str(item.get("severity") or "orange").lower()
            else:
                continue
            key = compute_signature_key(rule_id, skeleton, region)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(FailureSignature(
                signature_key=key,
                rule_id=rule_id,
                skeleton=skeleton,
                region=region,
                category="threshold",
                severity=severity,
                expected_signal=expected[:240],
                remediation_hint=_hint_for(
                    rule_id,
                    f"Failed threshold check: {rule_id.split(':', 1)[-1]}",
                ),
                failure_count=1,
                top_examples=[_make_example(alpha_id, expr, when)],
                first_seen_at=when,
                last_seen_at=when,
            ))

    # 3) _robustness_failed → robustness category
    rb = metrics.get("_robustness_failed")
    if rb:
        # Accept list of rule names, list of dicts, or truthy flag
        items: List[Any]
        if isinstance(rb, list):
            items = rb
        elif isinstance(rb, dict):
            items = [rb]
        else:
            items = [{"name": "robustness_failed"}]
        for item in items:
            if isinstance(item, str):
                rule_id = f"robustness:{item.strip().lower()}"
                expected = item
                severity = "orange"
            elif isinstance(item, dict):
                rid_raw = (
                    item.get("rule") or item.get("name") or
                    item.get("test") or item.get("check") or "robustness_failed"
                )
                rule_id = f"robustness:{str(rid_raw).strip().lower()}"
                expected = str(item.get("message") or item.get("reason") or "")
                severity = str(item.get("severity") or "orange").lower()
            else:
                continue
            key = compute_signature_key(rule_id, skeleton, region)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(FailureSignature(
                signature_key=key,
                rule_id=rule_id,
                skeleton=skeleton,
                region=region,
                category="robustness",
                severity=severity,
                expected_signal=expected[:240],
                remediation_hint=_hint_for(
                    rule_id, "Failed robustness gate — check region/universe variance"
                ),
                failure_count=1,
                top_examples=[_make_example(alpha_id, expr, when)],
                first_seen_at=when,
                last_seen_at=when,
            ))

    # 4) hypothesis trigger_detail (when caller supplies hypothesis arg) —
    #    emitted ONCE per (alpha, trigger.type) tuple.
    if hypothesis is not None:
        td = getattr(hypothesis, "trigger_detail", None)
        if isinstance(td, dict):
            for trigger_type, hit in td.items():
                if not isinstance(hit, dict):
                    continue
                rule_id = f"hyp_trigger_{str(trigger_type).strip()}"
                severity = str(hit.get("severity") or "orange").lower()
                key = compute_signature_key(rule_id, skeleton, region)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                out.append(FailureSignature(
                    signature_key=key,
                    rule_id=rule_id,
                    skeleton=skeleton,
                    region=region,
                    category="hyp_trigger",
                    severity=severity,
                    expected_signal=str(hit.get("reason") or "")[:240],
                    remediation_hint=_hint_for(rule_id),
                    failure_count=1,
                    top_examples=[_make_example(alpha_id, expr, when)],
                    first_seen_at=when,
                    last_seen_at=when,
                ))

    return out


def extract_failures_from_alpha_failure(
    failure: Any,
    now_utc: Optional[datetime] = None,
) -> List[FailureSignature]:
    """AlphaFailure row → 1 ``sim_error`` signature.

    S5: AlphaFailure has no ``region`` column. The service layer left-joins
    Alpha and stamps the resolved region on ``failure._resolved_region``
    before calling this function. Fallback to "" — fetch_top_pitfalls then
    treats sim_error region="" as cross-region applicable.
    """
    when = _now_iso(now_utc)
    region = getattr(failure, "_resolved_region", None) or ""
    expr = getattr(failure, "expression", "") or ""
    skeleton = _skeletonize(expr)
    error_type = (getattr(failure, "error_type", "") or "").strip()
    if not error_type:
        return []
    rule_id = f"sim_error:{error_type.lower()}"
    msg = getattr(failure, "error_message", "") or ""
    failure_id = getattr(failure, "id", None)

    key = compute_signature_key(rule_id, skeleton, region)
    return [FailureSignature(
        signature_key=key,
        rule_id=rule_id,
        skeleton=skeleton,
        region=region,
        category="sim_error",
        severity="red" if "error" in error_type.lower() else "orange",
        expected_signal=msg[:240],
        remediation_hint=_hint_for(
            rule_id, f"BRAIN simulate rejected with {error_type}"
        ),
        failure_count=1,
        top_examples=[_make_example(
            f"failure:{failure_id}" if failure_id else "", expr, when,
        )],
        first_seen_at=when,
        last_seen_at=when,
    )]


def extract_failures_from_hypothesis_round(
    round_stats: Any,
    hypothesis: Any,
    now_utc: Optional[datetime] = None,
) -> List[FailureSignature]:
    """HypothesisRoundStats with attribution=='hypothesis' → 1 attribution
    signature on the hypothesis's REGION (universe-agnostic).

    Other attribution values ('implementation' / 'both' / 'unknown' / None)
    yield empty list — implementation failures are not hypothesis-level
    knowledge worth sedimenting (they're code-bugs, not thesis-bugs).
    """
    if round_stats is None or hypothesis is None:
        return []
    attribution = (getattr(round_stats, "attribution", None) or "").strip().lower()
    if attribution != "hypothesis":
        return []

    when = _now_iso(now_utc)
    region = getattr(hypothesis, "region", "") or ""
    # No expression context here — use a synthetic skeleton tag so different
    # hypotheses don't collide on the same UNKNOWN bucket.
    statement = (getattr(hypothesis, "statement", "") or "")[:120]
    skeleton = f"hyp:{statement[:60]}" if statement else "UNKNOWN"
    rule_id = "attribution_hypothesis"
    hid = getattr(hypothesis, "id", None)
    reason = getattr(round_stats, "attribution_reason", "") or ""

    key = compute_signature_key(rule_id, skeleton, region)
    return [FailureSignature(
        signature_key=key,
        rule_id=rule_id,
        skeleton=skeleton,
        region=region,
        category="attribution",
        severity="orange",
        expected_signal=reason[:240],
        remediation_hint=_hint_for(
            "hyp_trigger_attribution_hypothesis_dominant",
            "Hypothesis-attributed failure — refine thesis or abandon.",
        ),
        failure_count=1,
        top_examples=[_make_example(
            f"hyp:{hid}" if hid else "", statement, when,
        )],
        first_seen_at=when,
        last_seen_at=when,
    )]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def aggregate_signatures(
    sig_list: Iterable[FailureSignature],
) -> Dict[str, FailureSignature]:
    """Collapse a list of single-event signatures into a dict keyed by
    ``signature_key``. failure_count summed, ``top_examples`` reservoir-
    sampled via the shared helper (S6 consistency with upsert_pitfalls)."""
    by_key: Dict[str, FailureSignature] = {}
    for sig in sig_list:
        if sig is None:
            continue
        existing = by_key.get(sig.signature_key)
        if existing is None:
            # Clone to avoid mutating caller's instance
            by_key[sig.signature_key] = FailureSignature(
                signature_key=sig.signature_key,
                rule_id=sig.rule_id,
                skeleton=sig.skeleton,
                region=sig.region,
                category=sig.category,
                severity=sig.severity,
                expected_signal=sig.expected_signal,
                remediation_hint=sig.remediation_hint,
                failure_count=sig.failure_count,
                top_examples=list(sig.top_examples or []),
                first_seen_at=sig.first_seen_at,
                last_seen_at=sig.last_seen_at,
            )
            continue
        existing.failure_count += sig.failure_count
        existing.top_examples = _merge_examples(
            existing.top_examples, sig.top_examples, keep=5,
        )
        # first_seen_at = MIN; last_seen_at = MAX (string ISO sort works)
        if sig.first_seen_at and (
            not existing.first_seen_at or
            sig.first_seen_at < existing.first_seen_at
        ):
            existing.first_seen_at = sig.first_seen_at
        if sig.last_seen_at and (
            not existing.last_seen_at or
            sig.last_seen_at > existing.last_seen_at
        ):
            existing.last_seen_at = sig.last_seen_at
        # Severity escalation: red > orange > yellow > info
        _SEV_RANK = {"red": 3, "orange": 2, "yellow": 1, "info": 0}
        if _SEV_RANK.get(sig.severity, 0) > _SEV_RANK.get(existing.severity, 0):
            existing.severity = sig.severity
    return by_key


# ---------------------------------------------------------------------------
# Service-layer helper — pattern text used as KnowledgeEntry.pattern.
# S7: signature_key only (16 hex chars is already a stable UNIQUE identity)
# — DO NOT append skeleton[:60] (two skeletons sharing the same 60-char
# prefix would hash-collide via compute_pattern_hash on the joined string).
# ---------------------------------------------------------------------------
def _pattern_text_for(sig: FailureSignature) -> str:
    """KnowledgeEntry.pattern body. The signature_key alone is the UNIQUE
    identity. The skeleton goes into description/meta_data for humans."""
    return f"PITFALL::{sig.signature_key}"


__all__ = [
    "FailureSignature",
    "_REMEDIATION_HINTS",
    "_skeletonize",
    "_merge_examples",
    "_pattern_text_for",
    "compute_signature_key",
    "extract_failures_from_alpha",
    "extract_failures_from_alpha_failure",
    "extract_failures_from_hypothesis_round",
    "aggregate_signatures",
]
