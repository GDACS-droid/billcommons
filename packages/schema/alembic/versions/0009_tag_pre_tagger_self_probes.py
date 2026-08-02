"""Attribute the pre-tagger tool_invocations rows to our own uptime monitor

`tool_invocations` shipped on 2026-08-02 to answer "is anyone actually using
this?". The read-path monitor shipped the same day and calls a real MCP tool
every two minutes on purpose -- an `initialize` handshake succeeds against a
dead database, so nothing cheaper detects the outage it exists to catch.

Within two hours the monitor was 65 of the 67 rows in the table. The metric
built to measure adoption was overwhelmingly measuring the monitor, and
/stats/usage was publishing that number while its own `note` claimed health
probes were excluded.

The tagger (billcommons_mcp.telemetry.ClientFamilyMiddleware) fixes this going
forward. This migration corrects the rows written before it existed.

Why this is a factual correction and not a flattering one: every row is
attributable with certainty, not by assumption.

  * The 65 `get_jurisdiction_coverage` rows are spaced at 120s +/- 11s with NO
    exceptions -- the monitor's exact cadence. A human or agent call landing in
    that window would have produced a short gap. None exists.
  * The 2 `get_active_sessions` rows are maintainer verification calls made by
    hand while confirming telemetry worked end-to-end. Also not usage.

Bounded by timestamp and by `client_family IS NULL` so it can only ever touch
rows written before the tagger deployed; on a fresh database it is a no-op.
Irreversible in the strict sense -- downgrade cannot know which NULLs it set --
so downgrade restores NULL for exactly the same bounded window.

Revision ID: 0009
Revises: 0008
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The moment this migration actually ran against production, between the
# 21:22:08Z row (backfilled) and the 21:24:03Z row (which it did not touch).
#
# Deliberately NOT a round number in the future. The first draft said 21:30Z,
# four minutes ahead of the clock, which would have tagged the next two monitor
# ticks as self-probes before the tagger was even live -- manufacturing the
# evidence that the tagger worked. Any bound past the run time makes this
# migration capable of hiding the failure it is meant to correct.
CUTOFF = "2026-08-02 21:23:00+00"


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE tool_invocations
           SET client_family = 'self-probe'
         WHERE client_family IS NULL
           AND occurred_at < TIMESTAMPTZ '{CUTOFF}'
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE tool_invocations
           SET client_family = NULL
         WHERE client_family = 'self-probe'
           AND occurred_at < TIMESTAMPTZ '{CUTOFF}'
        """
    )
