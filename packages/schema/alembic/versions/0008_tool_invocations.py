"""tool_invocations: aggregate usage telemetry for the MCP surface

We could not answer "is anyone actually using this?". The MCP logs showed 139
successful POSTs and, over the same window, exactly ONE tool call -- everything
else was connect, list the tools, disconnect: the signature of directory health
probers, not users. That distinction is invisible without recording it, and it
is the only distinction that matters when deciding whether distribution work is
landing.

Deliberately aggregate-only and unauthenticated. NO api key wall (an auth gate
added to measure usage suppresses the usage it measures), NO IP address, NO
query text, NO bill ids. Just: which tool, did it work, how long, and which
client family said hello. That is enough to tell a real research session from a
health check, and not enough to profile anyone.

MCP only. The REST surface is crawler-dominated -- 50,000 pageviews from 64
unique visitors -- so a row per request would be a write amplifier that
measures bots. Railway and Vercel already report REST latency.

Revision ID: 0008
Revises: 0007
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Tool name, e.g. "search_legislation". Not the arguments.
        sa.Column("tool", sa.Text(), nullable=False),
        # "ok" | "error"
        sa.Column("outcome", sa.Text(), nullable=False),
        # Structured error code when outcome = "error" (e.g. "ambiguous_bill").
        # Error CLASS, never an error message -- messages can quote user input.
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        # Client family from the MCP initialize handshake ("claude-code",
        # "cursor", ...) where the client volunteers it. Never a version, never
        # an instance id.
        sa.Column("client_family", sa.Text(), nullable=True),
        sa.CheckConstraint("outcome in ('ok','error')", name="ck_tool_invocations_outcome"),
    )
    # The only query shape this table serves: aggregate over a recent window.
    op.create_index(
        "ix_tool_invocations_occurred_at",
        "tool_invocations",
        ["occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tool_invocations_occurred_at", table_name="tool_invocations")
    op.drop_table("tool_invocations")
