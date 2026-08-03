"""Trigram index on sponsorships.name, to make ?sponsor= searchable.

997,758 sponsorship rows carry a bare sponsor name and the only indexes on the
table are the primary key and bill_id. A substring search -- which is the only
useful kind here, because the stored values are bare surnames ("Rouson",
"Petrie-Norris") that a caller will type partially -- is therefore a sequential
scan over the whole table on a public, anonymous endpoint.

GIN + gin_trgm_ops rather than a btree on lower(name): a btree only accelerates
anchored prefixes, and the query this exists for is `ILIKE '%rouson%'`. pg_trgm
is already an installed extension (find_similar_bills uses similarity() over
bill titles), so this adds an index, not a dependency.

CONCURRENTLY, with an autocommit block: the table is live and read by every
bill-detail request, and a plain CREATE INDEX takes an ACCESS EXCLUSIVE lock
for the duration of the build. The trade is that this migration cannot run
inside a transaction, so a failure leaves an INVALID index behind rather than
rolling back -- drop and re-run if that happens.

Revision ID: 0010
Revises: 0009
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_sponsorships_name_trgm"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
            "ON sponsorships USING gin (lower(name) gin_trgm_ops)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
