"""feedback: site feedback ingestion

One row per submitted message from the /feedback form (or any API caller).
Deliberately minimal: no auth, no threading, no status workflow -- the point
is that a visitor can tell us something is wrong or missing without leaving
the site, and we can read it. Email is optional; a message with no reply
address is still worth having.

Revision ID: 0007
Revises: 0006
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feedback",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        # Which page the visitor was on ("/states/NY/..."), self-reported by
        # the form; context for "this looks wrong" messages.
        sa.Column("page", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("feedback")
