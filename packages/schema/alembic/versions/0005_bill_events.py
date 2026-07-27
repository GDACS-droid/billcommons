"""bill_events: an append-only change log

`/changes` previously read `bills.updated_at`. That column cannot answer the
questions a change feed exists to answer, and three separate failure modes all
trace back to it:

  * It carries no KIND. "This bill changed" forces a consumer to re-fetch and
    diff every child collection to discover whether it was a status move, a
    new action, a new sponsor, or new document text.
  * It only exists on `bills`, so every writer of a CHILD row has to remember
    to reach up and stamp the parent. Two paths were already found not doing
    it (sponsorships, and the full-text crawl landing extracted_text -- "new
    amendment text posted", which is arguably the single most valuable event
    this system produces).
  * It is MUTABLE, so a wholesale re-derivation restamps tens of thousands of
    rows and publishes them as a wave of changes indistinguishable from real
    legislative movement.

An append-only log fixes all three structurally rather than by remembering.

`seq` is the cursor. It is a bigserial rather than a timestamp because
timestamps in this corpus tie heavily -- a single bulk-load instant covers
25,240 bills -- and a cursor that cannot break a tie cannot page past one.

READERS MUST NOT SERVE THE HEAD OF THIS TABLE. A sequence value is allocated
at INSERT and becomes visible at COMMIT, so a long-running writer can hold a
low `seq` that appears only after higher ones are already visible; a consumer
that advanced past it never sees it. The reader therefore only serves rows
below a watermark older than the longest possible write transaction. See
billcommons_api.routers.changes.COMMIT_SAFETY_LAG_SECONDS.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bill_events",
        sa.Column("seq", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "bill_id",
            UUID(as_uuid=True),
            sa.ForeignKey("bills.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # created | status | actions | sponsors | text | metadata
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Optional human-readable detail, e.g. "in_committee -> enacted".
        sa.Column("detail", sa.Text(), nullable=True),
    )
    # The feed's only access path: a range scan from the consumer's cursor.
    op.create_index("ix_bill_events_seq", "bill_events", ["seq"])
    # Watermark lookup (max seq older than the safety lag) and retention.
    op.create_index("ix_bill_events_changed_at", "bill_events", ["changed_at"])
    # Serves `/changes?ids=` -- a watchlist asking only about its own bills,
    # which must cost result-size, not corpus-size.
    op.create_index("ix_bill_events_bill_id_seq", "bill_events", ["bill_id", "seq"])


def downgrade() -> None:
    op.drop_index("ix_bill_events_bill_id_seq", table_name="bill_events")
    op.drop_index("ix_bill_events_changed_at", table_name="bill_events")
    op.drop_index("ix_bill_events_seq", table_name="bill_events")
    op.drop_table("bill_events")
