"""webhook_subscriptions.challenge_attempted_at: rotation for run_challenges

Verify round-11 fix #5 (opus C, HIGH): `run_challenges` selected due,
unverified subs with a fixed `ORDER BY created_at` -- under any STABLE
ordering, a page of slow/stuck unverified subs at the front of that order
starves every newer one forever, the identical starvation class fix
`rotation_order`/`eligible_deliveries` already exist to prevent on the
VERIFIED delivery path (see that function's own docstring). Unlike the
delivery path, challenges had no "least-recently-attempted" column of their
own to rotate on -- `last_attempt_at` is delivery-path-only (never written
by an unverified sub, which never reaches `_drain_one`) and `created_at`
is fixed at INSERT time forever, exactly the STABLE ordering that starves.

This column is `NULL` for a sub that has never had a challenge attempt (so
it always sorts first, same NULLS-FIRST convention `ix_webhook_subscriptions_
due` already uses for `last_attempt_at`), and is stamped with `now()` on
every `_attempt_challenge` call thereafter -- rotating the least-recently-
attempted unverified sub to the front of the next tick's batch.

Deliberately nullable with no default: a sub's very first challenge attempt
must sort as "never attempted" (NULL), identical in spirit to `last_seq`'s
own "computed at INSERT time, not a lazy default" convention from migration
0012 (see that migration's own docstring) -- except here the column starts
genuinely empty rather than watermark-computed, since there is nothing to
compute until the first attempt happens.

Revision ID: 0015
Revises: 0014
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "webhook_subscriptions",
        sa.Column("challenge_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Mirrors `ix_webhook_subscriptions_due`'s own partial-index shape --
    # the run_challenges SELECT filters to `verified = false AND active =
    # true` and orders by this column NULLS FIRST, so a partial index
    # scoped identically is the one that actually serves that query.
    op.create_index(
        "ix_webhook_subscriptions_challenge_due",
        "webhook_subscriptions",
        ["challenge_attempted_at"],
        postgresql_where=sa.text("active AND NOT verified"),
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_subscriptions_challenge_due", table_name="webhook_subscriptions")
    op.drop_column("webhook_subscriptions", "challenge_attempted_at")
