"""Retain bounded source-change provenance for Scout reuse.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-01
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("scout_sources", sa.Column("change_kind", sa.Text(), nullable=True))
    op.add_column("scout_sources", sa.Column("change_summary", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("scout_sources", "change_summary")
    op.drop_column("scout_sources", "change_kind")
