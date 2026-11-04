"""Create run_checkpoints and action_approvals tables for M7 LangGraph + HITL.

Revision ID: 0003_m7
Revises: 0002_m6
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_m7"
down_revision: str | None = "0002_m6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
timestamp = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "run_checkpoints",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("node_name", sa.String(length=64), nullable=False),
        sa.Column("state_json", json_type, nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.UniqueConstraint("run_id", "step_index", name="run_checkpoints_run_step"),
    )
    op.create_index("ix_run_checkpoints_run_id", "run_checkpoints", ["run_id"])

    op.create_table(
        "action_approvals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("action_index", sa.Integer(), nullable=False),
        sa.Column("action_text", sa.Text(), nullable=False),
        sa.Column("risk", sa.String(length=16), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.UniqueConstraint("run_id", "action_index", name="action_approvals_run_action"),
    )
    op.create_index("ix_action_approvals_run_id", "action_approvals", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_action_approvals_run_id", table_name="action_approvals")
    op.drop_table("action_approvals")
    op.drop_index("ix_run_checkpoints_run_id", table_name="run_checkpoints")
    op.drop_table("run_checkpoints")
