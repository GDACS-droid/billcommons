"""Join saved-monitor and API-sync snapshot-blocker migration histories.

Revision ID: 0032_intelligence_merge
Revises: 0031, 0031_snapshot_blockers

Both parent migrations retain their identities. Upgrading to this revision
applies whichever parent is missing before joining the two branches.
"""

revision = "0032_intelligence_merge"
down_revision = ("0031", "0031_snapshot_blockers")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
