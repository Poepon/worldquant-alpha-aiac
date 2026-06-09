"""
Metadata Models - Dataset and field metadata

Contains DatasetMetadata, DataField, Operator, and related models.
"""

from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, Text, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class DatasetMetadata(SQLAlchemyBase):
    """Dataset DEFINITION — region-scoped, universe/delay-INVARIANT.

    Refactor 2026-05-26 (multi-cell breadth): split from a single per-(region,
    universe) row into a definition row + per-(delay, universe) ``DatasetCellStats``,
    mirroring BRAIN's data-sets model (a definition + a ``data[]`` array of
    per-cell stats). UK is now ``(dataset_id, region)`` — universe/delay and all
    per-cell metrics (coverage/counts/mining_weight/...) moved to
    ``DatasetCellStats``. ``DataField.dataset_id`` still FKs ``datasets.id`` (PK
    unchanged), so a field belongs to exactly one (dataset_id, region) def.
    """
    __tablename__ = "datasets"
    __table_args__ = (
        UniqueConstraint('dataset_id', 'region', name='uq_dataset_region'),
        {'extend_existing': True}
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    dataset_id = Column(String(100), nullable=False)
    region = Column(String(10), nullable=False)
    name = Column(String(200), nullable=False, default="")
    description = Column(Text)
    category = Column(String(100))
    subcategory = Column(String(100))

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class DatasetCellStats(SQLAlchemyBase):
    """Per-(delay, universe) statistics for a dataset (one row per BRAIN
    ``data[]`` cell). ``region`` is reachable via ``dataset_ref → datasets.region``.

    ``mining_weight`` lives here so the dataset-steering bandit can become
    per-cell; today the refresh writes the same weight to every cell of a
    (region, dataset) — the schema is ready for per-cell reward later.
    """
    __tablename__ = "dataset_cell_stats"
    __table_args__ = (
        UniqueConstraint('dataset_ref', 'delay', 'universe', name='uq_dataset_cell'),
        {'extend_existing': True}
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    dataset_ref = Column(Integer, ForeignKey("datasets.id"), nullable=False)
    universe = Column(String(50), nullable=False, default="TOP3000")
    delay = Column(Integer, nullable=False, default=1)

    # Metrics (per cell)
    coverage = Column(Float)
    date_coverage = Column(Float)
    value_score = Column(Integer)
    user_count = Column(Integer)
    alpha_count = Column(Integer)
    field_count = Column(Integer)
    pyramid_multiplier = Column(Float)
    mining_weight = Column(Float, default=1.0)

    # Brain extras (per cell)
    themes = Column(JSONB)
    resources = Column(JSONB)

    # Mining stats (per cell)
    alpha_success_count = Column(Integer, default=0)
    alpha_fail_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)

    last_synced_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class DataField(SQLAlchemyBase):
    """Data field DEFINITION — universe/delay-INVARIANT, belongs to one dataset def.

    Real API structure from get_datafields: id / description / dataset{id,name} /
    category{id,name} / subcategory{id,name} / type (MATRIX|VECTOR|GROUP).

    Refactor 2026-05-26: per-cell stats (coverage/counts/themes/is_active) moved to
    ``DataFieldCellStats`` keyed by (delay, universe); ``region`` dropped (reachable
    via ``dataset_id → datasets.region``). UK ``(dataset_id, field_id)`` unchanged →
    the inbound FK ``datafields.dataset_id`` is preserved.
    """
    __tablename__ = "datafields"
    __table_args__ = (
        UniqueConstraint('dataset_id', 'field_id', name='uq_datafield_dataset_field'),
        {'extend_existing': True}
    )

    id = Column(Integer, primary_key=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"))
    field_id = Column(String(200), nullable=False)
    field_name = Column(String(200), nullable=False)
    field_type = Column(String(50))  # VECTOR, MATRIX, GROUP
    description = Column(Text)

    # Category info (API returns nested objects, we store IDs)
    category = Column(String(100))       # category.id
    category_name = Column(String(200))  # category.name
    subcategory = Column(String(100))    # subcategory.id
    subcategory_name = Column(String(200))  # subcategory.name

    created_at = Column(DateTime, server_default=func.now())


class DataFieldCellStats(SQLAlchemyBase):
    """Per-(delay, universe) statistics for a data field (one row per BRAIN
    ``data[]`` cell). ``region`` reachable via ``datafield_ref → datafields →
    datasets.region``. ``is_active`` is per cell (mining-driven prune flips it)."""
    __tablename__ = "datafield_cell_stats"
    __table_args__ = (
        UniqueConstraint('datafield_ref', 'delay', 'universe', name='uq_datafield_cell'),
        {'extend_existing': True}
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    datafield_ref = Column(Integer, ForeignKey("datafields.id"), nullable=False)
    universe = Column(String(50), nullable=False, default="TOP3000")
    delay = Column(Integer, nullable=False, default=1)

    # Metrics from API (per cell)
    date_coverage = Column(Float)        # dateCoverage
    coverage = Column(Float)             # coverage
    pyramid_multiplier = Column(Float)   # pyramidMultiplier
    user_count = Column(Integer)         # userCount
    alpha_count = Column(Integer)        # alphaCount
    themes = Column(JSONB, default=list)  # callable default — never share one list instance

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # ── Orthogonal-breadth field-exploration ledger (2026-06-09, PR-A) ──
    # Per-(field, universe, delay) mining stats for the field-coverage bandit
    # (gated ENABLE_FIELD_SCREENING, default OFF). Populated by the
    # run_field_ledger_refresh beat from `alphas` (field_id ∈ expression) +
    # alpha_pnl. Migration r2b7f4c9a1e3. INERT until the new code/flag deploy.
    #   - times_mined / distinct_alphas: usage → novelty (UCB ∝ 1/√(times+1))
    #   - signal_p90 / band_pass_count: dense signal (reward uses
    #     signal_p90 × can_submit_rate, NOT bare p90 — anti CONCENTRATED_WEIGHT)
    #   - orthogonality: informational only (1 - mean self_corr vs pool); the
    #     reward gates on self_corr<0.5 hard门, does NOT use orthogonality
    #     directly (design §0.2 — marginal-to-13-pool just relabels the loop)
    #   - last_mined: most recent alpha using this field (recency)
    times_mined = Column(Integer, default=0)
    distinct_alphas = Column(Integer, default=0)
    signal_p90 = Column(Float)
    band_pass_count = Column(Integer, default=0)
    orthogonality = Column(Float)
    last_mined = Column(DateTime)


class Operator(SQLAlchemyBase):
    """
    Operator - BRAIN platform operators.
    
    Real API structure from get_operators:
    - name: operator name (e.g., "ts_rank", "add")
    - category: operator category (e.g., "Arithmetic", "Time Series")
    - scope: list of scopes ["COMBO", "REGULAR", "SELECTION"]
    - definition: usage definition (e.g., "ts_rank(x, d)")
    - description: detailed description
    - documentation: documentation URL path (e.g., "/operators/ts_rank")
    - level: operator level (e.g., "ALL")
    """
    __tablename__ = "operators"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)
    category = Column(String(100))
    description = Column(Text)
    definition = Column(Text)
    scope = Column(ARRAY(String))
    level = Column(String(50))
    documentation = Column(String(200))  # API returns this field
    syntax = Column(Text)  # Legacy field
    param_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())


class OperatorBlacklist(SQLAlchemyBase):
    """
    Operator Blacklist - Operators that should not be used.
    """
    __tablename__ = "operator_blacklist"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True)
    operator_name = Column(String(100), unique=True, nullable=False)
    error_message = Column(Text)
    first_seen_at = Column(DateTime, server_default=func.now())
    hit_count = Column(Integer, default=1)
    is_active = Column(Boolean, default=True)


