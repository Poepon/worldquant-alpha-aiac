"""Unit tests for OpsService Phase 4 — LLM op monitor parser.

来源: docs/alphagbm_skills_research_2026-05-15.md ops dashboard plan Phase 4.

Covers the Markdown parser + the get_llm_op_latest fallback walk. The
parser is the part most likely to break across changes to the daily
task's output format, so we pin every counter + the two structured
sections (hallucinated-op histogram + affected entries table) against
a real-shaped fixture sample.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from backend.services.ops_service import OpsService


SAMPLE_MD = """# LLM op hallucination monitor — 2026-05-16

**Active KB entries scanned**: 2144
**Valid BRAIN ops in registry**: 66
**Clean entries**: 2139
**Pattern-level hallucinations**: 0
**Template-only hallucinations**: 5
**Deactivated**: 0

## Hallucinated op names (count of entries)

- `sign_flip` — 2
- `window` — 1
- `group_operation` — 1

## Affected entries (first 30)

| KB# | source | bad_ops | pattern (first 80) |
|---|---|---|---|
| 1191 | template | window | `ts_arg_max(returns, 5)` |
| 5662 | template | sign_flip | `multiply(-1, ts_decay_linear(ts_zscore(high, 5), 4))` |
| 5932 | template | sign_flip,decay_weighted_moving_std | `multiply(-1, ts_decay_linear(x, 4))` |
"""


# ---------------------------------------------------------------------------
# _parse_llm_op_md
# ---------------------------------------------------------------------------

def test_parse_llm_op_md_counters():
    out = OpsService._parse_llm_op_md(SAMPLE_MD)
    assert out["scanned"] == 2144
    assert out["valid_ops_in_registry"] == 66
    assert out["clean"] == 2139
    assert out["pattern_halluc"] == 0
    assert out["template_halluc"] == 5
    assert out["deactivated"] == 0


def test_parse_llm_op_md_histogram():
    out = OpsService._parse_llm_op_md(SAMPLE_MD)
    assert out["hallucinated_ops"] == [
        {"op": "sign_flip", "count": 2},
        {"op": "window", "count": 1},
        {"op": "group_operation", "count": 1},
    ]


def test_parse_llm_op_md_affected_table():
    out = OpsService._parse_llm_op_md(SAMPLE_MD)
    affected = out["affected_entries"]
    assert len(affected) == 3
    assert affected[0]["kb_id"] == 1191
    assert affected[0]["source"] == "template"
    assert affected[0]["bad_ops"] == ["window"]
    assert affected[0]["pattern"] == "ts_arg_max(returns, 5)"
    # Multi-op row splits on comma
    assert affected[2]["bad_ops"] == [
        "sign_flip", "decay_weighted_moving_std",
    ]


def test_parse_llm_op_md_partial_input():
    """Empty / partial md must not raise — zero counters + empty lists."""
    out = OpsService._parse_llm_op_md("# header only\n")
    assert out["scanned"] == 0
    assert out["hallucinated_ops"] == []
    assert out["affected_entries"] == []


def test_parse_llm_op_md_skips_header_rows():
    """The header + divider rows should not slip into affected entries."""
    text = """# LLM op hallucination monitor — 2026-05-16

| KB# | source | bad_ops | pattern |
|---|---|---|---|
| 42 | template | foo | `x` |
"""
    out = OpsService._parse_llm_op_md(text)
    assert len(out["affected_entries"]) == 1
    assert out["affected_entries"][0]["kb_id"] == 42


# ---------------------------------------------------------------------------
# get_llm_op_latest — fallback walk
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_llm_op_latest_reads_today(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    monkeypatch.setattr(
        "backend.services.ops_report_reader._DOCS_ROOT", docs,
    )
    kind_dir = docs / "llm_op_monitor"
    kind_dir.mkdir(parents=True)
    today = date.today()
    (kind_dir / f"{today.isoformat()}.md").write_text(SAMPLE_MD, encoding="utf-8")

    svc = OpsService(db=None)  # type: ignore[arg-type]
    out = await svc.get_llm_op_latest()
    assert out["source"] == "docs_today"
    assert out["summary"]["scanned"] == 2144
    assert out["report_date"] == "2026-05-16"


@pytest.mark.asyncio
async def test_get_llm_op_latest_archive_fallback(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    monkeypatch.setattr(
        "backend.services.ops_report_reader._DOCS_ROOT", docs,
    )
    kind_dir = docs / "llm_op_monitor"
    kind_dir.mkdir(parents=True)
    older = date.today() - timedelta(days=3)
    (kind_dir / f"{older.isoformat()}.md").write_text(SAMPLE_MD, encoding="utf-8")

    svc = OpsService(db=None)  # type: ignore[arg-type]
    out = await svc.get_llm_op_latest()
    assert out["source"] == "docs_archived"
    assert out["stale_days"] == 3
    assert out["summary"]["scanned"] == 2144


@pytest.mark.asyncio
async def test_get_llm_op_latest_missing(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    monkeypatch.setattr(
        "backend.services.ops_report_reader._DOCS_ROOT", docs,
    )
    svc = OpsService(db=None)  # type: ignore[arg-type]
    out = await svc.get_llm_op_latest()
    assert out["source"] == "missing"
    assert out["summary"]["scanned"] == 0
    assert out["summary"]["affected_entries"] == []
    assert out["report_date"] is None
