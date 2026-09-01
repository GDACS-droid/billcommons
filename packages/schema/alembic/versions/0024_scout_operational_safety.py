"""Harden Scout browser lifecycle accounting and telemetry.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-01
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "scout_browser_sessions",
        sa.Column("routed_requests", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("scout_browser_sessions", sa.Column("cleanup_attempted_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint("ck_scout_browser_sessions_status", "scout_browser_sessions", type_="check")
    op.create_check_constraint(
        "ck_scout_browser_sessions_status",
        "scout_browser_sessions",
        "status in ('starting','running','released','cleanup_failed','reaping','abandoned')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_scout_browser_sessions_status", "scout_browser_sessions", type_="check")
    op.create_check_constraint(
        "ck_scout_browser_sessions_status",
        "scout_browser_sessions",
        "status in ('starting','running','released','cleanup_failed')",
    )
    op.drop_column("scout_browser_sessions", "cleanup_attempted_at")
    op.drop_column("scout_browser_sessions", "routed_requests")
