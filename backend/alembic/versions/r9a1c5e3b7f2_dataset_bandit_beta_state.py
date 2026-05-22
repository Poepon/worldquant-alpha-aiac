"""dataset-steering bandit: bandit_state Beta-Bernoulli columns

Revision ID: r9a1c5e3b7f2
Revises: q8f0d4c2e9b3
Create Date: 2026-05-22

Tier A of the dataset-steering bandit (plan dataset_steering_bandit_plan_v3).
Adds the discounted Beta-Bernoulli posterior to bandit_state:
  - alpha_param  Float  (discounted #successes; default 1.0 = uniform prior)
  - beta_param   Float  (discounted #failures;  default 1.0 = uniform prior)
  - pulls_at_last_refresh Int (cumulative-pull snapshot for window audit)

The weekly/daily ``run_dataset_weight_refresh`` beat job updates these and
samples θ=Beta(α,β) to write DatasetMetadata.mining_weight. Additive +
per-column inspector guard → re-run safe, byte-for-byte legacy when
ENABLE_DATASET_VALUE_BANDIT is OFF (job no-ops, nothing reads the columns).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "r9a1c5e3b7f2"
down_revision: Union[str, Sequence[str], None] = "q8f0d4c2e9b3"
branch_labels = None
depends_on = None


_NEW_COLUMNS = (
    ("alpha_param", sa.Column("alpha_param", sa.Float(), nullable=False, server_default="1.0")),
    ("beta_param", sa.Column("beta_param", sa.Float(), nullable=False, server_default="1.0")),
    ("pulls_at_last_refresh", sa.Column("pulls_at_last_refresh", sa.Integer(), nullable=False, server_default="0")),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "bandit_state" not in set(inspector.get_table_names()):
        # bandit_state created by an earlier baseline; nothing to alter if absent.
        return
    existing = {c["name"] for c in inspector.get_columns("bandit_state")}
    for name, column in _NEW_COLUMNS:
        if name not in existing:
            op.add_column("bandit_state", column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "bandit_state" not in set(inspector.get_table_names()):
        return
    existing = {c["name"] for c in inspector.get_columns("bandit_state")}
    for name, _ in reversed(_NEW_COLUMNS):
        if name in existing:
            op.drop_column("bandit_state", name)
