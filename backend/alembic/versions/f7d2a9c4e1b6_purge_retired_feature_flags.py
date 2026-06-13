"""purge retired feature flag override + audit rows

清理 2026-06-13 feature-flag 白名单瘦身删除的 12 个 flag 在
feature_flag_overrides / feature_flag_audit 的孤儿行 + 已知历史孤儿。
取最干净策略:override 行 + audit 行一并删。

Revision ID: f7d2a9c4e1b6
Revises: e9c1a7f3b5d2
Create Date: 2026-06-13
"""
from alembic import op
import sqlalchemy as sa

revision = "f7d2a9c4e1b6"
down_revision = "e9c1a7f3b5d2"
branch_labels = None
depends_on = None

# Task 1 census REMOVE_SET(12) + 已知历史孤儿(注释里提过、白名单早已无)
REMOVED_FLAGS = [
    "ENABLE_DEFAULT_FLAT_SESSION",
    "ENABLE_FLAT_CONTINUOUS",
    "GRAMMAR_VALIDATOR_RETRY_MAX",
    "ENABLE_R1A_HOOK",
    "ENABLE_LLM_JUDGE",
    "ENABLE_G5_CROSSOVER",
    "ENABLE_TASK_SCHEMA_V2",
    "FLAT_CROSS_REGION_QUOTA",
    "FLAT_CROSS_REGION_ENFORCE",
    "ENABLE_TASK_STOP_LOSS",
    "TASK_STOP_LOSS_PASS_RATE_FLOOR",
    "TASK_STOP_LOSS_CONSECUTIVE_FAIL_ROUNDS",
    # 已知历史孤儿(子系统更早退役,白名单已无,override/audit 行可能残留)
    "ENABLE_CASCADE_LEGACY",
    "ENABLE_HIERARCHICAL_RAG_CACHE",
    "ENABLE_R5_L2_RANKING",
]


def upgrade():
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM feature_flag_overrides WHERE flag_name = ANY(:names)"),
        {"names": REMOVED_FLAGS},
    )
    conn.execute(
        sa.text("DELETE FROM feature_flag_audit WHERE flag_name = ANY(:names)"),
        {"names": REMOVED_FLAGS},
    )


def downgrade():
    # 不可逆:删除的 override/audit 行无法恢复(取最干净策略,设计已确认)。
    pass
