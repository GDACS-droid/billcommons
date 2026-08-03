"""Optional jurisdiction scope on alert subscriptions.

Alerts were topic-only, so subscribing to a national topic meant a digest
covering all 50 states. That is the wrong product for the caller most likely to
want alerts at all: a city or county affairs office tracking its OWN
legislature. Lauderhill watching "local-government" would have received ~4,200
bills' worth of events, of which the Florida slice is what they actually need.

NULL means national -- the existing behaviour, and the existing single row
keeps it without a data migration.

The uniqueness contract of the subscribe endpoint is (email, kind, target,
jurisdiction), so the same address can hold both a Florida digest and a
national one. That is deliberate: they are different products, not a duplicate.

The replacement constraint is NULLS NOT DISTINCT (Postgres 15+; this server is
18.4). Widening a unique constraint with a nullable column is a classic way to
silently lose the constraint: under the default NULLS DISTINCT, every national
subscription has a NULL in the tuple, and Postgres considers NULLs distinct
from each other -- so (alice, topic, ai, NULL) could be inserted without limit
and the row-level guard would be gone precisely for the pre-existing case.

Revision ID: 0011
Revises: 0010
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "alert_subscriptions",
        sa.Column("jurisdiction", sa.Text(), nullable=True),
    )
    op.drop_constraint(
        "uq_alert_email_kind_target", "alert_subscriptions", type_="unique"
    )
    op.execute(
        "ALTER TABLE alert_subscriptions "
        "ADD CONSTRAINT uq_alert_email_kind_target_jurisdiction "
        "UNIQUE NULLS NOT DISTINCT (email, kind, target, jurisdiction)"
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_alert_email_kind_target_jurisdiction",
        "alert_subscriptions",
        type_="unique",
    )
    op.drop_column("alert_subscriptions", "jurisdiction")
    op.create_unique_constraint(
        "uq_alert_email_kind_target",
        "alert_subscriptions",
        ["email", "kind", "target"],
    )
