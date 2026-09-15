"""Add owner-scoped saved Scout monitors and their evidence-run journal.

Revision ID: 0031
Revises: 0030
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scout_monitors",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("customer_id", UUID(as_uuid=True), sa.ForeignKey("api_customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("original_query", sa.Text(), nullable=False),
        sa.Column("normalized_query", sa.Text(), nullable=False),
        sa.Column("jurisdiction", sa.Text(), nullable=False),
        sa.Column("cache_key", sa.Text(), nullable=False),
        sa.Column("cadence_seconds", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_completed_run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("consecutive_deferrals", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("cadence_seconds BETWEEN 21600 AND 604800", name="ck_scout_monitors_cadence"),
        sa.CheckConstraint("consecutive_deferrals >= 0", name="ck_scout_monitors_deferrals"),
        sa.UniqueConstraint("customer_id", "normalized_query", "jurisdiction", name="uq_scout_monitors_owner_query"),
    )
    op.create_index("ix_scout_monitors_due", "scout_monitors", ["next_run_at"], postgresql_where=sa.text("active"))
    op.create_table(
        "scout_monitor_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("monitor_id", UUID(as_uuid=True), sa.ForeignKey("scout_monitors.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "job_id", UUID(as_uuid=True),
            sa.ForeignKey(
                "scout_research_jobs.id", ondelete="NO ACTION", deferrable=True, initially="DEFERRED"
            ),
            nullable=True,
        ),
        sa.Column("baseline_run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("execution_mode", sa.Text(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("source_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("change_summary", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status in ('baseline','queued','completed','partial','failed','canceled','deferred')", name="ck_scout_monitor_runs_status"),
        sa.CheckConstraint("execution_mode in ('baseline','cached','coalesced','new')", name="ck_scout_monitor_runs_execution_mode"),
    )
    op.create_index("ix_scout_monitor_runs_monitor_scheduled", "scout_monitor_runs", ["monitor_id", "scheduled_for"])
    op.create_index("ix_scout_monitor_runs_job", "scout_monitor_runs", ["job_id"])


def downgrade() -> None:
    op.drop_table("scout_monitor_runs")
    op.drop_table("scout_monitors")
