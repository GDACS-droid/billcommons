"""webhook_creation_events: count creation ATTEMPTS, not surviving rows

Verify round-5 fix #4 (kimi #4, opus MED #2): `webhook_subscriptions` is a
set of SURVIVING rows -- a hard `DELETE /api/v1/webhooks/{id}` and the
dispatcher's own 24h unverified-GC (workers/webhooks/dispatch_webhooks.py's
`run_challenges`) both remove rows outright. The per-IP daily creation quota
(`billcommons_api.routers.webhooks.MAX_CREATIONS_PER_IP_PER_DAY`) previously
counted rows in THAT table, which let one IP loop create -> delete forever,
resetting its own quota on every cycle: unbounded verification-challenge
POSTs to an attacker-chosen endpoint (~7,200/day at the pre-fix 5/day cap
recycled every few minutes), and repeat "webhook created" emails to a
caller-supplied, never-confirmed address.

This table is a permanent, append-only (from the API's perspective) ledger
of creation attempts that passed validation and every quota check -- see
`billcommons_schema.models.WebhookCreationEvent`'s own docstring. Only
workers/webhooks/dispatch_webhooks.py's `prune_creation_events` ever deletes
from it, and only rows older than 48h.

Migration 0013 has NOT yet been applied to prod either as of this writing --
0013 and 0014 will be applied together pre-deploy (see the round-5 fix
list). This migration does not touch 0013 or its subject matter
(`notify_pending`'s CHECK constraint).

Revision ID: 0014
Revises: 0013
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "webhook_creation_events",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("creator_ip", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_webhook_creation_events_creator_ip_created_at",
        "webhook_creation_events",
        ["creator_ip", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_webhook_creation_events_creator_ip_created_at", table_name="webhook_creation_events"
    )
    op.drop_table("webhook_creation_events")
