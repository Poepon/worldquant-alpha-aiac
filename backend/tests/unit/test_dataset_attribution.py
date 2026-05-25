"""Unit tests for backend/dataset_attribution.py (FLAT-path dataset derivation).

Covers derive_dataset_id (pure) + build_field_dataset_map (real in-memory ORM,
per [[feedback_orm_constructor_real_test]] — the field→dataset JOIN + name-vs-id
choice is the bug-prone part and must be exercised against a real session).
"""
import pytest

from backend.dataset_attribution import (
    _clear_cache,
    build_field_dataset_map,
    derive_dataset_id,
    resolve_dataset_id,
)


class TestDeriveDatasetId:
    def test_dominant_dataset_wins(self):
        m = {"a": "ds1", "b": "ds1", "c": "ds2"}
        assert derive_dataset_id(["a", "b", "c"], m) == "ds1"  # 2 vs 1

    def test_tie_broken_alphabetically(self):
        m = {"a": "zebra", "b": "alpha"}
        assert derive_dataset_id(["a", "b"], m) == "alpha"  # 1-1 tie → alphabetical

    def test_case_insensitive_field_lookup(self):
        assert derive_dataset_id(["CLOSE"], {"close": "pv1"}) == "pv1"

    def test_empty_inputs_return_none(self):
        assert derive_dataset_id([], {"a": "x"}) is None
        assert derive_dataset_id(["a"], {}) is None
        assert derive_dataset_id(None, {"a": "x"}) is None

    def test_unmapped_fields_return_none(self):
        assert derive_dataset_id(["unknown_field"], {"a": "x"}) is None


class TestResolveDatasetId:
    # a-fix 2026-05-23: fields are ground truth; anchor is only a fallback.
    def test_fields_win_over_differing_anchor(self):
        # The 3406 bug: anchored on pv96 but fields are analyst4 → analyst4 wins.
        m = {"anl4_cfps": "analyst4"}
        assert resolve_dataset_id(["anl4_cfps"], m, anchor="pv96") == "analyst4"

    def test_falls_back_to_anchor_when_no_field_resolves(self):
        # Catalog gap / fieldless expr → keep the anchor (legacy behavior).
        assert resolve_dataset_id(["unknown"], {"a": "x"}, anchor="pv13") == "pv13"
        assert resolve_dataset_id([], {}, anchor="pv13") == "pv13"

    def test_oneshot_anchor_equals_derived_is_noop(self):
        # ONESHOT: anchor == the mined dataset → derive returns the same → no-op.
        m = {"fn_assets": "fundamental2"}
        assert resolve_dataset_id(["fn_assets"], m, anchor="fundamental2") == "fundamental2"

    def test_no_anchor_no_resolve_returns_none(self):
        assert resolve_dataset_id(["unknown"], {"a": "x"}, anchor=None) is None


@pytest.mark.asyncio
class TestBuildFieldDatasetMap:
    async def test_real_orm_field_to_dataset_name(self, db_session):
        from backend.models import DataField, DatasetMetadata

        _clear_cache()
        # Cell-stats normalization: datasets/datafields are universe-invariant
        # defs (no universe column); region lives on the dataset def.
        ds = DatasetMetadata(
            dataset_id="fundamental2", region="USA", name="Fundamental 2",
        )
        db_session.add(ds)
        await db_session.flush()  # populate ds.id (FK target)
        db_session.add(DataField(
            dataset_id=ds.id, field_id="fn_assets", field_name="Assets",
        ))
        db_session.add(DataField(
            dataset_id=ds.id, field_id="fn_debt", field_name="Debt",
        ))
        await db_session.commit()

        m = await build_field_dataset_map(db_session, "USA", "TOP3000")
        # maps field_id → dataset NAME (not the metadata-row FK id)
        assert m.get("fn_assets") == "fundamental2"
        assert m.get("fn_debt") == "fundamental2"
        # end-to-end: derive an alpha's dataset from its fields
        assert derive_dataset_id(["fn_assets", "fn_debt"], m) == "fundamental2"

    async def test_soft_fail_unknown_region_returns_empty(self, db_session):
        _clear_cache()
        m = await build_field_dataset_map(db_session, "ZZZ", "NONE")
        assert m == {}
