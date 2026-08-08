"""bill_documents: bound text_tsv's generated-column input to avoid the
Postgres tsvector 1MB-of-lexemes limit

Bug: `text_tsv` is a STORED generated column
(`to_tsvector('english', coalesce(extracted_text, ''))`, created by raw DDL
in 0001_initial_schema.py) computed on every write to the row. Postgres
caps tsvector storage at 1,048,575 bytes; any write of a document whose
extracted_text exceeds that fails the WHOLE statement, so the document ends
up with NO extracted_text at all (~309 outlier documents observed, plus
newly-fetched large bills failing since 07-24).

Fix is schema-only: there is no `to_tsvector()` call site in application
code to change (see fulltext.py -- it never touches text_tsv directly).

Target is postgres:16 (infra/docker/docker-compose.yml), so
`ALTER COLUMN ... SET EXPRESSION` (PG17+) is unavailable -- DROP+ADD is the
only route, which also means dropping and recreating the supporting GIN
index in the same migration.

WHY 250,000 BYTES AND NOT 1,000,000 (the number the first version of this
migration used, on the reasoning that "total lexeme storage can only be <=
input bytes"): that premise is false twice over, and it was MEASURED false
on postgres 16.14 before this threshold was picked.

  1. The default parser emits OVERLAPPING lexemes for compound tokens:
     `aa100000-bb100000` yields three -- the whole hyphenated word plus each
     part -- so 17 input bytes become 33 bytes of lexeme.
  2. The 1,048,575-byte budget is not lexemes alone. Postgres charges each
     entry its short-aligned lexeme length PLUS 2 bytes of position count
     PLUS 2 bytes per position, which adds ~4 bytes per distinct lexeme.

Measured amplification (lexeme+position bytes per input byte), worst first:

    hyphenated 2x5-char distinct tokens   2.69x
    hyphenated 2x8-char distinct tokens   2.48x
    URL-dense text                        1.94x
    plain distinct 8-char words           1.35x
    ordinary repeated-vocabulary prose    ~0.01x

The theoretical worst case for this parser is ~3x (a hyphenated pair of
distinct 4-5 char parts: whole + both parts, each paying the ~4-byte entry
overhead). 250,000 input bytes x 3 = 750,000 < 1,048,575, i.e. a 4.2x margin
against the MEASURED worst and 1.4x against the theoretical one. The old
1,000,000-byte guard was not a bound at all: a 666,017-byte hyphen-dense
document passes it and produces 1,702,046 bytes -- reproducing the exact
original bug, on the exact documents this migration exists to fix
(legislative text is full of hyphenation and long citation URLs).

The middle branch exists so an ASCII document just over the guard does not
fall off a cliff: `octet_length = char_length` is exactly "every character is
one byte", so left(text, 250000) there is exactly 250,000 bytes. Only
genuinely multi-byte text takes the conservative 62,500-character window
(62,500 x 4 bytes/char at UTF-8's worst case = 250,000 bytes). Every branch
therefore feeds to_tsvector at most 250,000 bytes, for ANY input -- no
"works for ASCII" assumption anywhere.

The cost is real and accepted: documents over 250KB are indexed only up to
that window, so a query matching solely on the tail of a 1MB appropriations
bill will not find it. `extracted_text` itself is never truncated -- the full
text is stored, served, and diffable; only what feeds the generated tsvector
column is bounded. A partial index is recoverable; the failure it replaces
lost the document's text entirely and silently.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-08
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept byte-identical to models.BillDocument.text_tsv's Computed(...) so
# SQLAlchemy metadata never drifts from the live DDL.
MAX_TSV_INPUT_BYTES = 250000
MAX_TSV_INPUT_CHARS_MULTIBYTE = 62500  # x4 bytes/char worst case = 250,000 bytes

NEW_EXPR = """
to_tsvector('english',
  CASE WHEN octet_length(coalesce(extracted_text, '')) <= 250000
       THEN coalesce(extracted_text, '')
       WHEN octet_length(coalesce(extracted_text, '')) = char_length(coalesce(extracted_text, ''))
       THEN left(coalesce(extracted_text, ''), 250000)
       ELSE left(coalesce(extracted_text, ''), 62500)
  END)
"""


def upgrade() -> None:
    op.execute("ALTER TABLE bill_documents DROP COLUMN text_tsv")  # drops its GIN index too
    op.execute(
        f"ALTER TABLE bill_documents ADD COLUMN text_tsv tsvector "
        f"GENERATED ALWAYS AS ({NEW_EXPR}) STORED"
    )
    op.execute("CREATE INDEX ix_bill_documents_text_tsv ON bill_documents USING GIN (text_tsv)")


def downgrade() -> None:  # restores 0001's unbounded expression
    op.execute("ALTER TABLE bill_documents DROP COLUMN text_tsv")
    op.execute(
        "ALTER TABLE bill_documents ADD COLUMN text_tsv tsvector GENERATED ALWAYS AS "
        "(to_tsvector('english', coalesce(extracted_text, ''))) STORED"
    )
    op.execute("CREATE INDEX ix_bill_documents_text_tsv ON bill_documents USING GIN (text_tsv)")
