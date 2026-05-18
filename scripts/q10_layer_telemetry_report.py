"""Phase 3 Q10 PR2d: daily Q10 telemetry report (Slack or stdout).

Plan: ~/.claude/plans/phase3-q10-pyqlib-prescreen-2026-05-18.md v1.3 §9 + §15.2.

Aggregates the last N hours of qlib_prescreen_log + alphas-followup data
into a compact ops summary. Posts to Slack when a webhook URL is provided;
otherwise prints to stdout (for cron logs or manual ops review).

Metrics per plan §15.2:
  - rows / verdict breakdown (pass / reject / skip)
  - mode breakdown (shadow / soft / hard)
  - engine tier distribution
  - median + p99 elapsed_ms (latency contract: median ≤ 200ms target)
  - cost_saved% (hard-mode rejects / total prescreens) — proxy for BRAIN
    sim reduction; precise % requires JOIN to alphas (deferred)
  - translation_success_rate (non-untranslatable / total)
  - brain_disagreement_rate (FN rate from brain_followup_disagreement='true')

Threshold alerts (plan §9):
  - fn_rate > 0.15  → ALERT  (Q10 wrongly rejecting alphas BRAIN would PASS)
  - cost_saved < 0.10 → INFO   (low impact — consider lowering floor or
                                 widening translation coverage)

Usage::

    # cron daily 09:00 SH
    python scripts/q10_layer_telemetry_report.py --window-hours 24 \
        --slack-webhook https://hooks.slack.com/services/...

    # stdout for manual ops review
    python scripts/q10_layer_telemetry_report.py --window-hours 168

    # test path: feed a JSON of row dicts
    python scripts/q10_layer_telemetry_report.py --rows-json /tmp/q10_rows.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import statistics
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("q10_layer_telemetry")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


@dataclass
class Q10Row:
    """One qlib_prescreen_log row for aggregation (subset of full schema)."""
    verdict: str
    mode_at_call: str
    engine_kind: str
    elapsed_ms: int
    skip_reason: Optional[str] = None
    brain_disagreement: Optional[str] = None  # 'true' / 'false' / None


@dataclass
class Q10Summary:
    """Aggregated metrics ready for rendering / Slack post."""
    window_hours: int
    total_rows: int = 0
    verdict_counts: Dict[str, int] = field(default_factory=dict)
    mode_counts: Dict[str, int] = field(default_factory=dict)
    engine_counts: Dict[str, int] = field(default_factory=dict)
    median_elapsed_ms: float = 0.0
    p99_elapsed_ms: float = 0.0
    cost_saved_pct: float = 0.0          # hard-mode rejects / total
    translation_success_pct: float = 0.0  # 1 - (skip:untranslatable / total)
    fn_rate: Optional[float] = None       # FN / (FN+TP) from followup, None when insufficient

    @property
    def alert_level(self) -> str:
        """ALERT / INFO / OK per plan §9 thresholds."""
        if self.fn_rate is not None and self.fn_rate > 0.15:
            return "ALERT"
        if self.cost_saved_pct > 0 and self.cost_saved_pct < 10.0:
            return "INFO"
        if self.total_rows == 0:
            return "INFO"
        return "OK"


# ---------------------------------------------------------------------------
# DB query
# ---------------------------------------------------------------------------

async def fetch_rows_from_db(*, window_hours: int) -> List[Q10Row]:
    """Pull recent qlib_prescreen_log rows. Soft-fail to [] on any error."""
    try:
        from sqlalchemy import text
        from backend.database import AsyncSessionLocal
    except Exception as ex:
        logger.warning(f"DB imports unavailable ({ex}); returning empty set")
        return []
    sql = text(
        """
        SELECT verdict, mode_at_call, engine_kind, elapsed_ms,
               skip_reason, brain_disagreement
        FROM qlib_prescreen_log
        WHERE created_at > NOW() - (:hrs || ' hours')::interval
        """
    )
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(sql, {"hrs": str(int(window_hours))})).all()
    except Exception as ex:
        logger.error(f"DB query failed: {ex}")
        return []
    out: List[Q10Row] = []
    for r in rows:
        try:
            out.append(Q10Row(
                verdict=str(r[0] or "unknown"),
                mode_at_call=str(r[1] or "shadow"),
                engine_kind=str(r[2] or "unknown"),
                elapsed_ms=int(r[3] or 0),
                skip_reason=r[4],
                brain_disagreement=r[5],
            ))
        except Exception:
            continue
    return out


def load_rows_from_json(path: str) -> List[Q10Row]:
    with open(path, "r", encoding="utf-8") as fp:
        raw = json.load(fp)
    out: List[Q10Row] = []
    for item in raw:
        try:
            out.append(Q10Row(
                verdict=str(item.get("verdict", "unknown")),
                mode_at_call=str(item.get("mode_at_call", "shadow")),
                engine_kind=str(item.get("engine_kind", "unknown")),
                elapsed_ms=int(item.get("elapsed_ms", 0)),
                skip_reason=item.get("skip_reason"),
                brain_disagreement=item.get("brain_disagreement"),
            ))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _pctile(values: List[int], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(pct * (len(s) - 1)))))
    return float(s[k])


def aggregate(rows: List[Q10Row], *, window_hours: int) -> Q10Summary:
    s = Q10Summary(window_hours=window_hours, total_rows=len(rows))
    if not rows:
        return s
    for r in rows:
        s.verdict_counts[r.verdict] = s.verdict_counts.get(r.verdict, 0) + 1
        s.mode_counts[r.mode_at_call] = s.mode_counts.get(r.mode_at_call, 0) + 1
        s.engine_counts[r.engine_kind] = s.engine_counts.get(r.engine_kind, 0) + 1
    elapsed = [r.elapsed_ms for r in rows]
    s.median_elapsed_ms = float(statistics.median(elapsed)) if elapsed else 0.0
    s.p99_elapsed_ms = _pctile(elapsed, 0.99)
    # Cost saved: only hard-mode rejects actually skipped BRAIN; shadow/soft
    # rejects still went to BRAIN so they don't count.
    hard_rejects = sum(
        1 for r in rows
        if r.verdict == "reject" and r.mode_at_call == "hard"
    )
    s.cost_saved_pct = (hard_rejects / len(rows) * 100.0) if rows else 0.0
    # Translation success
    untrans = sum(1 for r in rows if r.skip_reason == "untranslatable")
    s.translation_success_pct = (1.0 - untrans / len(rows)) * 100.0
    # FN rate from brain_followup_disagreement (only meaningful in shadow/soft)
    disagreements = [r.brain_disagreement for r in rows if r.brain_disagreement in ("true", "false")]
    if len(disagreements) >= 10:
        s.fn_rate = sum(1 for d in disagreements if d == "true") / len(disagreements)
    return s


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def format_report(s: Q10Summary) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append(f"Q10 telemetry — last {s.window_hours}h — {s.alert_level}")
    lines.append("=" * 60)
    if s.total_rows == 0:
        lines.append("No qlib_prescreen_log rows in window (flag OFF? task idle?)")
        lines.append("=" * 60)
        return "\n".join(lines)
    lines.append(f"total rows: {s.total_rows}")
    lines.append(f"verdicts:   " + " / ".join(
        f"{k}={v}" for k, v in sorted(s.verdict_counts.items())
    ))
    lines.append(f"modes:      " + " / ".join(
        f"{k}={v}" for k, v in sorted(s.mode_counts.items())
    ))
    lines.append(f"engines:    " + " / ".join(
        f"{k}={v}" for k, v in sorted(s.engine_counts.items())
    ))
    lines.append(f"latency:    median={s.median_elapsed_ms:.0f}ms p99={s.p99_elapsed_ms:.0f}ms")
    lines.append(f"cost saved: {s.cost_saved_pct:.2f}% (hard rejects)")
    lines.append(f"translate:  {s.translation_success_pct:.2f}% success")
    if s.fn_rate is not None:
        lines.append(f"fn rate:    {s.fn_rate:.4f} (Q10 wrongly rejected → BRAIN would PASS)")
    else:
        lines.append("fn rate:    insufficient followup data (<10)")
    lines.append("=" * 60)
    if s.alert_level == "ALERT":
        lines.append(f"ACTION: fn_rate {s.fn_rate:.4f} > 0.15 — demote QLIB_PRESCREEN_MODE to soft/shadow")
    elif s.alert_level == "INFO":
        if s.total_rows == 0:
            lines.append("ACTION: no signal — verify ENABLE_QLIB_PRESCREEN is on + task running")
        else:
            lines.append("ACTION: cost saved <10% — consider lower floor or wider coverage")
    return "\n".join(lines)


def post_to_slack(webhook_url: str, text: str) -> bool:
    """POST to Slack incoming-webhook. Returns True on 2xx."""
    try:
        import urllib.request
        payload = json.dumps({"text": f"```{text}```"}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as ex:
        logger.warning(f"Slack post failed: {ex}")
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Q10 telemetry daily report")
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--slack-webhook", default=os.getenv("Q10_SLACK_WEBHOOK"))
    parser.add_argument("--rows-json", default=None,
                        help="Read pre-fetched rows from JSON (test path)")
    parser.add_argument("--exit-nonzero-on-alert", action="store_true",
                        help="rc=2 when alert_level=ALERT (for paging)")
    args = parser.parse_args(argv)

    if args.rows_json:
        rows = load_rows_from_json(args.rows_json)
    else:
        rows = asyncio.run(fetch_rows_from_db(window_hours=args.window_hours))

    summary = aggregate(rows, window_hours=args.window_hours)
    report = format_report(summary)
    print(report)

    if args.slack_webhook:
        ok = post_to_slack(args.slack_webhook, report)
        if not ok:
            logger.warning("Slack post failed (report still printed to stdout)")

    if args.exit_nonzero_on_alert and summary.alert_level == "ALERT":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
