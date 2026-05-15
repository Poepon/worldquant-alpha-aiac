"""P2-A macro_narrative_extract Celery task integration tests (PG-only, 2026-05-16).

Mirrors test_negative_knowledge_extract layout.

4 cases:
  - E1 ENABLE_EXTRACT=False → only seed UPSERT runs, no LLM call
  - E2 ENABLE_EXTRACT=True → batches over missing fields, LLM mock returns
       narratives, upsert_llm_narratives is invoked
  - E3 token budget guard: MAX_TOKENS_PER_DAY tiny → batches_skipped_budget
  - E4 schema/contract of docs/macro_narratives/<sh-date>.json
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("POSTGRES_PORT", "5433")


def _pg_reachable() -> bool:
    try:
        s = socket.create_connection(("localhost", 5433), timeout=1)
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason="P2-A macro-narrative extract tests require Postgres on localhost:5433",
)


# Warm-up
import backend.tasks  # noqa: E402, F401

from backend.models import (  # noqa: E402
    DataField,
    DatasetMetadata,
    KnowledgeEntry,
)


_TAG = f"mnX{uuid.uuid4().hex[:3]}_"


@pytest_asyncio.fixture
async def pg_session():
    from backend.config import settings

    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as s:
            yield s
            try:
                await s.execute(
                    text(
                        "DELETE FROM knowledge_entries "
                        "WHERE entry_type='MACRO_NARRATIVE' "
                        "AND (created_by = 'P2A_MACRO' "
                        "     OR meta_data->>'field_id' ILIKE :p)"
                    ),
                    {"p": f"%{_TAG}%"},
                )
                await s.execute(
                    delete(DataField).where(
                        DataField.field_id.like(f"{_TAG}%"),
                    )
                )
                await s.execute(
                    delete(DatasetMetadata).where(
                        DatasetMetadata.dataset_id.like(f"{_TAG}%"),
                    )
                )
                await s.commit()
            except Exception:
                await s.rollback()
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _seed_missing_field(pg_session, *, field_id: str,
                              dataset_brain_id: str = None,
                              region: str = "USA"):
    """Insert a tagged Dataset + DataField pair so list_fields_missing_narrative
    will surface this field on the next call."""
    ds_brain = dataset_brain_id or f"{_TAG}pv_x"
    ds = DatasetMetadata(
        dataset_id=ds_brain,
        region=region,
        universe="TOP3000",
        name=f"{_TAG}ds",
        description="x",
        category="Price/Volume",
    )
    pg_session.add(ds)
    await pg_session.commit()
    await pg_session.refresh(ds)
    df = DataField(
        dataset_id=ds.id,
        region=region,
        universe="TOP3000",
        field_id=field_id,
        field_name=field_id,
        field_type="MATRIX",
        description="seeded for extract test",
        is_active=True,
    )
    pg_session.add(df)
    await pg_session.commit()
    return ds, df


class _FakeLLMResponse:
    def __init__(self, parsed):
        self.success = True
        self.parsed = parsed
        self.error = None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestMacroNarrativeExtractTask:

    @pytest.mark.asyncio
    async def test_extract_disabled_runs_seed_only(self, pg_session, tmp_path):
        """E1: ENABLE_EXTRACT=False → Phase 2 skipped; seed_counters
        populated; LLM service NOT instantiated."""
        from backend.config import settings
        from backend.tasks import macro_narrative_extract as _mod

        original = settings.ENABLE_MACRO_NARRATIVE_EXTRACT
        settings.ENABLE_MACRO_NARRATIVE_EXTRACT = False
        try:
            with patch.object(_mod, "_OUTPUT_DIR", tmp_path):
                # Spy on LLMService to assert it's not constructed
                with patch(
                    "backend.agents.services.llm_service.LLMService"
                ) as llm_cls:
                    result = await _mod._run_async()
                    assert llm_cls.call_count == 0, (
                        "LLMService instantiated when EXTRACT flag was OFF"
                    )
        finally:
            settings.ENABLE_MACRO_NARRATIVE_EXTRACT = original

        assert "error" not in result, f"task raised: {result}"
        assert result["extract_enabled"] is False
        seed_counters = result.get("seed_counters", {})
        # Seed UPSERT ran — either new (first time) or updated (already there)
        assert (seed_counters.get("new", 0)
                + seed_counters.get("updated", 0)) >= 11
        # No LLM activity
        llm_counters = result.get("llm_counters", {})
        assert llm_counters.get("batches_run", 0) == 0
        assert llm_counters.get("new", 0) == 0

    @pytest.mark.asyncio
    async def test_extract_enabled_runs_batches(self, pg_session, tmp_path):
        """E2: ENABLE_EXTRACT=True + 3 seeded missing fields → LLM called
        once (one batch <20), returns annotations, upsert_llm_narratives
        creates LLM-source rows."""
        from backend.config import settings
        from backend.tasks import macro_narrative_extract as _mod

        f1 = f"{_TAG}f1"
        f2 = f"{_TAG}f2"
        f3 = f"{_TAG}f3"
        # Use separate datasets per field — uq_dataset_region_universe
        # blocks reusing the same dataset_id across multiple _seed_missing_field
        # calls.
        await _seed_missing_field(pg_session, field_id=f1,
                                  dataset_brain_id=f"{_TAG}pv_e2_1")
        await _seed_missing_field(pg_session, field_id=f2,
                                  dataset_brain_id=f"{_TAG}pv_e2_2")
        await _seed_missing_field(pg_session, field_id=f3,
                                  dataset_brain_id=f"{_TAG}pv_e2_3")

        # Fake LLM that returns one item per field
        async def _fake_call(system_prompt, user_prompt, temperature=0.4,
                             json_mode=True):
            return _FakeLLMResponse({
                "items": [
                    {"field_id": f1, "mechanism": f"{_TAG}m1",
                     "transmission_channel": f"{_TAG}t1",
                     "expected_signal_hint": "momentum", "confidence": 0.8},
                    {"field_id": f2, "mechanism": f"{_TAG}m2",
                     "transmission_channel": f"{_TAG}t2",
                     "expected_signal_hint": "value", "confidence": 0.7},
                    {"field_id": f3, "mechanism": f"{_TAG}m3",
                     "transmission_channel": f"{_TAG}t3",
                     "expected_signal_hint": "sentiment", "confidence": 0.6},
                ]
            })

        fake_llm = MagicMock()
        fake_llm.call = AsyncMock(side_effect=_fake_call)

        original = settings.ENABLE_MACRO_NARRATIVE_EXTRACT
        original_max = settings.MACRO_NARRATIVE_LLM_MAX_PER_DAY
        original_bsize = settings.MACRO_NARRATIVE_LLM_BATCH_SIZE
        settings.ENABLE_MACRO_NARRATIVE_EXTRACT = True
        # To avoid the test thrashing through every production field, also
        # filter list_fields_missing_narrative to OUR tagged fields only.
        try:
            # Monkey-patch list_fields_missing_narrative to return ONLY our
            # tagged seed fields — keeps the test deterministic regardless
            # of how many other production fields lack narratives. Use a
            # high inner-limit so even alphabetically-late tagged fields
            # come through.
            import backend.services.macro_narrative_service as _svc_mod
            real_list = _svc_mod.MacroNarrativeService.list_fields_missing_narrative

            async def _filtered_list(self, *, region=None, limit=500):
                rows = await real_list(self, region=region, limit=50000)
                return [r for r in rows
                        if (r.get("field_id") or "").startswith(_TAG)]

            with patch.object(_mod, "_OUTPUT_DIR", tmp_path), \
                 patch.object(
                     _svc_mod.MacroNarrativeService,
                     "list_fields_missing_narrative",
                     _filtered_list,
                 ), \
                 patch(
                     "backend.agents.services.llm_service.LLMService",
                     return_value=fake_llm,
                 ):
                result = await _mod._run_async()
        finally:
            settings.ENABLE_MACRO_NARRATIVE_EXTRACT = original
            settings.MACRO_NARRATIVE_LLM_MAX_PER_DAY = original_max
            settings.MACRO_NARRATIVE_LLM_BATCH_SIZE = original_bsize

        assert "error" not in result, f"task raised: {result}"
        assert result["extract_enabled"] is True
        llm_counters = result["llm_counters"]
        assert llm_counters["batches_run"] >= 1, llm_counters
        # 3 LLM items → 3 new MACRO_NARRATIVE rows (field-scope, source=llm)
        assert llm_counters["new"] >= 3, llm_counters

        # Verify rows in KB
        rows = (await pg_session.execute(
            text(
                "SELECT COUNT(*) FROM knowledge_entries "
                "WHERE entry_type='MACRO_NARRATIVE' "
                "AND meta_data->>'source' = 'llm' "
                "AND meta_data->>'field_id' ILIKE :p"
            ),
            {"p": f"{_TAG}%"},
        )).scalar() or 0
        assert rows == 3, f"expected 3 LLM rows, found {rows}"

    @pytest.mark.asyncio
    async def test_extract_token_budget_guard(self, pg_session, tmp_path,
                                              monkeypatch):
        """E3: tiny MAX_TOKENS_PER_DAY → batches_skipped_budget > 0."""
        from backend.config import settings
        from backend.tasks import macro_narrative_extract as _mod

        # Seed 5 missing fields so we'd run 1 batch (batch_size default 20)
        for i in range(5):
            await _seed_missing_field(
                pg_session, field_id=f"{_TAG}budget_{i}",
                dataset_brain_id=f"{_TAG}pv_e3_{i}",
            )

        # Fake redis returns 0 used initially but budget is tiny
        class _FakeRedis:
            def __init__(self): self.store = {}
            def get(self, k): return self.store.get(k)
            def incrby(self, k, n): self.store[k] = int(self.store.get(k, 0) or 0) + n
            def expire(self, k, ttl): pass
        fake_redis = _FakeRedis()

        # LLM should NEVER be reached because budget triggers
        fake_llm = MagicMock()
        fake_llm.call = AsyncMock(side_effect=AssertionError(
            "LLM should not be called when budget exceeded"
        ))

        original_flag = settings.ENABLE_MACRO_NARRATIVE_EXTRACT
        original_budget = settings.MAX_TOKENS_PER_DAY
        settings.ENABLE_MACRO_NARRATIVE_EXTRACT = True
        settings.MAX_TOKENS_PER_DAY = 10  # smaller than 300 tokens/field est
        try:
            with patch.object(_mod, "_OUTPUT_DIR", tmp_path), \
                 patch(
                     "backend.tasks.redis_pool.get_redis_client",
                     return_value=fake_redis,
                 ), \
                 patch(
                     "backend.agents.services.llm_service.LLMService",
                     return_value=fake_llm,
                 ):
                result = await _mod._run_async()
        finally:
            settings.ENABLE_MACRO_NARRATIVE_EXTRACT = original_flag
            settings.MAX_TOKENS_PER_DAY = original_budget

        assert "error" not in result, f"task raised: {result}"
        llm_counters = result["llm_counters"]
        assert llm_counters["batches_skipped_budget"] >= 1, llm_counters
        assert llm_counters["batches_run"] == 0, llm_counters

    @pytest.mark.asyncio
    async def test_output_json_schema(self, pg_session, tmp_path):
        """E4: JSON file exists at _OUTPUT_DIR/<sh-date>.json with full
        schema."""
        from backend.config import settings
        from backend.tasks import macro_narrative_extract as _mod

        original = settings.ENABLE_MACRO_NARRATIVE_EXTRACT
        settings.ENABLE_MACRO_NARRATIVE_EXTRACT = False
        try:
            with patch.object(_mod, "_OUTPUT_DIR", tmp_path):
                result = await _mod._run_async()
        finally:
            settings.ENABLE_MACRO_NARRATIVE_EXTRACT = original

        assert result.get("json_path"), result
        out_path = Path(result["json_path"])
        assert out_path.exists()
        payload = json.loads(out_path.read_text(encoding="utf-8"))

        for key in (
            "report_date", "generated_at_utc", "schema_version",
            "seed_counters", "extract_enabled", "llm_counters",
            "fields_processed",
        ):
            assert key in payload, f"missing key {key}: {payload}"
        assert payload["schema_version"] == "p2a.v1"
