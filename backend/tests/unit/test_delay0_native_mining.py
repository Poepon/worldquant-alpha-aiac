"""Tests for native delay-0 mining wiring (②/B, 2026-05-26).

A FLAT session can be created at delay=0 so the LLM mines the delay-0 field
roster and the sim runs at delay-0 (a genuinely orthogonal axis vs our delay-1
submitted pool — transfer-re-sim of delay-1 winners FAILED, see
scripts/_probe_delay0.py, so native discovery is the only route).

delay flows: ops payload → start_flat_session(delay) → task.config['delay']
→ MiningState.delay → evaluation smart-settings override + the cell-join
delay (_task_delay / _get_dataset_fields delay param).

INVARIANT under test: delay=1 (the default) keeps every path byte-for-byte
identical — config omits the key, smart-settings gets no override, cell joins
pin delay=1. Per [[feedback_orm_constructor_real_test]] the join-bearing reads
+ the config-stamping path run against a real (aiosqlite) session.
"""
import pytest


# ---------------------------------------------------------------------------
# _task_delay — config resolution (default 1; never raises)
# ---------------------------------------------------------------------------
class _FakeTask:
    def __init__(self, config):
        self.config = config


class TestTaskDelayResolution:
    def test_absent_key_is_delay_1(self):
        from backend.tasks.mining_tasks import _task_delay
        assert _task_delay(_FakeTask({"flat_cursor": 0})) == 1
        assert _task_delay(_FakeTask({})) == 1
        assert _task_delay(_FakeTask(None)) == 1

    def test_delay_0_is_honored(self):
        from backend.tasks.mining_tasks import _task_delay
        assert _task_delay(_FakeTask({"delay": 0})) == 0
        assert _task_delay(_FakeTask({"delay": "0"})) == 0  # int() coerces

    def test_bad_config_falls_back_to_1(self):
        from backend.tasks.mining_tasks import _task_delay
        assert _task_delay(_FakeTask({"delay": "oops"})) == 1
        assert _task_delay(object()) == 1  # no .config attr → fallback


# ---------------------------------------------------------------------------
# _refresh_brain_client — FLAT 'fresh session' recovery (worldquant-miner pattern)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestRefreshBrainClient:
    async def test_refresh_recreates_client_and_reauths(self, monkeypatch):
        # Closes the global client, acquires a FRESH one, re-auths — the exact
        # sequence that breaks the long-lived-client sim-hang. Order matters:
        # close must precede get_client so a genuinely new client is built.
        from backend.tasks import mining_tasks
        from backend.adapters.brain_adapter import BrainAdapter
        calls = []

        async def _close(): calls.append("close")
        async def _get_client(): calls.append("get_client"); return "FRESH_CLIENT"
        monkeypatch.setattr(BrainAdapter, "close", classmethod(lambda cls: _close()))
        monkeypatch.setattr(BrainAdapter, "get_client", classmethod(lambda cls: _get_client()))

        class _Brain:
            client = "OLD_CLIENT"
            async def ensure_session(self): calls.append("ensure_session")
        b = _Brain()
        ok = await mining_tasks._refresh_brain_client(b)
        assert ok is True
        assert calls == ["close", "get_client", "ensure_session"]
        assert b.client == "FRESH_CLIENT"  # old (rotted) client replaced

    async def test_refresh_is_non_fatal_on_error(self, monkeypatch):
        # A refresh failure must NEVER raise into the FLAT loop (best-effort).
        from backend.tasks import mining_tasks
        from backend.adapters.brain_adapter import BrainAdapter

        async def _boom(): raise RuntimeError("brain down")
        monkeypatch.setattr(BrainAdapter, "close", classmethod(lambda cls: _boom()))

        class _Brain:
            client = "OLD"
            async def ensure_session(self): pass
        ok = await mining_tasks._refresh_brain_client(_Brain())
        assert ok is False  # swallowed, returns False


# ---------------------------------------------------------------------------
# MiningState.delay — default 1, accepts 0
# ---------------------------------------------------------------------------
class TestMiningStateDelayField:
    def test_default_delay_is_1(self):
        from backend.agents.graph.state import MiningState
        st = MiningState(task_id=1)
        assert st.delay == 1

    def test_delay_0_accepted(self):
        from backend.agents.graph.state import MiningState
        st = MiningState(task_id=1, delay=0)
        assert st.delay == 0


