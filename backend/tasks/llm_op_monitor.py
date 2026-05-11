"""LLM operator hallucination monitor (V-22.3 long-term enforcement).

V-22.3 commit (dacd5be) added op-whitelist validation at KB write time
in feedback_agent.learn_from_round. This monitor catches:
  - Historical entries written before V-22.3
  - Other write paths that bypass the canonicalize chain
  - Drift after BRAIN Operator registry updates (sync_datasets 06:00 daily)
  - meta_data.template field (LLM template, not the canonical pattern)

Beat schedule: daily 06:30 UTC (after sync_datasets at 06:00). Scans all
active SUCCESS_PATTERN + FAILURE_PITFALL entries, extracts op chains via
`extract_operator_chain`, compares against active Operator table, and:
  1. Soft-deactivates (is_active=False) entries with hallucinated ops
  2. Writes daily report to `docs/llm_op_hallucination_<date>.md`
  3. Returns summary dict for monitoring dashboards

Excludes FIELD/NUM/field/num placeholders from extract_operator_chain
output — these are skeleton placeholders, not ops.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Set

from loguru import logger
from sqlalchemy import select, update

from backend.celery_app import celery_app
from backend.tasks import run_async


# Skeleton placeholders that extract_operator_chain regex picks up but
# are NOT operators — exclude from whitelist comparison.
_SKELETON_PLACEHOLDERS: Set[str] = {"field", "num"}


@celery_app.task(name="backend.tasks.monitor_llm_op_hallucinations")
def monitor_llm_op_hallucinations(*, deactivate: bool = True) -> dict:
    """Beat-triggered wrapper. Returns summary stats."""
    return run_async(_monitor_async(deactivate=deactivate))


async def _monitor_async(*, deactivate: bool) -> dict:
    from backend.database import AsyncSessionLocal
    from backend.knowledge_extraction import extract_operator_chain
    from backend.models import KnowledgeEntry
    from backend.models.metadata import Operator

    stats = {
        "scanned": 0,
        "clean": 0,
        "hallucinated_pattern": 0,
        "hallucinated_template": 0,
        "deactivated": 0,
        "valid_op_count": 0,
        "bad_ops_seen": {},  # op_name → count of entries
    }

    async with AsyncSessionLocal() as db:
        # 1. Load active op whitelist (refreshed each run — picks up new BRAIN ops)
        valid_ops = set(
            r[0]
            for r in (
                await db.execute(
                    select(Operator.name).where(Operator.is_active == True)  # noqa: E712
                )
            ).all()
        )
        stats["valid_op_count"] = len(valid_ops)

        def _extract_real_ops(text: str) -> list[str]:
            """Extract ops from skeleton/expression text, excluding placeholders."""
            return [
                o
                for o in (extract_operator_chain(text or "") or [])
                if o.lower() not in _SKELETON_PLACEHOLDERS
            ]

        # 2. Scan active KB entries
        stmt = (
            select(KnowledgeEntry)
            .where(KnowledgeEntry.is_active == True)  # noqa: E712
            .where(KnowledgeEntry.entry_type.in_(["SUCCESS_PATTERN", "FAILURE_PITFALL"]))
        )
        rows = (await db.execute(stmt)).scalars().all()
        stats["scanned"] = len(rows)

        bad_entries = []  # (entry, source_field, bad_ops)

        for entry in rows:
            # 2a. Check pattern field
            pattern_ops = _extract_real_ops(entry.pattern)
            pattern_bad = [o for o in pattern_ops if o not in valid_ops]

            # 2b. Check meta_data.template (LLM-generated, may slip past V-22.3)
            md = entry.meta_data or {}
            template_text = md.get("template") or ""
            template_ops = _extract_real_ops(template_text)
            template_bad = [o for o in template_ops if o not in valid_ops]

            if pattern_bad:
                bad_entries.append((entry, "pattern", pattern_bad))
                stats["hallucinated_pattern"] += 1
                for op in pattern_bad:
                    stats["bad_ops_seen"][op] = stats["bad_ops_seen"].get(op, 0) + 1
            elif template_bad:
                # template-only issue: don't deactivate, just log
                bad_entries.append((entry, "template", template_bad))
                stats["hallucinated_template"] += 1
                for op in template_bad:
                    stats["bad_ops_seen"][op] = stats["bad_ops_seen"].get(op, 0) + 1
            else:
                stats["clean"] += 1

        # 3. Soft-deactivate pattern-level hallucinations
        if deactivate and bad_entries:
            today = datetime.now(timezone.utc).date().isoformat()
            for entry, source_field, bad_ops in bad_entries:
                if source_field != "pattern":
                    continue  # only deactivate pattern-level
                try:
                    new_md = {
                        **(entry.meta_data or {}),
                        "llm_op_monitor_deactivated_at": today,
                        "llm_op_monitor_reason": "hallucinated op in pattern",
                        "llm_op_monitor_bad_ops": bad_ops,
                    }
                    await db.execute(
                        update(KnowledgeEntry)
                        .where(KnowledgeEntry.id == entry.id)
                        .values(is_active=False, meta_data=new_md)
                    )
                    stats["deactivated"] += 1
                except Exception as e:
                    logger.warning(
                        f"[llm_op_monitor] deactivate KB#{entry.id} failed: {e}"
                    )
            if stats["deactivated"]:
                await db.commit()

        # 4. Write daily report
        try:
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            report_dir = Path("docs/llm_op_monitor")
            report_dir.mkdir(parents=True, exist_ok=True)
            report_path = report_dir / f"{today_str}.md"
            lines = [
                f"# LLM op hallucination monitor — {today_str}",
                "",
                f"**Active KB entries scanned**: {stats['scanned']}",
                f"**Valid BRAIN ops in registry**: {stats['valid_op_count']}",
                f"**Clean entries**: {stats['clean']}",
                f"**Pattern-level hallucinations**: {stats['hallucinated_pattern']}",
                f"**Template-only hallucinations**: {stats['hallucinated_template']}",
                f"**Deactivated**: {stats['deactivated']}",
                "",
            ]
            if stats["bad_ops_seen"]:
                lines.append("## Hallucinated op names (count of entries)")
                lines.append("")
                for op, n in sorted(
                    stats["bad_ops_seen"].items(), key=lambda kv: -kv[1]
                ):
                    lines.append(f"- `{op}` — {n}")
                lines.append("")
            if bad_entries:
                lines.append("## Affected entries (first 30)")
                lines.append("")
                lines.append("| KB# | source | bad_ops | pattern (first 80) |")
                lines.append("|---|---|---|---|")
                for entry, source_field, bad_ops in bad_entries[:30]:
                    pat = (entry.pattern or "")[:80].replace("|", "\\|")
                    bad_str = ",".join(bad_ops)[:50]
                    lines.append(
                        f"| {entry.id} | {source_field} | {bad_str} | `{pat}` |"
                    )
                lines.append("")
            else:
                lines.append("No hallucinations detected. ✅\n")
            report_path.write_text("\n".join(lines), encoding="utf-8")
            stats["report_path"] = str(report_path)
        except Exception as e:
            logger.warning(f"[llm_op_monitor] report write failed: {e}")

    logger.info(
        f"[llm_op_monitor] done | scanned={stats['scanned']} "
        f"clean={stats['clean']} hallucinated_pattern={stats['hallucinated_pattern']} "
        f"hallucinated_template={stats['hallucinated_template']} "
        f"deactivated={stats['deactivated']}"
    )
    return stats
