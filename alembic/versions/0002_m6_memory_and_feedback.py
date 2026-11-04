"""Create hypotheses, hypothesis_evidence, feedback, memory_items, and memory_recalls tables.

Revision ID: 0002_m6
Revises: 0001_m2
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_m6"
down_revision: str | None = "0001_m2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
timestamp = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "hypotheses",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("cause", sa.String(length=64), nullable=False),
        sa.Column("cause_summary", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("run_id", "cause", name="hypotheses_run_cause"),
    )
    op.create_index("ix_hypotheses_run_id", "hypotheses", ["run_id"])

    op.create_table(
        "hypothesis_evidence",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "hypothesis_id",
            sa.String(length=36),
            sa.ForeignKey("hypotheses.id"),
            nullable=False,
        ),
        sa.Column(
            "evidence_id",
            sa.String(length=36),
            sa.ForeignKey("evidence.id"),
            nullable=False,
        ),
        sa.Column("relation", sa.String(length=32), nullable=False),
        sa.UniqueConstraint(
            "hypothesis_id",
            "evidence_id",
            "relation",
            name="hypothesis_evidence_unique",
        ),
    )
    op.create_index(
        "ix_hypothesis_evidence_hypothesis_id",
        "hypothesis_evidence",
        ["hypothesis_id"],
    )
    op.create_index(
        "ix_hypothesis_evidence_evidence_id",
        "hypothesis_evidence",
        ["evidence_id"],
    )

    op.create_table(
        "feedback",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("true_cause", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", timestamp, nullable=False),
    )
    op.create_index("ix_feedback_run_id", "feedback", ["run_id"])

    op.create_table(
        "memory_items",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("layer", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("service", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=True),
        sa.Column("cause", sa.String(length=64), nullable=True),
        sa.Column("trust_level", sa.String(length=32), nullable=False),
        sa.Column(
            "source_run_id",
            sa.String(length=36),
            sa.ForeignKey("runs.id"),
            nullable=True,
        ),
        sa.Column(
            "source_feedback_id",
            sa.String(length=36),
            sa.ForeignKey("feedback.id"),
            nullable=True,
        ),
        sa.Column("embedding", json_type, nullable=False),
        sa.Column("valid_from", timestamp, nullable=True),
        sa.Column("valid_until", timestamp, nullable=True),
        sa.Column("service_version", sa.Text(), nullable=True),
        sa.Column("usage_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("helpful_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_used_at", timestamp, nullable=True),
        sa.Column(
            "superseded_by",
            sa.String(length=36),
            sa.ForeignKey("memory_items.id"),
            nullable=True,
        ),
        sa.Column("created_at", timestamp, nullable=False),
    )
    op.create_index("ix_memory_items_layer", "memory_items", ["layer"])
    op.create_index("ix_memory_items_service", "memory_items", ["service"])
    op.create_index("ix_memory_items_trust_level", "memory_items", ["trust_level"])
    op.create_index("ix_memory_items_source_run_id", "memory_items", ["source_run_id"])

    op.create_table(
        "memory_recalls",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column(
            "memory_id",
            sa.String(length=36),
            sa.ForeignKey("memory_items.id"),
            nullable=False,
        ),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("injected", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("cited", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("run_id", "memory_id", name="memory_recalls_run_memory"),
    )
    op.create_index("ix_memory_recalls_run_id", "memory_recalls", ["run_id"])
    op.create_index("ix_memory_recalls_memory_id", "memory_recalls", ["memory_id"])


def downgrade() -> None:
    op.drop_index("ix_memory_recalls_memory_id", table_name="memory_recalls")
    op.drop_index("ix_memory_recalls_run_id", table_name="memory_recalls")
    op.drop_table("memory_recalls")
    op.drop_index("ix_memory_items_source_run_id", table_name="memory_items")
    op.drop_index("ix_memory_items_trust_level", table_name="memory_items")
    op.drop_index("ix_memory_items_service", table_name="memory_items")
    op.drop_index("ix_memory_items_layer", table_name="memory_items")
    op.drop_table("memory_items")
    op.drop_index("ix_feedback_run_id", table_name="feedback")
    op.drop_table("feedback")
    op.drop_index("ix_hypothesis_evidence_evidence_id", table_name="hypothesis_evidence")
    op.drop_index("ix_hypothesis_evidence_hypothesis_id", table_name="hypothesis_evidence")
    op.drop_table("hypothesis_evidence")
    op.drop_index("ix_hypotheses_run_id", table_name="hypotheses")
    op.drop_table("hypotheses")