# ---------------------------------------------------------------------------
# smart-settings override — the evaluation node's injection mechanism
# ---------------------------------------------------------------------------
class TestSmartSettingsDelayOverride:
    def test_no_override_keeps_default_delay_1(self):
        from backend.sim_settings import smart_simulation_settings
        s = smart_simulation_settings("rank(close)", region="USA", universe="TOP3000",
                                      test_period="P2Y0M", overrides=None)
        assert s["delay"] == 1  # _BASE_DEFAULTS unchanged → delay-1 path intact

    def test_override_sets_delay_0(self):
        from backend.sim_settings import smart_simulation_settings
        s = smart_simulation_settings("rank(close)", region="USA", universe="TOP3000",
                                      test_period="P2Y0M", overrides={"delay": 0})
        assert s["delay"] == 0

    def test_evaluation_node_override_logic_mirrors_state_delay(self):
        # Mirrors the inline guard in node_evaluate: override only when delay != 1.
        from backend.sim_settings import smart_simulation_settings
        for _delay in (0, 1):
            ov = {"delay": _delay} if _delay != 1 else None
            s = smart_simulation_settings("rank(close)", region="USA", universe="TOP3000",
                                          test_period="P2Y0M", overrides=ov)
            assert s["delay"] == _delay


# ---------------------------------------------------------------------------
# _get_dataset_fields / _get_universal_pv_fields — per-delay cell join
# ---------------------------------------------------------------------------
async def _mk_field_cell(db, *, dataset_id, region, universe, field_id, delay, is_active=True):
    """Create (or reuse) a dataset+field def and add ONE datafield cell at the
    given (universe, delay)."""
    from sqlalchemy import select
    from backend.models import DatasetMetadata, DataField, DataFieldCellStats
    ds = (await db.execute(select(DatasetMetadata).where(
        DatasetMetadata.dataset_id == dataset_id, DatasetMetadata.region == region,
    ))).scalar_one_or_none()
    if ds is None:
        ds = DatasetMetadata(dataset_id=dataset_id, region=region, name=dataset_id)
        db.add(ds)
        await db.flush()
    df = (await db.execute(select(DataField).where(
        DataField.dataset_id == ds.id, DataField.field_id == field_id,
    ))).scalar_one_or_none()
    if df is None:
        df = DataField(dataset_id=ds.id, field_id=field_id, field_name=field_id.upper(),
                       field_type="MATRIX", description=f"{field_id} desc")
        db.add(df)
        await db.flush()
    db.add(DataFieldCellStats(datafield_ref=df.id, universe=universe, delay=delay,
                              is_active=is_active, coverage=0.9))
    await db.commit()


@pytest.mark.asyncio
class TestDatasetFieldsPerDelay:
    async def test_delay_param_selects_delay_0_cell(self, db_session):
        from backend.tasks.mining_tasks import _get_dataset_fields
        # close exists at BOTH delays; only_d0 exists at delay-0 only.
        await _mk_field_cell(db_session, dataset_id="pv1", region="USA",
                             universe="TOP3000", field_id="close", delay=1)
        await _mk_field_cell(db_session, dataset_id="pv1", region="USA",
                             universe="TOP3000", field_id="close", delay=0)
        await _mk_field_cell(db_session, dataset_id="pv1", region="USA",
                             universe="TOP3000", field_id="only_d0", delay=0)
        d1 = await _get_dataset_fields(db_session, "pv1", "USA", "TOP3000", 1)
        d0 = await _get_dataset_fields(db_session, "pv1", "USA", "TOP3000", 0)
        assert [f["id"] for f in d1] == ["close"]
        assert sorted(f["id"] for f in d0) == ["close", "only_d0"]

    async def test_default_delay_param_is_1(self, db_session):
        # Omitting the delay arg must behave exactly like delay=1 (byte-for-byte
        # legacy callers are unaffected).
        from backend.tasks.mining_tasks import _get_dataset_fields
        await _mk_field_cell(db_session, dataset_id="pv1", region="USA",
                             universe="TOP3000", field_id="close", delay=1)
        await _mk_field_cell(db_session, dataset_id="pv1", region="USA",
                             universe="TOP3000", field_id="only_d0", delay=0)
        out = await _get_dataset_fields(db_session, "pv1", "USA", "TOP3000")  # no delay arg
        assert [f["id"] for f in out] == ["close"]  # delay-0-only field NOT returned

    async def test_universal_pv_fields_delay_param(self, db_session):
        from backend.tasks.mining_tasks import _get_universal_pv_fields
        await _mk_field_cell(db_session, dataset_id="pv1", region="USA",
                             universe="TOP3000", field_id="close", delay=0)
        out = await _get_universal_pv_fields(db_session, "USA", "TOP3000", 0)
        assert [f["id"] for f in out] == ["close"]
        # delay-1 has no cell → empty (proves the join is delay-scoped)
        assert await _get_universal_pv_fields(db_session, "USA", "TOP3000", 1) == []


