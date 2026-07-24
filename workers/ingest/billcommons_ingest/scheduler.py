"""Refresh scheduler: enqueue `api_sync` jobs per SPEC "Refresh targets".

Cadence (docs/SPEC.md "Refresh targets" / ARCHITECTURE.md "Refresh policy"):
    active/special session -> every 30 min
    year-round session      -> hourly
    recently adjourned      -> daily
    dormant                 -> weekly session-status check
    (calendars/hearings are a separate, not-yet-built feed -- out of scope)

Cadence is derived HONESTLY from what `sessions` rows actually persist --
there is no separate "session_status" column in the schema (only
`active: bool`, `classification: "regular"|"special"`, `start_date`,
`end_date`; see packages/schema/billcommons_schema/models.py). The registry
JSON's richer `session_status` vocabulary (active/year_round/adjourned/
no_2026_regular_session) collapses onto those columns at seed time, so this
module reconstructs the same four-way split from them:

    active=True,  end_date is None        -> year-round        (hourly)
    active=True,  end_date is not None     -> active/special     (30 min)
    active=False, end_date within RECENT   -> recently adjourned (daily)
    active=False, otherwise (or no dates)  -> dormant             (weekly)

This mirrors the one real year-round jurisdiction in the registry (DC:
active=True, expected_adjournment=null) and is the only signal this schema
version can support -- documented here rather than silently guessed at.

Idempotent dispatch: `due_states` only returns (jurisdiction, kind) pairs
whose last enqueue is older than the cadence interval, checked against the
MOST RECENT `ingest_jobs` row of kind `api_sync` for that jurisdiction
(payload->>'state') that is queued/running/done -- i.e. "don't enqueue a
new one if the last one hasn't even had time to matter yet." This reuses
the existing `ingest_jobs` table rather than adding a new state table (per
the surgical-change principle -- one small query beats a new schema
migration for what is fundamentally the same durable job history).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from billcommons_ingest import queue as queue_mod
from billcommons_schema.models import IngestJob, Jurisdiction, Session as SessionModel

API_SYNC_KIND = "api_sync"

# Cadence tiers, in minutes.
CADENCE_ACTIVE_MINUTES = 30
CADENCE_YEAR_ROUND_MINUTES = 60
CADENCE_RECENTLY_ADJOURNED_MINUTES = 24 * 60
CADENCE_DORMANT_MINUTES = 7 * 24 * 60

# A session's end_date within this many days of "now" still counts as
# "recently adjourned" (daily cadence) rather than "dormant" (weekly) --
# SPEC distinguishes the two but doesn't give an exact cutoff; 30 days is a
# reasonable, documented choice (a month is long enough that any late
# post-adjournment paperwork/enrolled-bill filings have settled, but a
# freshly-adjourned session's list may still be finalizing for a few
# weeks).
RECENTLY_ADJOURNED_WINDOW_DAYS = 30

CADENCE_TIER_ACTIVE = "active"
CADENCE_TIER_YEAR_ROUND = "year_round"
CADENCE_TIER_RECENTLY_ADJOURNED = "recently_adjourned"
CADENCE_TIER_DORMANT = "dormant"

_CADENCE_MINUTES = {
    CADENCE_TIER_ACTIVE: CADENCE_ACTIVE_MINUTES,
    CADENCE_TIER_YEAR_ROUND: CADENCE_YEAR_ROUND_MINUTES,
    CADENCE_TIER_RECENTLY_ADJOURNED: CADENCE_RECENTLY_ADJOURNED_MINUTES,
    CADENCE_TIER_DORMANT: CADENCE_DORMANT_MINUTES,
}


def cadence_tier(
    *, active: bool, end_date, now: datetime | None = None
) -> str:
    """Pure function: classify a session's refresh cadence tier from the
    fields the schema actually stores. `end_date` is a `datetime.date` or
    None. `now` is injectable for tests (defaults to real UTC now)."""
    now = now or datetime.now(timezone.utc)

    if active:
        if end_date is None:
            return CADENCE_TIER_YEAR_ROUND
        return CADENCE_TIER_ACTIVE

    if end_date is not None:
        days_since_end = (now.date() - end_date).days
        if 0 <= days_since_end <= RECENTLY_ADJOURNED_WINDOW_DAYS:
            return CADENCE_TIER_RECENTLY_ADJOURNED

    return CADENCE_TIER_DORMANT


def cadence_minutes(tier: str) -> int:
    return _CADENCE_MINUTES[tier]


def is_due(*, last_enqueued_at: datetime | None, tier: str, now: datetime | None = None) -> bool:
    """Pure function: has enough time passed since the last enqueue for this
    tier's cadence? `last_enqueued_at=None` (never enqueued) is always due."""
    if last_enqueued_at is None:
        return True
    now = now or datetime.now(timezone.utc)
    return now - last_enqueued_at >= timedelta(minutes=cadence_minutes(tier))


@dataclass
class ScheduleDecision:
    jurisdiction_abbr: str
    tier: str
    due: bool
    last_enqueued_at: datetime | None


def _last_enqueued_at(db: OrmSession, state: str) -> datetime | None:
    """Most recent `ingest_jobs` row of kind api_sync for this state
    (queued/running/done -- any of those means a sync was actually
    scheduled for that state; a `dead` row from an exhausted-retries
    failure does NOT count, so a permanently-broken sync doesn't block
    re-scheduling forever)."""
    row = db.execute(
        select(IngestJob)
        .where(
            IngestJob.kind == API_SYNC_KIND,
            IngestJob.payload["state"].astext == state,
            IngestJob.status.in_(("queued", "running", "done")),
        )
        .order_by(IngestJob.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row.created_at if row is not None else None


def plan_schedule(db: OrmSession, *, now: datetime | None = None) -> list[ScheduleDecision]:
    """Read-only pass: for every jurisdiction with at least one session row,
    compute its cadence tier (from its most-recently-active-or-relevant
    session -- the one with the largest start_date) and whether an
    `api_sync` job is due. Does not enqueue anything -- see
    `run_schedule_pass` for the write side."""
    now = now or datetime.now(timezone.utc)
    decisions: list[ScheduleDecision] = []

    jurisdictions = db.execute(select(Jurisdiction)).scalars().all()
    for jurisdiction in jurisdictions:
        session_row = db.execute(
            select(SessionModel)
            .where(SessionModel.jurisdiction_id == jurisdiction.id)
            .order_by(SessionModel.active.desc(), SessionModel.start_date.desc().nulls_last())
        ).scalars().first()
        if session_row is None:
            continue

        tier = cadence_tier(active=session_row.active, end_date=session_row.end_date, now=now)
        last_enqueued_at = _last_enqueued_at(db, jurisdiction.abbreviation)
        due = is_due(last_enqueued_at=last_enqueued_at, tier=tier, now=now)
        decisions.append(
            ScheduleDecision(
                jurisdiction_abbr=jurisdiction.abbreviation,
                tier=tier,
                due=due,
                last_enqueued_at=last_enqueued_at,
            )
        )
    return decisions


def run_schedule_pass(db: OrmSession, *, now: datetime | None = None) -> list[str]:
    """Enqueue one `api_sync` ingest_jobs row for every jurisdiction whose
    cadence tier is due. Caller commits. Returns the list of jurisdiction
    abbreviations enqueued (for logging)."""
    decisions = plan_schedule(db, now=now)
    enqueued = []
    for decision in decisions:
        if not decision.due:
            continue
        queue_mod.enqueue(db, API_SYNC_KIND, {"state": decision.jurisdiction_abbr})
        enqueued.append(decision.jurisdiction_abbr)
    return enqueued
