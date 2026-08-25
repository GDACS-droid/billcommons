"""billing fix pass: `incomplete_expired` joins `canceled` as terminal in
the one-non-terminal-subscription-per-customer index (2026-08-21
monetization Phase 2 fix pass, fixlist item 3).

`uq_api_subscriptions_one_active_per_customer` (migration 0019) was
`WHERE status NOT IN ('canceled')`. Stripe's `incomplete_expired` is also a
TERMINAL status (Checkout in subscription mode creates the subscription
`incomplete` when the first payment needs action/fails, and it expires to
`incomplete_expired` after ~23h) -- that row is never `canceled`, so under
the pre-fix predicate: (a) the logged-in customer's next Checkout attempt
409'd forever (`billing._active_subscription` used the same `NOT IN
('canceled')` filter); (b) a guest retry's money was captured at Stripe,
then `checkout.session.completed` routed into the duplicate-subscription
branch and canceled the brand-new (paid-for) subscription with no refund
and no local record; (c) even after fixing the Python-side filters (this
fix pass's `_TERMINAL_SUB_STATUSES` constant in `billing.py`), an insert
for that customer's retry would still hit THIS index and 500-loop, because
the index itself only knew about `canceled`. Re-created here with
`WHERE status NOT IN ('canceled', 'incomplete_expired')`.

Fixlist item 22 (`snapshot_entitlements.stripe_payment_intent_id` needs a
partial unique index) turned out to be ALREADY SATISFIED by migration
0019's own `uq_snapshot_entitlements_payment_intent` (`WHERE
stripe_payment_intent_id IS NOT NULL`, no `kind` filter -- broader than
what item 22 asked for) -- Phase 1's amendment pass added it before this
fix pass ran. No new index needed; `billing.py`'s insert/read were still
updated to rely on it (`ON CONFLICT DO NOTHING` + `.scalars().first()`)
rather than assume no other writer could ever race it.

Round-2 fixlist item 9 (found in this pass, blocks a release rollback):
`upgrade()` above permits MULTIPLE `incomplete_expired` rows per customer
(a normal state -- each expired Checkout retry leaves one) -- but the
original `downgrade()` unconditionally recreated 0019's stricter
`WHERE status NOT IN ('canceled')` index, which forbids more than one
non-canceled row per customer. The moment any customer accumulates two
`incomplete_expired` rows under the upgraded schema, `downgrade()`'s
`CREATE UNIQUE INDEX` fails outright and the release cannot be rolled
back -- round-1's own "`upgrade -> downgrade -> upgrade` on a disposable
DB" gate passed only because it ran against an EMPTY database. Fixed by
collapsing surplus non-canceled rows per customer (keep the newest,
delete the rest) before recreating the stricter index -- re-tested with
seeded rows, not an empty DB.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-21
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Item 3: re-create the one-non-terminal-subscription-per-customer
    # index with `incomplete_expired` joining `canceled` as terminal.
    op.execute("DROP INDEX IF EXISTS uq_api_subscriptions_one_active_per_customer")
    op.execute(
        "CREATE UNIQUE INDEX uq_api_subscriptions_one_active_per_customer ON api_subscriptions "
        "(customer_id) WHERE status NOT IN ('canceled', 'incomplete_expired')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_api_subscriptions_one_active_per_customer")
    # Keep the billing-authoritative row when collapsing to 0019's stricter
    # predicate. `created_at` alone is not sufficient: PostgreSQL's `now()`
    # can tie within a bulk transaction and a newer incomplete retry must
    # never displace an older active subscription.
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
                WHERE status != 'canceled'
            ) ranked
            WHERE rn > 1
        ) duplicates
        WHERE a.id = duplicates.id
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_api_subscriptions_one_active_per_customer ON api_subscriptions "
        "(customer_id) WHERE status NOT IN ('canceled')"
    )
