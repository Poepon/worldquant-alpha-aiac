"""Tests for the self-healing invalid-datafield prune (2026-05-22).

BRAIN rejects stale catalog fields as "Invalid data field <id>"; the dataset
bandit surfaces them by mining dormant datasets (pv96 burned 107 sims/wk).
prune_invalid_datafields deactivates them so _get_dataset_fields (now
is_active-filtered) stops offering them.
"""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from backend.tasks.datafield_prune import _extract_invalid_fields, _prune_async


class TestExtractInvalidFields:
    def test_single(self):
        assert _extract_invalid_fields(
            ["Invalid data field pv96_eq_dvd_cash_cg_amt. <link>Learn more</link>"]
        ) == {"pv96_eq_dvd_cash_cg_amt"}

    def test_multiple_and_dedupe(self):
        out = _extract_invalid_fields([
            "Invalid data field aaa_bbb. x",
            "Invalid data field ccc_ddd.",
            "Invalid data field aaa_bbb.",  # dupe
        ])
        assert out == {"aaa_bbb", "ccc_ddd"}

    def test_no_match(self):
        assert _extract_invalid_fields(["Simulation timed out", "", None]) == set()


class _SharedSessionCM:
    def __init__(self, s):
        self._s = s

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *e):
        return False


@pytest_asyncio.fixture
def session_factory(db_session):
    return lambda: _SharedSessionCM(db_session)


@pytest.mark.asyncio
class TestPrune:
    async def test_deactivates_only_rejected_fields(self, db_session, session_factory):
        from backend.models import AlphaFailure, DataField, DatasetMetadata

        ds = DatasetMetadata(dataset_id="pv96", region="USA", universe="TOP3000", name="pv96")
        db_session.add(ds)
        await db_session.flush()
        bad = DataField(dataset_id=ds.id, region="USA", universe="TOP3000",
                        field_id="pv96_eq_dvd_cash_cg_amt", field_name="x", is_active=True)
        good = DataField(dataset_id=ds.id, region="USA", universe="TOP3000",
                         field_id="pv96_close", field_name="c", is_active=True)
        db_session.add_all([bad, good])
        db_session.add(AlphaFailure(
            task_id=1, expression="ts_mean(pv96_eq_dvd_cash_cg_amt, 5)",
            error_type="SIMULATION_ERROR",
            error_message="Invalid data field pv96_eq_dvd_cash_cg_amt. <link>",
        ))
        await db_session.commit()

        out = await _prune_async(window_days=14, cap=500, session_factory=session_factory)
        assert out["deactivated"] == 1
        assert "pv96_eq_dvd_cash_cg_amt" in out["fields"]

        rows = {r.field_id: r.is_active for r in (await db_session.execute(select(DataField))).scalars().all()}
        assert rows["pv96_eq_dvd_cash_cg_amt"] is False  # rejected → deactivated
        assert rows["pv96_close"] is True                # untouched

    async def test_idempotent_no_failures(self, db_session, session_factory):
        out = await _prune_async(window_days=14, cap=500, session_factory=session_factory)
        assert out["deactivated"] == 0