class Region(SQLAlchemyBase):
    """
    Region - Market regions.
    """
    __tablename__ = "regions"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True)
    code = Column(String(10), unique=True, nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())


class Universe(SQLAlchemyBase):
    """
    Universe - Stock universes.
    """
    __tablename__ = "universes"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True)
    region_id = Column(Integer, ForeignKey("regions.id"))
    code = Column(String(50), nullable=False)
    description = Column(Text)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())


class Neutralization(SQLAlchemyBase):
    """
    Neutralization - Neutralization methods.
    """
    __tablename__ = "neutralizations"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True)
    code = Column(String(50), unique=True, nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, server_default=func.now())


class PyramidMultiplier(SQLAlchemyBase):
    """
    Pyramid Multiplier - Multipliers by category/region.
    """
    __tablename__ = "pyramid_multipliers"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True)
    category = Column(String(100), nullable=False)
    region = Column(String(10), nullable=False)
    delay = Column(Integer, nullable=False)
    multiplier = Column(Float, nullable=False)
    last_synced_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class Template(SQLAlchemyBase):
    """
    Template - Alpha expression templates.
    """
    __tablename__ = "templates"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    expression = Column(Text, nullable=False)
    alpha_type = Column(String(20), default='atom', nullable=False)
    template_configurations = Column(JSONB)
    recommended_region = Column(String(10))
    recommended_universe = Column(String(50))
    recommended_delay = Column(Integer, default=1)
    recommended_decay = Column(Integer, default=0)
    success_rate = Column(Float, default=0.0)
    total_generated = Column(Integer, default=0)
    avg_sharpe = Column(Float)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class TemplateVariable(SQLAlchemyBase):
    """
    Template Variable - Variables in templates.
    """
    __tablename__ = "template_variables"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True)
    template_id = Column(Integer, ForeignKey("templates.id"))
    variable_name = Column(String(100), nullable=False)
    config_type = Column(String(50), nullable=False)
    allowed_values = Column(JSONB)
    default_value = Column(String(200))
