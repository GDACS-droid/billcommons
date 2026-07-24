"""jurisdiction_coverage: record how much full text is technically available

SPEC GREEN criterion #5 is "full text searchable wherever technically
available from source". Coverage rows only carried `full_text_count` (bills
we HAVE text for) and `bill_count` (all bills), so the only ratio the
promotion rule and the public matrix could express was text-vs-all-bills.
That is the wrong denominator twice over:

  * bills whose source publishes no document at all can never have text, so
    counting them against a jurisdiction understates it, and
  * documents that are terminally unfetchable (robots-disallowed, scanned
    PDFs with no text layer) are NOT "technically available", so counting
    them makes criterion #5 unreachable for e.g. DC/TN rather than a
    documentable limitation.

`full_text_available_count` is the honest denominator: bills with at least
one document we could still legitimately obtain text for. GREEN then means
"we have text for ~all of what is actually gettable", and a jurisdiction
whose sources are entirely robots-blocked lands at available == 0, where
criterion #5 is vacuously satisfied and the limitation is recorded in
known_gaps instead of being hidden behind a percentage.

Deliberately NULLable with no server_default, so "not yet recomputed" stays
distinguishable from "genuinely nothing obtainable". Backfilling 0 would make
every pre-existing row look like an all-robots-blocked jurisdiction, and the
promotion rule reads that as "criterion #5 is vacuous" -- handing a free GREEN
to all 51. NULL instead means unknown, and blocks promotion until a recompute
pass has actually measured the row.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-24
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jurisdiction_coverage",
        sa.Column("full_text_available_count", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jurisdiction_coverage", "full_text_available_count")
