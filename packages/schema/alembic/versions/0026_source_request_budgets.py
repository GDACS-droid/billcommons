"""Durable shared upstream request admission.

Revision ID: 0026
Revises: 0025
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_request_budgets",
        sa.Column("scope", sa.Text(), primary_key=True),
        sa.Column("budget_date", sa.Date(), nullable=False),
        sa.Column("requests_reserved", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("request_limit", sa.Integer(), nullable=False),
        sa.Column("minimum_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("next_request_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("length(scope) BETWEEN 1 AND 80", name="ck_source_budget_scope"),
        sa.CheckConstraint("requests_reserved >= 0", name="ck_source_budget_reserved"),
        sa.CheckConstraint("request_limit > 0", name="ck_source_budget_limit"),
        sa.CheckConstraint("minimum_interval_seconds BETWEEN 1 AND 3600", name="ck_source_budget_interval"),
    )


def downgrade() -> None:
    op.drop_table("source_request_budgets")