# ---------------------------------------------------------------------------
# _get_datasets_to_mine — delay-0 must exclude delay-1-only datasets
# ---------------------------------------------------------------------------
async def _mk_dataset_cell(db, *, dataset_id, region, universe, delay, weight=1.0):
    """Create (or reuse) a dataset def + a DatasetCellStats cell at (universe, delay)."""
    from sqlalchemy import select
    from backend.models import DatasetMetadata, DatasetCellStats
    ds = (await db.execute(select(DatasetMetadata).where(
        DatasetMetadata.dataset_id == dataset_id, DatasetMetadata.region == region,
    ))).scalar_one_or_none()
    if ds is None:
        ds = DatasetMetadata(dataset_id=dataset_id, region=region, name=dataset_id)
        db.add(ds)
        await db.flush()
    db.add(DatasetCellStats(dataset_ref=ds.id, universe=universe, delay=delay,
                            field_count=1, mining_weight=weight, is_active=True))
    await db.commit()


class _FakeMiningTask:
    def __init__(self, *, region, universe, config):
        self.region = region
        self.universe = universe
        self.config = config
        self.dataset_strategy = "AUTO"
        self.target_datasets = []


@pytest.mark.asyncio
class TestGetDatasetsToMinePerDelay:
    async def test_delay_0_excludes_delay_1_only_datasets(self, db_session):
        # ds_d0 has a delay-0 cell; ds_d1only has ONLY a delay-1 cell (like
        # model16/pv96). A delay-0 task must not pick ds_d1only (it would
        # field-skip the round → "starts at round 2").
        from backend.tasks.mining_tasks import _get_datasets_to_mine
        await _mk_dataset_cell(db_session, dataset_id="news12_d0", region="USA",
                               universe="TOP3000", delay=0)
        await _mk_dataset_cell(db_session, dataset_id="model16_d1", region="USA",
                               universe="TOP3000", delay=1)
        task0 = _FakeMiningTask(region="USA", universe="TOP3000", config={"delay": 0})
        out = await _get_datasets_to_mine(db_session, task0)
        assert "news12_d0" in out
        assert "model16_d1" not in out  # delay-1-only excluded at delay-0

    async def test_delay_1_keeps_permissive_outer_join(self, db_session):
        # The same delay-1-only dataset IS returned for a delay-1 task (the
        # permissive LEFT JOIN behavior is preserved byte-for-byte).
        from backend.tasks.mining_tasks import _get_datasets_to_mine
        await _mk_dataset_cell(db_session, dataset_id="model16_d1", region="USA",
                               universe="TOP3000", delay=1)
        task1 = _FakeMiningTask(region="USA", universe="TOP3000", config={"flat_cursor": 0})
        out = await _get_datasets_to_mine(db_session, task1)
        assert "model16_d1" in out


