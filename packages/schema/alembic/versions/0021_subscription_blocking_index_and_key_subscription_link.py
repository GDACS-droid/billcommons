"""billing fix pass round 2: `incomplete` is never billing-authoritative
(fixlist item 1/E6 Gate A), and `api_keys.subscription_id` links a minted
key back to the subscription it was minted FOR (2026-08-21 monetization
Phase 2 round-2 fix pass).

**Item 1 (HIGH).** `uq_api_subscriptions_one_active_per_customer`
(migration 0020) blocked concurrency on `WHERE status NOT IN ('canceled',
'incomplete_expired')` -- which still includes `incomplete` (the status
Stripe assigns while a brand-new subscription's first invoice is unpaid).
One customer, therefore, could never have TWO subscriptions in flight even
when the second one is a legitimate retry of a first that is merely
`incomplete` (not yet resolved either way): `billing._active_subscription`
409'd the retry at the API layer, and even after loosening the Python-side
predicates (`_BLOCKING_SUB_STATUSES` == `_PLAN_AUTHORITY_STATUSES`,
`billing.py`), an INSERT for a second `incomplete` row would still hit
THIS index and 500-loop. Re-created here as `WHERE status IN ('active',
'trialing','past_due','unpaid')` -- i.e. blocking on exactly
`_BLOCKING_SUB_STATUSES`, so any number of `incomplete`/`incomplete_
expired`/`canceled` rows can coexist per customer, but at most one
genuinely billing-authoritative row still can.

**E6/Gate A.** `api_keys.subscription_id` (nullable FK -> `api_subscriptions.
id`, `ON DELETE SET NULL` -- a key must never be silently destroyed by a
subscription row disappearing) records which subscription a paid key was
minted for, so `billing._ensure_provisioned_for_subscription` can make "did
we already mint a key for THIS subscription" idempotent PER SUBSCRIPTION,
independent of which of the two racing trigger events
(`checkout.session.completed` vs a `customer.subscription.*`/`invoice.paid`
webhook) observes the subscription reach `active`/`trialing` first.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-21
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Item 1: block concurrency on exactly `_BLOCKING_SUB_STATUSES` --
    # `incomplete` (and `incomplete_expired`/`canceled`) may now coexist
    # with each other, but at most one row per customer may be
    # `active`/`trialing`/`past_due`/`unpaid` at a time.
    op.execute("DROP INDEX IF EXISTS uq_api_subscriptions_one_active_per_customer")
    op.execute(
        "CREATE UNIQUE INDEX uq_api_subscriptions_one_active_per_customer ON api_subscriptions "
        "(customer_id) WHERE status IN ('active', 'trialing', 'past_due', 'unpaid')"
    )

    op.add_column(
        "api_keys",
        sa.Column(
            "subscription_id",
            UUID(as_uuid=True),
            sa.ForeignKey("api_subscriptions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_api_keys_subscription_id", "api_keys", ["subscription_id"])
    op.execute(
        "CREATE UNIQUE INDEX uq_api_keys_one_live_per_subscription ON api_keys "
        "(subscription_id) WHERE status IN ('active', 'rotating') AND subscription_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("uq_api_keys_one_live_per_subscription", table_name="api_keys")
    op.drop_index("ix_api_keys_subscription_id", table_name="api_keys")
    op.drop_column("api_keys", "subscription_id")

    op.execute("DROP INDEX IF EXISTS uq_api_subscriptions_one_active_per_customer")
    # Reverting to 0020's stricter predicate must retain a
    # billing-authoritative row in preference to a later incomplete retry,
    # while deterministically resolving tied or NULL creation timestamps.
    op.execute(
        """
        DELETE FROM api_subscriptions a
        USING (
            SELECT id
            FROM (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY customer_id
                           ORDER BY
                               (status IN ('active', 'trialing', 'past_due', 'unpaid'))::int DESC,
                               created_at DESC NULLS LAST,
                               id DESC
                       ) AS rn
                FROM api_subscriptions
                WHERE status NOT IN ('canceled', 'incomplete_expired')
            ) ranked
            WHERE rn > 1
        ) duplicates
        WHERE a.id = duplicates.id
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_api_subscriptions_one_active_per_customer ON api_subscriptions "
        "(customer_id) WHERE status NOT IN ('canceled', 'incomplete_expired')"
    )
