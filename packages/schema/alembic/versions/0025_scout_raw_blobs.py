"""Add service-independent raw payload storage for Scout.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-01
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scout_raw_blobs",
        sa.Column("sha256", sa.Text(), primary_key=True),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("length(sha256) = 64", name="ck_scout_raw_blobs_sha256_length"),
        sa.CheckConstraint("length(data) <= 2097152", name="ck_scout_raw_blobs_data_size"),
    )


def downgrade() -> None:
    op.drop_table("scout_raw_blobs")