# ---------------------------------------------------------------------------
# _incremental_save_alphas — the LIVE persist path stamps delay from sim
# (the ONLY mined-alpha persist path post tier-system removal; workflow.py's
# ORM Alpha() batch path is skipped, so the fix must live HERE).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestIncrementalSaveStampsDelay:
    async def _capture_insert_delay(self, sim_delay, monkeypatch):
        from types import SimpleNamespace
        from sqlalchemy.dialects import postgresql
        from backend.config import settings
        from backend.agents.graph.nodes.persistence import _incremental_save_alphas

        monkeypatch.setattr(settings, "ENABLE_FAIL_ALPHA_PERSIST", True, raising=False)
        monkeypatch.setattr(settings, "HYPOTHESIS_REUSE_TERMINAL_GUARD_ENABLED", False, raising=False)

        captured = {}

        class _Ins:
            def scalar_one_or_none(self): return 999

        class _Sel:
            def scalar_one_or_none(self): return SimpleNamespace(id=999)

        class _Nested:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return False

        class _Stub:
            def begin_nested(self): return _Nested()
            async def execute(self, stmt):
                if "Insert" in type(stmt).__name__:
                    captured["params"] = stmt.compile(dialect=postgresql.dialect()).params
                    return _Ins()
                return _Sel()
            async def commit(self): pass

        metrics = {"sharpe": 0.4, "fitness": 0.05, "turnover": 0.35,
                   "_sim_settings": {"delay": sim_delay, "region": "USA"}}
        alpha = SimpleNamespace(
            expression="ts_rank(close, 10)", hypothesis="t", explanation="t",
            alpha_id="d0persist", is_valid=True, is_simulated=True,
            simulation_success=True, simulation_error=None, validation_error=None,
            quality_status="FAIL", metrics=metrics, parent_alpha_id=None,
            wrapper_kind=None,
        )
        await _incremental_save_alphas(
            db_session=_Stub(), task_id=42, run_id=None,
            region="USA", universe="TOP3000", dataset_id="pv13",
            pending_alphas=[alpha], hypothesis_id=None, g8_forest_referenced_ids=None,
        )
        return captured.get("params", {})

    async def test_persists_delay_0_from_sim_settings(self, monkeypatch):
        params = await self._capture_insert_delay(0, monkeypatch)
        assert params.get("delay") == 0  # ground truth from metrics._sim_settings

    async def test_persists_delay_1_from_sim_settings(self, monkeypatch):
        params = await self._capture_insert_delay(1, monkeypatch)
        assert params.get("delay") == 1


# ---------------------------------------------------------------------------
# Alpha.delay column — persisted from the sim's actual delay (not default 1)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestAlphaDelayColumnRoundTrip:
    async def test_alpha_persists_delay_0(self, db_session):
        # Per [[feedback_orm_constructor_real_test]]: the Alpha(delay=…) field
        # the workflow now sets must round-trip through a real session — a
        # delay-0 mined alpha must read back delay=0, not the column default 1.
        from sqlalchemy import select
        from backend.models import Alpha
        a = Alpha(task_id=999001, alpha_id="DELAY0TEST", expression="rank(close)",
                  region="USA", universe="TOP3000", dataset_id="news12", delay=0,
                  quality_status="FAIL")
        db_session.add(a)
        await db_session.commit()
        got = (await db_session.execute(
            select(Alpha.delay).where(Alpha.alpha_id == "DELAY0TEST"))).scalar_one()
        assert got == 0  # not the model default (1)

    async def test_alpha_default_delay_is_1(self, db_session):
        # Omitting delay still defaults to 1 (delay-1 path unchanged).
        from sqlalchemy import select
        from backend.models import Alpha
        a = Alpha(task_id=999002, alpha_id="DELAY1DEFAULT", expression="rank(close)",
                  region="USA", universe="TOP3000", dataset_id="pv1",
                  quality_status="FAIL")
        db_session.add(a)
        await db_session.commit()
        got = (await db_session.execute(
            select(Alpha.delay).where(Alpha.alpha_id == "DELAY1DEFAULT"))).scalar_one()
        assert got == 1


# ---------------------------------------------------------------------------
# start_flat_session — config stamping + validation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestStartFlatSessionDelay:
    async def _svc(self, db_session, monkeypatch):
        from backend.services.task_service import TaskService

        async def _noop_dispatch(self, *a, **k):
            return None
        monkeypatch.setattr(TaskService, "_dispatch_session_worker", _noop_dispatch)
        return TaskService(db_session)

    async def test_delay_1_omits_config_key(self, db_session, monkeypatch):
        svc = await self._svc(db_session, monkeypatch)
        info = await svc.start_flat_session(region="USA", universe="TOP3000",
                                            datasets=["pv1"], delay=1)
        task = await svc.task_repo.get_by_id(info.task_id)
        assert "delay" not in (task.config or {})  # byte-identical legacy config

    async def test_delay_0_stamps_config(self, db_session, monkeypatch):
        svc = await self._svc(db_session, monkeypatch)
        info = await svc.start_flat_session(region="USA", universe="TOP3000",
                                            datasets=["pv1"], delay=0)
        task = await svc.task_repo.get_by_id(info.task_id)
        assert (task.config or {}).get("delay") == 0

    async def test_invalid_delay_raises(self, db_session, monkeypatch):
        svc = await self._svc(db_session, monkeypatch)
        with pytest.raises(ValueError):
            await svc.start_flat_session(region="USA", universe="TOP3000", delay=2)
