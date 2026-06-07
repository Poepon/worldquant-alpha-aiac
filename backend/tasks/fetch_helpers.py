"""Dataset-field / operator fetch helpers shared with the HG pool.

Extracted verbatim from ``backend/tasks/mining_tasks.py`` in Phase 1c-delete so
the live pool (``backend/pool/hydrate.py``) can fetch its round fields/operators
without importing the (now-deleted) heavy FLAT mining task module.

Both functions are self-contained: ``_get_dataset_fields`` does NOT merge the
universal-PV whitelist (that merge lived at the FLAT caller and was retired with
the FLAT path). Behaviour is byte-for-byte the pre-1c FLAT field/operator fetch.
"""

from sqlalchemy import select, and_, case
from loguru import logger

from backend.config import settings
from backend.models import (
    DatasetMetadata,
    Operator,
    DataField,
    DataFieldCellStats,
)

# Default BRAIN delay. A delay-0 session passes delay=0; absent/1 = the
# established delay-1 path (byte-for-byte the legacy cell join).
_FLAT_DELAY = 1


def _is_signal_field(field_id, field_type) -> bool:
    """Field-hygiene (#25c): True if usable as a numeric alpha SIGNAL input.

    False for NON-SIGNAL metadata the code-gen LLM must never be offered as an
    alpha input — universe-membership flags (field_type UNIVERSE: top500/top200),
    symbols (SYMBOL), and UTC timestamps / dates / ISO-entity codes (by field_id
    substring). The LLM builds degenerate/garbage expressions on these (e.g.
    ts_zscore(entity_country_iso_code_4), subtract(top500,top500)=0) → 0/neg
    sharpe; this was the root of the 2026-05-20 submit-yield collapse. Pure +
    config-driven so it stays unit-testable without DB."""
    excl_types = set(getattr(settings, "FIELD_HYGIENE_EXCLUDE_TYPES", ["UNIVERSE", "SYMBOL"]) or [])
    if (field_type or "") in excl_types:
        return False
    fid = (field_id or "").lower()
    for sub in (getattr(settings, "FIELD_HYGIENE_EXCLUDE_ID_SUBSTRINGS",
                         ["_time_utc", "_date_utc", "iso_code"]) or []):
        if sub and sub in fid:
            return False
    return True


async def _get_operators(db):
    """Get operators for mining."""
    # ORDER BY category, name — deterministic, and ensures any downstream cap
    # spans categories rather than slicing off the (insertion-last) Cross
    # Sectional / Group operators (id 51-66). See plan a-streamed-wren.
    op_query = (
        select(Operator)
        .where(Operator.is_active == True)
        .order_by(Operator.category, Operator.name)
    )
    op_result = await db.execute(op_query)

    operators = []
    for op in op_result.scalars().all():
        operators.append({
            "name": op.name,
            "category": op.category,
            "description": op.description,
            "definition": op.definition
        })

    if not operators:
        # Fallback if DB is empty
        logger.warning("No operators found in DB, using basic set")
        operators = [
            {"name": "ts_rank", "category": "Time Series", "description": "Rank over time", "definition": "ts_rank(x, d)"},
            {"name": "ts_mean", "category": "Time Series", "description": "Mean over time", "definition": "ts_mean(x, d)"},
            {"name": "ts_std_dev", "category": "Time Series", "description": "Std Dev over time", "definition": "ts_std_dev(x, d)"},
            {"name": "ts_corr", "category": "Time Series", "description": "Correlation", "definition": "ts_corr(x, y, d)"},
            {"name": "ts_product", "category": "Time Series", "description": "Product over time", "definition": "ts_product(x, d)"},
            {"name": "ts_sum", "category": "Time Series", "description": "Sum over time", "definition": "ts_sum(x, d)"}
        ]

    return operators


async def _get_dataset_fields(db, dataset_id, region, universe, delay=_FLAT_DELAY):
    """Get fields for a dataset (cell-stats normalization: datasets/datafields are
    universe-invariant defs; the per-(universe, delay) cell supplies is_active).
    delay defaults to 1; a delay-0 session passes delay=0 so the LLM sees the
    delay-0-available field roster (sparser, partly distinct from delay-1)."""
    ds_meta_stmt = select(DatasetMetadata.id).where(
        DatasetMetadata.dataset_id == dataset_id,
        DatasetMetadata.region == region,
    )
    ds_meta_id = (await db.execute(ds_meta_stmt)).scalar_one_or_none()

    if ds_meta_id is None:
        return []

    # is_active == True (2026-05-22): exclude fields BRAIN rejects as
    # "Invalid data field" — auto-deactivated by prune_invalid_datafields.
    # Without this filter is_active was a dead flag and a stale catalog field
    # (e.g. pv96_eq_dvd_cash_cg_amt, 107 sim failures/wk once the dataset
    # bandit steered onto long-dormant pv96) kept being offered to the LLM.
    # is_active is now per (universe, delay) on datafield_cell_stats — join the
    # mining cell so a field deactivated in this universe is hidden here.
    # Value fields first (2026-05-23): GROUP-heavy datasets (pv13: 135 GROUP /
    # 30 MATRIX) would otherwise crowd MATRIX/VECTOR value fields out of the
    # downstream [:60]/[:30] caps, leaving the LLM only group fields to (mis)use
    # as value inputs. Order non-GROUP (value) fields ahead of GROUP.
    fields_stmt = (
        select(DataField.field_id, DataField.field_name, DataField.description, DataField.field_type)
        .join(
            DataFieldCellStats,
            and_(
                DataFieldCellStats.datafield_ref == DataField.id,
                DataFieldCellStats.universe == universe,
                DataFieldCellStats.delay == delay,
            ),
        )
        .where(
            DataField.dataset_id == ds_meta_id,
            DataFieldCellStats.is_active.is_(True),
        )
        .order_by(
            case((DataField.field_type == "GROUP", 1), else_=0),
            DataField.field_id,
        )
    )
    rows = (await db.execute(fields_stmt)).all()

    # Field hygiene (#25c): drop non-signal metadata (universe flags / symbols /
    # UTC timestamps / dates / ISO codes) so the LLM only sees real alpha inputs.
    # Flag-gated (default ON); OFF → byte-for-byte legacy roster.
    if bool(getattr(settings, "ENABLE_FIELD_HYGIENE", True)):
        _n0 = len(rows)
        rows = [r for r in rows if _is_signal_field(r[0], r[3])]  # field_id, field_type
        _dropped = _n0 - len(rows)
        if _dropped:
            logger.debug(
                f"[field-hygiene] {dataset_id}/{universe}/d{delay}: dropped "
                f"{_dropped}/{_n0} non-signal fields"
            )

    return [
        {"id": fid, "name": fname, "description": desc, "type": ftype}
        for (fid, fname, desc, ftype) in rows
    ]
