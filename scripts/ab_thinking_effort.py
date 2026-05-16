"""A/B Validation — Per-Node Thinking Effort

Compare per-node LLM metrics (tokens / latency / success rate) across two
time windows. Use this to validate whether bumping `hypothesis` / `code_gen`
from `xhigh` to `max` (or any other tier swap) actually moves alpha quality
enough to justify the cost.

Workflow:
    1. Set `.env`: THINKING_EFFORT_OVERRIDES='{"hypothesis":"xhigh","code_gen":"xhigh"}'
       Restart backend. Launch a mining task (e.g. via the UI or phase2_smoke_launch).
       Record `T_start_A`. Wait ~30 rounds. Record `T_end_A`.

    2. Set `.env`: THINKING_EFFORT_OVERRIDES='{"hypothesis":"max","code_gen":"max"}'
       Restart backend. Launch the same kind of task. Record `T_start_B` / `T_end_B`.

    3. Run this script:
           python scripts/ab_thinking_effort.py \\
               --a-start "2026-05-16 02:00" --a-end "2026-05-16 03:30" \\
               --b-start "2026-05-16 04:00" --b-end "2026-05-16 05:30" \\
               [--a-label xhigh] [--b-label max] \\
               [--log .cursor/debug.log] [--out docs/ab_thinking_effort_YYYY-MM-DD.md]

What you get:
    Markdown report at `--out` with per-node call_count / tokens_avg /
    latency_avg / success_rate, plus delta % between A and B. This is the
    "cost half" of the A/B; pair it with manual PASS / Sharpe comparison
    from the Alphas table (TODO: wire in once we have a session_id tag on
    each LLM call so we can JOIN alpha rows back to the call window).

Implementation note: parses [LLMService] Call success/failed log lines
produced by backend/agents/services/llm_service.py:call(). The format is
fixed: ` node=<key> effort=<tier> tokens=<int> latency=<int>ms`.
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# loguru default: "YYYY-MM-DD HH:MM:SS.fff | LEVEL | module:func:line - message"
# Match both success and failed; capture timestamp + node + effort + tokens + latency.
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})[.,]?\d*"
    r".*?\[LLMService\] Call (?P<outcome>success|failed) \| "
    r"id=\d+ "
    r"node=(?P<node>\S+) effort=(?P<effort>\S+) "
    r"(?:tokens=(?P<tokens>\d+) )?"
    r"(?:latency=(?P<latency>\d+)ms)?"
)


@dataclass
class _NodeAgg:
    node_key: str
    effort: str
    calls: int = 0
    tokens_total: int = 0
    latency_ms_total: int = 0
    success: int = 0
    failure: int = 0

    @property
    def tokens_avg(self) -> float:
        return self.tokens_total / self.calls if self.calls else 0.0

    @property
    def latency_avg(self) -> float:
        return self.latency_ms_total / self.calls if self.calls else 0.0

    @property
    def success_rate(self) -> float:
        return self.success / self.calls if self.calls else 0.0


def _parse_window(
    log_path: Path, t_start: datetime, t_end: datetime
) -> Dict[Tuple[str, str], _NodeAgg]:
    """Walk the log, accumulate per-(node, effort) within [t_start, t_end]."""
    agg: Dict[Tuple[str, str], _NodeAgg] = {}
    if not log_path.exists():
        raise FileNotFoundError(f"log file not found: {log_path}")
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _LINE_RE.search(line)
            if not m:
                continue
            try:
                ts = datetime.strptime(m.group("ts").replace("T", " "), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if not (t_start <= ts <= t_end):
                continue
            node = m.group("node")
            effort = m.group("effort")
            key = (node, effort)
            if key not in agg:
                agg[key] = _NodeAgg(node_key=node, effort=effort)
            entry = agg[key]
            entry.calls += 1
            if m.group("outcome") == "success":
                entry.success += 1
                entry.tokens_total += int(m.group("tokens") or 0)
            else:
                entry.failure += 1
            entry.latency_ms_total += int(m.group("latency") or 0)
    return agg


def _delta_pct(a: float, b: float) -> str:
    """Format b vs a as '+12.3%' / '-5.1%' / 'n/a'."""
    if a == 0:
        return "n/a" if b == 0 else "+∞"
    pct = (b - a) / a * 100.0
    return f"{pct:+.1f}%"


def _format_table(
    a_agg: Dict[Tuple[str, str], _NodeAgg],
    b_agg: Dict[Tuple[str, str], _NodeAgg],
    a_label: str,
    b_label: str,
) -> str:
    """Build a markdown table comparing two phases per node."""
    # Group by node_key (sum across effort variants — usually a node has 1 effort per phase).
    def collapse(agg: Dict[Tuple[str, str], _NodeAgg]) -> Dict[str, _NodeAgg]:
        out: Dict[str, _NodeAgg] = {}
        for (node, _), v in agg.items():
            if node not in out:
                out[node] = _NodeAgg(node_key=node, effort="")
            o = out[node]
            o.calls += v.calls
            o.tokens_total += v.tokens_total
            o.latency_ms_total += v.latency_ms_total
            o.success += v.success
            o.failure += v.failure
            # Preserve the effort tier seen (semicolon-joined if mixed)
            if v.effort not in o.effort.split(";"):
                o.effort = f"{o.effort};{v.effort}" if o.effort else v.effort
        return out

    a = collapse(a_agg)
    b = collapse(b_agg)
    nodes = sorted(set(a.keys()) | set(b.keys()))

    lines: List[str] = []
    lines.append(
        f"| Node | A effort | B effort | A calls | B calls | "
        f"A tokens_avg | B tokens_avg | Δ tokens | "
        f"A latency_avg | B latency_avg | Δ latency | "
        f"A succ% | B succ% |"
    )
    lines.append("|" + "---|" * 13)
    total_a_tokens = total_b_tokens = 0
    total_a_calls = total_b_calls = 0
    for node in nodes:
        ax = a.get(node)
        bx = b.get(node)
        a_eff = ax.effort if ax else "-"
        b_eff = bx.effort if bx else "-"
        a_calls = ax.calls if ax else 0
        b_calls = bx.calls if bx else 0
        a_tk = ax.tokens_avg if ax else 0.0
        b_tk = bx.tokens_avg if bx else 0.0
        a_lat = ax.latency_avg if ax else 0.0
        b_lat = bx.latency_avg if bx else 0.0
        a_succ = ax.success_rate if ax else 0.0
        b_succ = bx.success_rate if bx else 0.0
        total_a_tokens += ax.tokens_total if ax else 0
        total_b_tokens += bx.tokens_total if bx else 0
        total_a_calls += a_calls
        total_b_calls += b_calls
        lines.append(
            f"| `{node}` | {a_eff} | {b_eff} | "
            f"{a_calls} | {b_calls} | "
            f"{a_tk:.0f} | {b_tk:.0f} | {_delta_pct(a_tk, b_tk)} | "
            f"{a_lat:.0f}ms | {b_lat:.0f}ms | {_delta_pct(a_lat, b_lat)} | "
            f"{a_succ:.1%} | {b_succ:.1%} |"
        )
    lines.append(
        f"| **TOTAL** | — | — | "
        f"**{total_a_calls}** | **{total_b_calls}** | "
        f"— | — | — | — | — | — | — | — |"
    )
    lines.append("")
    lines.append(
        f"**Total tokens consumed**: A({a_label}) = {total_a_tokens:,}, "
        f"B({b_label}) = {total_b_tokens:,} "
        f"(Δ {_delta_pct(total_a_tokens, total_b_tokens)})"
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--a-start", required=True, help="Phase A start (YYYY-MM-DD HH:MM[:SS])")
    parser.add_argument("--a-end", required=True, help="Phase A end")
    parser.add_argument("--b-start", required=True, help="Phase B start")
    parser.add_argument("--b-end", required=True, help="Phase B end")
    parser.add_argument("--a-label", default="A", help="Label for phase A (e.g. 'xhigh')")
    parser.add_argument("--b-label", default="B", help="Label for phase B (e.g. 'max')")
    parser.add_argument("--log", default=".cursor/debug.log", help="Path to backend debug log")
    parser.add_argument("--out", default=None, help="Output markdown path (default: docs/ab_thinking_effort_<date>.md)")
    args = parser.parse_args()

    def _parse_dt(s: str) -> datetime:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        raise SystemExit(f"unrecognized datetime: {s!r}")

    a_start, a_end = _parse_dt(args.a_start), _parse_dt(args.a_end)
    b_start, b_end = _parse_dt(args.b_start), _parse_dt(args.b_end)

    log_path = Path(args.log)
    a_agg = _parse_window(log_path, a_start, a_end)
    b_agg = _parse_window(log_path, b_start, b_end)

    out_path = Path(args.out) if args.out else Path(f"docs/ab_thinking_effort_{datetime.now():%Y-%m-%d}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    header = [
        f"# A/B Thinking Effort — {args.a_label} vs {args.b_label}",
        "",
        f"- Phase A ({args.a_label}): `{a_start}` → `{a_end}`",
        f"- Phase B ({args.b_label}): `{b_start}` → `{b_end}`",
        f"- Source: `{log_path}`",
        f"- Generated: `{datetime.now():%Y-%m-%d %H:%M:%S}`",
        "",
        "## Per-Node Metrics",
        "",
    ]
    table = _format_table(a_agg, b_agg, args.a_label, args.b_label)
    footer = [
        "",
        "## How to Read",
        "",
        "- `Δ tokens` / `Δ latency`: change from A → B. Positive = B uses more.",
        "- `succ%`: LLMService.call success rate (does NOT reflect alpha PASS rate).",
        "- **TODO**: integrate alpha PASS / Sharpe by JOINing the Alphas table on the",
        "  time window. Until then, run that comparison manually via the Alphas",
        "  router (`/api/v1/alphas?created_after=...&created_before=...`) and",
        "  paste the summary below.",
        "",
        "## Manual Alpha Quality Comparison (fill in)",
        "",
        f"- {args.a_label} PASS rate: ___% (n=___)",
        f"- {args.b_label} PASS rate: ___% (n=___)",
        f"- {args.a_label} avg Sharpe: ___",
        f"- {args.b_label} avg Sharpe: ___",
        f"- {args.a_label} diversity score: ___",
        f"- {args.b_label} diversity score: ___",
        "",
        "## Decision Gate",
        "",
        f"- Upgrade `hypothesis` / `code_gen` to `{args.b_label}` iff:",
        f"  - PASS rate improves ≥ 10%, AND",
        f"  - total tokens delta < 2× (current Δ = see TOTAL row above)",
        "",
    ]
    body = "\n".join(header + [table] + footer)
    out_path.write_text(body, encoding="utf-8")
    print(f"Report written: {out_path}")
    print(f"Phase A: {sum(v.calls for v in a_agg.values())} calls across {len(a_agg)} (node, effort) groups")
    print(f"Phase B: {sum(v.calls for v in b_agg.values())} calls across {len(b_agg)} (node, effort) groups")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
