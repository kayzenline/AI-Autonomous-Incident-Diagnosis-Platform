"""Create incident, run, event, tool call, and evidence tables.

Revision ID: 0001_m2
Revises:
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_m2"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
timestamp = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "incidents",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("service", sa.Text(), nullable=True),
        sa.Column("occurred_at", timestamp, nullable=True),
        sa.Column("detected_at", timestamp, nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("user_severity", sa.Text(), nullable=True),
        sa.Column("metadata", json_type, nullable=False),
        sa.Column("archived_at", timestamp, nullable=True),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
    )
    op.create_index(
        "incidents_visible_created",
        "incidents",
        ["created_at"],
        sqlite_where=sa.text("archived_at IS NULL"),
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("incident_id", sa.String(length=36), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("agent_version", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("config", json_type, nullable=False),
        sa.Column("started_at", timestamp, nullable=True),
        sa.Column("ended_at", timestamp, nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("final_report", json_type, nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", timestamp, nullable=False),
    )
    op.create_index("runs_incident_id", "runs", ["incident_id"])
    op.create_index(
        "runs_idempotency",
        "runs",
        ["incident_id", "idempotency_key"],
        unique=True,
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "runs_one_active",
        "runs",
        ["incident_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('PENDING', 'RUNNING')"),
        postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"),
    )
    op.create_table(
        "run_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.UniqueConstraint("run_id", "seq", name="run_events_run_seq"),
    )
    op.create_index("ix_run_events_run_id", "run_events", ["run_id"])
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("event_id", sa.String(length=36), sa.ForeignKey("run_events.id"), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("input", json_type, nullable=False),
        sa.Column("output", json_type, nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=True),
    )
    op.create_index("ix_tool_calls_run_id", "tool_calls", ["run_id"])
    op.create_index("ix_tool_calls_event_id", "tool_calls", ["event_id"])
    op.create_table(
        "evidence",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("tool_call_id", sa.String(length=36), sa.ForeignKey("tool_calls.id"), nullable=False),
        sa.Column("evidence_key", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("time_range", json_type, nullable=True),
    )
    op.create_index("ix_evidence_run_id", "evidence", ["run_id"])
    op.create_index("ix_evidence_tool_call_id", "evidence", ["tool_call_id"])


def downgrade() -> None:
    op.drop_table("evidence")
    op.drop_table("tool_calls")
    op.drop_table("run_events")
    op.drop_index("runs_one_active", table_name="runs")
    op.drop_index("runs_idempotency", table_name="runs")
    op.drop_index("runs_incident_id", table_name="runs")
    op.drop_table("runs")
    op.drop_index("incidents_visible_created", table_name="incidents")
    op.drop_table("incidents")
