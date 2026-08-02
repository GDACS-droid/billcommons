"""Whether an ENROLLED bill's executive-action window has closed.

Lives in `shared`, not in the ingest worker, because BOTH the ingest status
derivation and the public API need it and they ship in different containers --
`infra/docker/Dockerfile.api` copies only `packages/` and `apps/api`, so an API
import of `billcommons_ingest` would pass every local test and then fail at
container start.
"""
from __future__ import annotations

from datetime import date, timedelta

ENROLLED = "enrolled"

# How long after sine die an ENROLLED bill can still credibly be described as
# "awaiting executive action".
#
# The ENROLLED carve-out from LIVE_STATUSES has no upper bound, which is right
# for weeks and wrong for years. Every state constitution gives the executive a
# bounded window -- typically 5 to 45 days from presentment -- after which the
# bill becomes law or dies by pocket veto. An enrolled bill whose session ended
# long ago is not waiting for anything; we simply never captured the final
# action.
#
# Measured 2026-08-02: of 4,918 enrolled bills, 3,274 (67%) sat in sessions
# that had adjourned more than 180 days earlier -- including 2,192 Texas bills
# from a session that ended 2025-06-02, fourteen months before, every one of
# them rendering "awaiting executive action (signature or veto)".
#
# 180 days is deliberately far beyond any state's deadline: the point is not to
# guess the outcome, only to stop asserting a wait that has certainly ended.
ENROLLED_PENDING_GRACE_DAYS = 180


def enrolled_outcome_is_uncaptured(
    status: str | None, session_end_date: date | None, today: date | None = None
) -> bool:
    """True when an ENROLLED bill's executive-action window has certainly closed.

    An unknown end date returns False for the same reason apply_session_outcome
    ignores it: those sessions are overwhelmingly two-year carryover biennia,
    where a bill enrolled in year one is genuinely still pending in year two.
    """
    if status != ENROLLED or session_end_date is None:
        return False
    return (today or date.today()) - session_end_date > timedelta(
        days=ENROLLED_PENDING_GRACE_DAYS
    )
