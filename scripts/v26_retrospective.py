"""V-26 retrospective audit — pre/post mining-restart comparison.

Run this once before restarting mining (with `--save-baseline`) and again
after a week of mining (with `--compare`). The script writes a JSON snapshot
to docs/v26_retrospective/ keyed on the run timestamp, then prints a diff
table against the most recent baseline.

What it measures:

  Cost / waste
    - BRAIN call total (alphas + alpha_failures)
    - failure_rate %, by error_type
    - cost-per-PASS (BRAIN calls / new PASS)
    - cost-per-can_submit
    - IQC audit_failures distribution (V-26.86)

  V-26 trigger rate (proof the fixes are actually firing)
    - alpha_failures with hypothesis_id != NULL (V-26.13/26/V-25.B)
    - alphas with metrics._brain_pass_downgrade (V-26.21 expanded set)
    - alphas with metrics._v16_suspicion_flags on PROV path (V-26.20)
    - alphas with metrics._brain_actionable_fails (V-26.20)
    - per-alpha metrics_snapshot_at spread (V-26.91)

  Hypothesis lifecycle
    - status distribution, focusing on ABANDONED (was 0 pre-V-26)
    - alpha_count distribution (V-26.13: should now include failures)

  KB health
    - SUCCESS_PATTERN / FAILURE_PITFALL active counts
    - patterns with running-avg fields > avg_sharpe only (V-26.9)
    - hypothesis_ids meta_data coverage (V-26.12 family signal)

Usage:
    venv/Scripts/python.exe -m scripts.v26_retrospective --save-baseline
    # (run mining for 5-7 days)
    venv/Scripts/python.exe -m scripts.v26_retrospective --compare
    venv/Scripts/python.exe -m scripts.v26_retrospective --window-hours 168 --compare
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

from sqlalchemy import text

from backend.database import AsyncSessionLocal


_OUT_DIR = Path("docs/v26_retrospective")


async def collect(window_hours: int) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    since = now - timedelta(hours=window_hours)

    snapshot: Dict[str, Any] = {
        "captured_at": now.isoformat(),
        "window_hours": window_hours,
        "since": since.isoformat(),
    }

    async with AsyncSessionLocal() as db:
        # === cost / waste ===
        cost: Dict[str, Any] = {}
        cost["alphas_total"] = (
            await db.execute(
                text("SELECT COUNT(*) FROM alphas WHERE created_at >= :s"),
                {"s": since},
            )
        ).scalar() or 0
        cost["alphas_pass"] = (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM alphas WHERE created_at >= :s AND quality_status='PASS'"
                ),
                {"s": since},
            )
        ).scalar() or 0
        cost["alphas_prov"] = (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM alphas WHERE created_at >= :s AND quality_status='PASS_PROVISIONAL'"
                ),
                {"s": since},
            )
        ).scalar() or 0
        cost["alphas_can_submit"] = (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM alphas WHERE created_at >= :s AND can_submit=true"
                ),
                {"s": since},
            )
        ).scalar() or 0
        cost["failures_total"] = (
            await db.execute(
                text("SELECT COUNT(*) FROM alpha_failures WHERE created_at >= :s"),
                {"s": since},
            )
        ).scalar() or 0
        cost["brain_calls_est"] = cost["alphas_total"] + cost["failures_total"]
        cost["failure_rate_pct"] = (
            round(100.0 * cost["failures_total"] / cost["brain_calls_est"], 2)
            if cost["brain_calls_est"]
            else 0.0
        )
        cost["cost_per_pass"] = (
            round(cost["brain_calls_est"] / cost["alphas_pass"], 1)
            if cost["alphas_pass"]
            else None
        )
        cost["cost_per_can_submit"] = (
            round(cost["brain_calls_est"] / cost["alphas_can_submit"], 1)
            if cost["alphas_can_submit"]
            else None
        )
        snapshot["cost"] = cost

        # error_type breakdown
        rows = (
            await db.execute(
                text(
                    """SELECT error_type, COUNT(*) cnt
                       FROM alpha_failures WHERE created_at >= :s
                       GROUP BY error_type ORDER BY cnt DESC"""
                ),
                {"s": since},
            )
        ).all()
        snapshot["error_types"] = {r[0]: r[1] for r in rows}

        # === V-26 trigger rate ===
        v26: Dict[str, Any] = {}
        v26["failures_with_hypothesis_id"] = (
            await db.execute(
                text(
                    """SELECT COUNT(*) FROM alpha_failures
                       WHERE created_at >= :s AND hypothesis_id IS NOT NULL"""
                ),
                {"s": since},
            )
        ).scalar() or 0
        v26["failures_with_hypothesis_id_pct"] = (
            round(100.0 * v26["failures_with_hypothesis_id"] / cost["failures_total"], 2)
            if cost["failures_total"]
            else None
        )
        v26["pass_brain_downgrade"] = (
            await db.execute(
                text(
                    """SELECT COUNT(*) FROM alphas
                       WHERE created_at >= :s AND metrics ? '_brain_pass_downgrade'"""
                ),
                {"s": since},
            )
        ).scalar() or 0
        # per-check breakdown of expanded V-26.21 set
        rows = (
            await db.execute(
                text(
                    """SELECT jsonb_array_elements_text(metrics->'_brain_pass_downgrade'), COUNT(*)
                       FROM alphas
                       WHERE created_at >= :s AND metrics ? '_brain_pass_downgrade'
                       GROUP BY 1 ORDER BY 2 DESC"""
                ),
                {"s": since},
            )
        ).all()
        v26["pass_brain_downgrade_by_check"] = {r[0]: r[1] for r in rows}

        v26["prov_with_v16_flags"] = (
            await db.execute(
                text(
                    """SELECT COUNT(*) FROM alphas
                       WHERE created_at >= :s
                         AND quality_status='PASS_PROVISIONAL'
                         AND metrics ? '_v16_suspicion_flags'"""
                ),
                {"s": since},
            )
        ).scalar() or 0
        v26["prov_with_actionable_fails"] = (
            await db.execute(
                text(
                    """SELECT COUNT(*) FROM alphas
                       WHERE created_at >= :s
                         AND quality_status='PASS_PROVISIONAL'
                         AND metrics ? '_brain_actionable_fails'"""
                ),
                {"s": since},
            )
        ).scalar() or 0
        # per-alpha snapshot_at spread (V-26.91)
        row = (
            await db.execute(
                text(
                    """SELECT COUNT(DISTINCT metrics_snapshot_at), COUNT(*)
                       FROM alphas WHERE created_at >= :s"""
                ),
                {"s": since},
            )
        ).first()
        if row and row[1]:
            v26["snapshot_at_spread"] = {
                "distinct_timestamps": row[0],
                "alphas": row[1],
                "ratio": round(row[0] / row[1], 3),
            }
        snapshot["v26_triggers"] = v26

        # === IQC audit ===
        iqc: Dict[str, Any] = {}
        iqc["total_audited"] = (
            await db.execute(
                text("SELECT COUNT(*) FROM alphas WHERE metrics ? '_iqc_marginal'")
            )
        ).scalar() or 0
        rows = (
            await db.execute(
                text(
                    """SELECT COALESCE((metrics->'_iqc_marginal'->>'audit_failures')::int, 0),
                              COUNT(*)
                       FROM alphas WHERE metrics ? '_iqc_marginal'
                       GROUP BY 1 ORDER BY 1"""
                )
            )
        ).all()
        iqc["audit_failures_dist"] = {str(r[0]): r[1] for r in rows}
        rows = (
            await db.execute(
                text(
                    """SELECT
                       CASE
                         WHEN (metrics->'_iqc_marginal'->>'delta_score')::float > 0 THEN 'positive'
                         WHEN (metrics->'_iqc_marginal'->>'delta_score')::float = 0 THEN 'zero'
                         WHEN (metrics->'_iqc_marginal'->>'delta_score')::float < 0 THEN 'negative'
                         ELSE 'null'
                       END,
                       COUNT(*)
                       FROM alphas
                       WHERE metrics ? '_iqc_marginal'
                         AND metrics->'_iqc_marginal' ? 'delta_score'
                       GROUP BY 1"""
                )
            )
        ).all()
        iqc["delta_score_dist"] = {r[0]: r[1] for r in rows}
        snapshot["iqc"] = iqc

        # === Hypothesis lifecycle ===
        hyp: Dict[str, Any] = {}
        rows = (
            await db.execute(
                text("SELECT status, COUNT(*) FROM hypotheses GROUP BY status")
            )
        ).all()
        hyp["status_dist"] = {r[0]: r[1] for r in rows}
        rows = (
            await db.execute(
                text(
                    """SELECT
                       CASE WHEN alpha_count=0 THEN '0'
                            WHEN alpha_count BETWEEN 1 AND 10 THEN '1-10'
                            WHEN alpha_count BETWEEN 11 AND 50 THEN '11-50'
                            ELSE '50+' END,
                       COUNT(*),
                       SUM(CASE WHEN pass_count>0 THEN 1 ELSE 0 END)
                       FROM hypotheses GROUP BY 1 ORDER BY 1"""
                )
            )
        ).all()
        hyp["alpha_count_buckets"] = {
            r[0]: {"hypotheses": r[1], "with_pass": r[2]} for r in rows
        }
        snapshot["hypothesis"] = hyp

        # === KB health ===
        kb: Dict[str, Any] = {}
        kb["success_pattern_active"] = (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM knowledge_entries WHERE entry_type='SUCCESS_PATTERN' AND is_active=true"
                )
            )
        ).scalar() or 0
        kb["failure_pitfall_active"] = (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM knowledge_entries WHERE entry_type='FAILURE_PITFALL' AND is_active=true"
                )
            )
        ).scalar() or 0
        # V-26.9: how many SUCCESS rows have running-averaged fitness/turnover
        # (i.e. avg_fitness key exists in meta_data)
        kb["success_with_avg_fitness"] = (
            await db.execute(
                text(
                    """SELECT COUNT(*) FROM knowledge_entries
                       WHERE entry_type='SUCCESS_PATTERN' AND is_active=true
                         AND meta_data ? 'avg_fitness'"""
                )
            )
        ).scalar() or 0
        # V-26.12: KB rows tagged with hypothesis_ids (family-signal coverage)
        kb["success_with_hypothesis_ids"] = (
            await db.execute(
                text(
                    """SELECT COUNT(*) FROM knowledge_entries
                       WHERE entry_type='SUCCESS_PATTERN' AND is_active=true
                         AND meta_data ? 'hypothesis_ids'
                         AND jsonb_array_length(meta_data->'hypothesis_ids') > 0"""
                )
            )
        ).scalar() or 0
        snapshot["kb"] = kb

    return snapshot


def _print_diff(old: Dict[str, Any], new: Dict[str, Any]) -> None:
    """Print a focused diff table."""
    print(f"\n{'='*72}")
    print(f"V-26 retrospective: {old['captured_at']} → {new['captured_at']}")
    print(
        f"Windows: {old['window_hours']}h → {new['window_hours']}h "
        f"(both relative to capture_at)"
    )
    print(f"{'='*72}\n")

    def line(label: str, old_val, new_val, fmt: str = "{}"):
        try:
            old_s = fmt.format(old_val) if old_val is not None else "—"
        except Exception:
            old_s = str(old_val)
        try:
            new_s = fmt.format(new_val) if new_val is not None else "—"
        except Exception:
            new_s = str(new_val)
        print(f"  {label:42s} {old_s:>14s}  →  {new_s:>14s}")

    print("[cost / waste]")
    co = old["cost"]
    cn = new["cost"]
    line("alphas total", co.get("alphas_total"), cn.get("alphas_total"))
    line("alphas PASS", co.get("alphas_pass"), cn.get("alphas_pass"))
    line("alphas PROV", co.get("alphas_prov"), cn.get("alphas_prov"))
    line("alphas can_submit", co.get("alphas_can_submit"), cn.get("alphas_can_submit"))
    line("alpha_failures", co.get("failures_total"), cn.get("failures_total"))
    line("BRAIN calls (est)", co.get("brain_calls_est"), cn.get("brain_calls_est"))
    line("failure_rate %", co.get("failure_rate_pct"), cn.get("failure_rate_pct"), "{:.2f}")
    line("cost_per_pass", co.get("cost_per_pass"), cn.get("cost_per_pass"))
    line("cost_per_can_submit", co.get("cost_per_can_submit"), cn.get("cost_per_can_submit"))

    print("\n[V-26 trigger rates]")
    vo = old.get("v26_triggers", {})
    vn = new.get("v26_triggers", {})
    line(
        "failures w/ hypothesis_id",
        vo.get("failures_with_hypothesis_id"),
        vn.get("failures_with_hypothesis_id"),
    )
    line(
        "  ^ % of failures",
        vo.get("failures_with_hypothesis_id_pct"),
        vn.get("failures_with_hypothesis_id_pct"),
        "{:.2f}",
    )
    line(
        "PASS→PROV downgrade (V-26.21)",
        vo.get("pass_brain_downgrade"),
        vn.get("pass_brain_downgrade"),
    )
    line(
        "PROV w/ V-16 flags (V-26.20)",
        vo.get("prov_with_v16_flags"),
        vn.get("prov_with_v16_flags"),
    )
    line(
        "PROV w/ actionable_fails",
        vo.get("prov_with_actionable_fails"),
        vn.get("prov_with_actionable_fails"),
    )
    so = vo.get("snapshot_at_spread") or {}
    sn = vn.get("snapshot_at_spread") or {}
    line(
        "snapshot_at unique/total",
        f"{so.get('distinct_timestamps')}/{so.get('alphas')}" if so else None,
        f"{sn.get('distinct_timestamps')}/{sn.get('alphas')}" if sn else None,
    )

    print("\n[hypothesis lifecycle]")
    ho = old.get("hypothesis", {}).get("status_dist", {})
    hn = new.get("hypothesis", {}).get("status_dist", {})
    for status in sorted(set(ho) | set(hn)):
        line(f"  status={status}", ho.get(status, 0), hn.get(status, 0))

    print("\n[IQC marginal]")
    io = old.get("iqc", {})
    inew = new.get("iqc", {})
    line("audited total", io.get("total_audited"), inew.get("total_audited"))
    do = io.get("delta_score_dist", {})
    dn = inew.get("delta_score_dist", {})
    for k in ("positive", "zero", "negative", "null"):
        line(f"  Δscore {k}", do.get(k, 0), dn.get(k, 0))
    ao = io.get("audit_failures_dist", {})
    an = inew.get("audit_failures_dist", {})
    for k in sorted(set(ao) | set(an), key=lambda x: int(x)):
        line(f"  audit_failures={k}", ao.get(k, 0), an.get(k, 0))

    print("\n[KB health]")
    ko = old.get("kb", {})
    kn = new.get("kb", {})
    line(
        "SUCCESS_PATTERN active",
        ko.get("success_pattern_active"),
        kn.get("success_pattern_active"),
    )
    line(
        "FAILURE_PITFALL active",
        ko.get("failure_pitfall_active"),
        kn.get("failure_pitfall_active"),
    )
    line(
        "SUCCESS w/ avg_fitness (V-26.9)",
        ko.get("success_with_avg_fitness"),
        kn.get("success_with_avg_fitness"),
    )
    line(
        "SUCCESS w/ hypothesis_ids (V-26.12)",
        ko.get("success_with_hypothesis_ids"),
        kn.get("success_with_hypothesis_ids"),
    )

    print("\n[error_type breakdown (current window only)]")
    et = new.get("error_types", {})
    total = sum(et.values()) or 1
    for k, v in sorted(et.items(), key=lambda x: -x[1])[:8]:
        print(f"  {k:30s} {v:>8d}  ({100*v/total:.1f}%)")


def _latest_baseline() -> Path | None:
    if not _OUT_DIR.exists():
        return None
    files = sorted(_OUT_DIR.glob("baseline_*.json"))
    return files[-1] if files else None


# =============================================================================
# P2-D (2026-05-15) — `--full` superset extension
# =============================================================================
# RetrospectiveReport (Pydantic v2) is used ONLY by the --full branch.
# Legacy branches (no-flag / --save-baseline / --compare) keep their original
# json.dumps(snap, indent=2, default=str) — byte-for-byte preserved. Do NOT
# refactor those to Pydantic.
# =============================================================================


def _load_latest_health_json(topic_dir: str) -> Dict[str, Any]:
    """Helper: read the most recent ``docs/<topic_dir>/<sh-date>.json`` and
    return as dict. Returns ``{}`` if the directory or any matching file is
    missing or unreadable — never raises (this is best-effort enrichment).
    """
    base = Path("docs") / topic_dir
    if not base.exists() or not base.is_dir():
        return {}
    files = sorted(base.glob("*.json"))
    if not files:
        return {}
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception:
        return {}


def _derive_recommended_actions(
    pillar_summary: Dict[str, Any],
    neg_knowledge: Dict[str, Any],
    hyp_health: Dict[str, Any],
) -> list:
    """Translate health summaries into a small ordered action list. Each
    action is a dict with ``priority`` ('high'/'medium'/'low'), ``action``,
    and ``reason``."""
    actions: list = []

    # Pillar deficit > 0.20 → high-priority pivot suggestion
    try:
        regions = pillar_summary.get("regions") or {}
        for region, payload in regions.items():
            deficits = (payload or {}).get("deficits") or {}
            if not deficits:
                continue
            worst_pillar, worst_def = max(
                deficits.items(), key=lambda kv: kv[1] or 0,
            )
            if (worst_def or 0) >= 0.20:
                actions.append({
                    "priority": "high" if (worst_def or 0) >= 0.30 else "medium",
                    "action": f"Pivot pillar — boost {worst_pillar} in {region}",
                    "reason": (
                        f"pillar deficit {worst_def:.2f} "
                        f"vs target in region {region}"
                    ),
                })
    except Exception:
        pass

    # Top negative-knowledge pattern with fail_count >= 50 → disable / nudge
    try:
        top = (neg_knowledge.get("top_patterns") or [])[:3]
        for t in top:
            fc = int(t.get("fail_count", 0) or 0)
            if fc >= 50:
                actions.append({
                    "priority": "high" if fc >= 100 else "medium",
                    "action": (
                        f"Disable pattern — {t.get('rule_id', '?')} "
                        f"(skeleton {t.get('skeleton', '?')})"
                    ),
                    "reason": (
                        f"fail_count={fc}; consider blacklisting or "
                        f"strengthening static-check"
                    ),
                })
    except Exception:
        pass

    # Hypothesis health triggers → audit-level action
    try:
        triggers = hyp_health.get("triggers_summary") or hyp_health.get(
            "trigger_summary"
        ) or {}
        if triggers:
            firing = {k: v for k, v in triggers.items() if v}
            if firing:
                actions.append({
                    "priority": "medium",
                    "action": "Audit hypothesis triggers",
                    "reason": (
                        f"triggers firing: "
                        f"{', '.join(sorted(firing.keys()))}"
                    ),
                })
    except Exception:
        pass

    return actions


async def _build_trigger_summary(window_hours: int) -> Dict[str, Any]:
    """Scan ``hypotheses.trigger_detail`` JSONB across all rows and bucket
    by trigger type (T1-T5)."""
    out: Dict[str, int] = {}
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                text(
                    "SELECT trigger_detail FROM hypotheses "
                    "WHERE trigger_detail IS NOT NULL"
                )
            )).all()
            for row in rows:
                td = row[0]
                if not isinstance(td, dict):
                    continue
                for trigger_type in td.keys():
                    out[str(trigger_type)] = out.get(str(trigger_type), 0) + 1
    except Exception:
        return {}
    return out


def _build_full_report(snap: Dict[str, Any]) -> Dict[str, Any]:
    """Compose the --full superset payload. Pydantic model is applied at
    serialize time so failure of one section can't break the whole report."""
    return {
        "schema_version": "p2d.v1",
        "captured_at": snap.get("captured_at"),
        "window_hours": snap.get("window_hours"),
        "since": snap.get("since"),
        # 1-6: legacy v26 sections (attached as-is)
        "cost": snap.get("cost") or {},
        "error_types": snap.get("error_types") or {},
        "v26_triggers": snap.get("v26_triggers") or {},
        "iqc": snap.get("iqc") or {},
        "hypothesis": snap.get("hypothesis") or {},
        "kb": snap.get("kb") or {},
        # 7: trigger summary (synthesised from trigger_detail JSONB)
        "trigger_summary": {},  # filled in below
        # 8-11: health summaries (latest JSON snapshots from docs/<topic>/)
        "alpha_health_summary": _load_latest_health_json("alpha_health_check"),
        "hypothesis_health_summary": _load_latest_health_json(
            "hypothesis_health_check",
        ),
        "pillar_balance_summary": _load_latest_health_json("pillar_balance"),
        "negative_knowledge": _load_latest_health_json("negative_knowledge"),
        # 12: derived actions (computed below)
        "recommended_actions": [],
    }


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--window-hours", type=int, default=48,
                   help="Look-back window for cost/error metrics (default 48h)")
    p.add_argument("--save-baseline", action="store_true",
                   help="Capture and save as baseline_<ts>.json (pre-restart use)")
    p.add_argument("--compare", action="store_true",
                   help="Capture now and diff against latest baseline")
    # P2-D: ADDITIVE superset flag. The legacy paths above are completely
    # unchanged — they remain byte-for-byte identical to pre-P2-D output.
    p.add_argument("--full", action="store_true",
                   help="P2-D superset: include health summaries + negative "
                        "knowledge + actions (writes "
                        "docs/v26_retrospective/full_<sh-date>.json)")
    args = p.parse_args()

    if not args.save_baseline and not args.compare and not args.full:
        # Default: print snapshot only
        snap = await collect(args.window_hours)
        print(json.dumps(snap, indent=2, default=str))
        return

    snap = await collect(args.window_hours)
    if args.save_baseline:
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        ts = snap["captured_at"].replace(":", "-").split(".")[0]
        out = _OUT_DIR / f"baseline_{ts}.json"
        out.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
        print(f"[v26_retrospective] baseline saved -> {out}")
        print(f"[v26_retrospective] re-run with --compare after a week of mining")

    if args.compare:
        base = _latest_baseline()
        if base is None:
            print("[v26_retrospective] no baseline found; run --save-baseline first")
            return
        old = json.loads(base.read_text(encoding="utf-8"))
        _print_diff(old, snap)
        # Also dump the post snapshot for archive
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        ts = snap["captured_at"].replace(":", "-").split(".")[0]
        out = _OUT_DIR / f"post_{ts}.json"
        out.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
        print(f"\n[v26_retrospective] post snapshot saved -> {out}")
        print(f"[v26_retrospective] baseline used: {base.name}")

    if args.full:
        # P2-D: ADDITIVE branch — assembles the superset report without
        # touching the legacy save/compare paths above. SH-date filename
        # aligns with alpha_health / pillar_balance / negative_knowledge —
        # deliberate divergence from legacy's UTC-iso ts.
        from pydantic import BaseModel, ConfigDict
        from datetime import timezone as _tz, timedelta as _td

        _SH_TZ = _tz(_td(hours=8))
        sh_now = datetime.now(_tz.utc).astimezone(_SH_TZ)
        sh_date = sh_now.strftime("%Y-%m-%d")

        full_payload = _build_full_report(snap)
        # Trigger summary needs DB access — compute alongside.
        full_payload["trigger_summary"] = await _build_trigger_summary(
            args.window_hours,
        )
        full_payload["recommended_actions"] = _derive_recommended_actions(
            pillar_summary=full_payload.get("pillar_balance_summary") or {},
            neg_knowledge=full_payload.get("negative_knowledge") or {},
            hyp_health=full_payload.get("hypothesis_health_summary") or {},
        )

        class RetrospectiveReport(BaseModel):
            model_config = ConfigDict(extra="allow")
            schema_version: str
            captured_at: str
            window_hours: int
            since: str
            cost: Dict[str, Any] = {}
            error_types: Dict[str, Any] = {}
            v26_triggers: Dict[str, Any] = {}
            iqc: Dict[str, Any] = {}
            hypothesis: Dict[str, Any] = {}
            kb: Dict[str, Any] = {}
            trigger_summary: Dict[str, Any] = {}
            alpha_health_summary: Dict[str, Any] = {}
            hypothesis_health_summary: Dict[str, Any] = {}
            pillar_balance_summary: Dict[str, Any] = {}
            negative_knowledge: Dict[str, Any] = {}
            recommended_actions: list = []

        report = RetrospectiveReport(**full_payload)

        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = _OUT_DIR / f"full_{sh_date}.json"
        out.write_text(
            report.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )
        print(f"[v26_retrospective] --full superset -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
