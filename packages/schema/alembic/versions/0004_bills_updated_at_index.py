"""bills: index (updated_at, id) for the change feed

`/api/v1/changes` is a keyset range scan over `(updated_at, id)` -- the two
columns in that exact order, because a timestamp alone cannot page past a tie
and this table has single instants covering 25,240 bills.

Unindexed, every page of that feed was a sequential scan of all 209k bills
plus a top-N sort: ~110ms and ~72k buffers to return 100 rows. A consumer
doing a first full sweep pages through the whole corpus, so the cost is paid
once per page -- thousands of full table scans to serve one sync, which is a
denial of service we would be running against ourselves.

Composite and in sort order so Postgres can walk the index and stop at LIMIT
rather than sorting; a lone `updated_at` index would still leave the tied rows
(which is most of them, post-bulk-load) to be sorted by id.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-27
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_bills_updated_at_id", "bills", ["updated_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_bills_updated_at_id", table_name="bills")
