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

from sqlalchemy import select, text
from sqlalchemy.orm import Session as OrmSession

from billcommons_ingest import queue as queue_mod
from billcommons_ingest.validation import GREEN_FULLTEXT_COVERAGE_THRESHOLD
from billcommons_schema.models import IngestJob, Jurisdiction, Session as SessionModel

API_SYNC_KIND = "api_sync"
VALIDATE_KIND = "validate"

# Arbitrary but FIXED 64-bit key for the session-level Postgres advisory
# lock `run_schedule_pass` holds for the duration of its read-then-insert
# pass (see `run_schedule_pass` below). Any 64-bit int works as long as it's
# stable and not reused elsewhere in this codebase for a different lock
# purpose (grepped: nothing else calls pg_advisory_lock in this repo as of
# this writing).
SCHEDULE_PASS_ADVISORY_LOCK_KEY = 847_291_003_615_882_001

# Separate, FIXED 64-bit key for `enqueue_validation_jobs`'s own
# read-then-insert pass -- must be DIFFERENT from
# SCHEDULE_PASS_ADVISORY_LOCK_KEY so an api_sync schedule pass and a
# validation enqueue pass can run concurrently without contending on the
# same lock (they touch disjoint job kinds), while two concurrent
# validation-enqueue passes still serialize against each other.
VALIDATE_ENQUEUE_ADVISORY_LOCK_KEY = 847_291_003_615_882_002

# Separate, FIXED 64-bit key guarding the dedicated `validate-worker` process's
# per-cycle SELECTION pass (cli.py cmd_validate_worker) -- DIFFERENT from both
# keys above so running 2+ validate-worker instances never double-selects (and
# never double-validates) the same jurisdiction in the same cycle, while still
# being independent of the crawl worker's own schedule/validate-enqueue passes
# (which enqueue `ingest_jobs` rows; the dedicated worker instead calls
# `plan_validation` directly and validates in-process, see cli.py).
VALIDATE_WORKER_CYCLE_ADVISORY_LOCK_KEY = 847_291_003_615_882_003

DEFAULT_VALIDATE_BATCH = 3
DEFAULT_DEGRADED_RECHECK_AGE_HOURS = 6

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
    abbreviations enqueued (for logging).

    `plan_schedule` (read: which jurisdictions are due, from the LATEST
    ingest_jobs row per state) followed by a loop of inserts is a
    read-then-insert with a gap in between -- two concurrent callers (e.g.
    the worker loop's periodic schedule-refresh pass racing a manually
    triggered `schedule-refresh` CLI run, or two worker processes) could
    both read "not yet enqueued" for the same jurisdiction and both insert
    an api_sync job, burning the shared Open States API quota on a
    duplicate sync. `pg_try_advisory_xact_lock` (TRANSACTION-scoped, not
    session-scoped) serializes the WHOLE pass: a caller that can't acquire
    it immediately (another pass is already running) skips this pass
    entirely and returns an empty list -- the next scheduled pass will pick
    up anything that's actually due by then, so skipping is safe and never
    silently drops a jurisdiction.

    Deliberately `_xact_lock`, not the plain session-scoped
    `pg_try_advisory_lock` this used previously: the session-scoped lock is
    released only by an EXPLICIT `pg_advisory_unlock` on that same backend
    connection. If `queue_mod.enqueue` (or anything else in the `try` body)
    raised a DB error, the whole transaction would be aborted -- and the
    `finally`'s own `pg_advisory_unlock` call, running inside that same
    now-aborted transaction, would itself fail (Postgres refuses further
    statements on an aborted transaction until rollback), leaking the
    session-level lock on the pooled connection. Every future
    `run_schedule_pass` call that happened to reuse that same pooled
    connection would then find the lock permanently held and silently
    return `[]` forever -- a total, silent scheduling stall with no error
    surfaced anywhere. The xact-scoped lock has no such failure mode: it is
    automatically released the instant the transaction ends, by COMMIT *or*
    ROLLBACK, with no explicit unlock statement required at all."""
    acquired = db.execute(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": SCHEDULE_PASS_ADVISORY_LOCK_KEY}
    ).scalar_one()
    if not acquired:
        return []

    decisions = plan_schedule(db, now=now)
    enqueued = []
    for decision in decisions:
        if not decision.due:
            continue
        queue_mod.enqueue(db, API_SYNC_KIND, {"state": decision.jurisdiction_abbr})
        enqueued.append(decision.jurisdiction_abbr)
    return enqueued


# ---------------------------------------------------------------------------
# Validation scheduler: keep FULL_TEXT_SEARCHABLE/DEGRADED rows moving
# through validation instead of sitting there forever (see cli.py cmd_worker
# "PERIODIC RECOMPUTE"/"VALIDATION SCHEDULER" docstrings + coverage.py's
# module docstring: recompute alone only ever reaches FULL_TEXT_SEARCHABLE;
# validation.py's validate_and_record is the ONLY thing that can move a row
# into/out of VALIDATING/GREEN/DEGRADED, so something has to keep calling it
# for every jurisdiction, on its own schedule, without a human running
# `validate --state XX` by hand for all 51).
# ---------------------------------------------------------------------------

# Priority tiers a jurisdiction_coverage row can fall into for the validation
# queue, in enqueue order (lower number = enqueued first):
#   1. FULL_TEXT_SEARCHABLE and not GREEN -- ready to be promoted, the
#      highest-value thing this pass can do.
#   2. DEGRADED whose last validation attempt is older than
#      `degraded_recheck_age_hours` -- give a previously-failing jurisdiction
#      a chance to recover (e.g. after the 6b/6e surface-form fixes) instead
#      of leaving it degraded forever.
#   3. everything else still in play (VALIDATING or any other non-terminal,
#      non-NOT_STARTED status), oldest `last_attempt_at` first -- so a
#      jurisdiction that's simply never been checked (or checked longest ago)
#      gets priority over one that ran recently.
_VALIDATION_PRIORITY_SQL = """
    SELECT jurisdiction_id, abbreviation, status, last_attempt_at, last_validated_at, priority
    FROM (
        SELECT jc.jurisdiction_id, j.abbreviation, jc.status, jc.last_attempt_at,
            last_validated.at AS last_validated_at,
            CASE
                WHEN jc.status = 'FULL_TEXT_SEARCHABLE' THEN 1
                -- Clocked off the last actual VALIDATION run, not
                -- jc.last_attempt_at. That column is written by three
                -- different things (registry seed, coverage recompute,
                -- validation), and the recompute pass restamps it every
                -- cycle -- so a cutoff against it is never satisfied and
                -- DEGRADED became unreachable here. Combined with DEGRADED's
                -- exclusion from priority 3 below, that made DEGRADED a
                -- terminal state no jurisdiction could recover from.
                WHEN jc.status = 'DEGRADED'
                     AND (last_validated.at IS NULL
                          OR last_validated.at <= :recheck_cutoff) THEN 2
                -- A GREEN row whose measured full-text coverage has fallen
                -- below the bar is STALE and must be rechecked, or it keeps
                -- the badge forever: GREEN is otherwise excluded from
                -- selection below, so nothing would ever re-evaluate it and
                -- apply_validation_result's demotion could never fire. This
                -- is how jurisdictions promoted under the old
                -- `full_text_count > 0` rule get re-judged. NULL available
                -- is "not yet measured" and deliberately does NOT qualify --
                -- a recompute pass resolves that first.
                -- Deliberately NOT gated on last_attempt_at: every coverage
                -- recompute stamps that column, so the recheck cutoff would
                -- never be satisfied and this tier would be dead. It is
                -- self-limiting instead -- one pass demotes the row out of
                -- GREEN, after which it no longer matches here.
                WHEN jc.status = 'GREEN'
                     AND jc.full_text_available_count IS NOT NULL
                     AND jc.full_text_available_count > 0
                     AND jc.full_text_count
                         < :green_fulltext_threshold * jc.full_text_available_count THEN 2
                -- Priority 3 is "anything else still in play" -- explicitly
                -- excludes DEGRADED (a DEGRADED row not yet due for its 6h
                -- recheck must be skipped entirely, not silently fall
                -- through to priority 3) in addition to the terminal/
                -- not-yet-started statuses already excluded below.
                WHEN jc.status NOT IN ('NOT_STARTED', 'SOURCE_IDENTIFIED', 'GREEN', 'BLOCKED', 'DEGRADED') THEN 3
                ELSE NULL
            END AS priority
        FROM jurisdiction_coverage jc
        JOIN jurisdictions j ON j.id = jc.jurisdiction_id
        LEFT JOIN LATERAL (
            SELECT max(vr.finished_at) AS at
            FROM validation_runs vr
            WHERE vr.jurisdiction_id = jc.jurisdiction_id
        ) last_validated ON TRUE
        WHERE jc.bill_count > 0
          AND NOT EXISTS (
              SELECT 1 FROM ingest_jobs ij
              WHERE ij.kind = :validate_kind
                AND ij.status IN ('queued', 'running')
                AND ij.payload ->> 'jurisdiction' = j.abbreviation
          )
    ) ranked
    WHERE priority IS NOT NULL
    -- Round-robin on the LAST ACTUAL VALIDATION, not jc.last_attempt_at.
    -- last_attempt_at is written by three different things, and the coverage
    -- recompute restamps EVERY row on every cycle -- so it encodes "when did
    -- recompute last touch this", not "when did we last validate this". All
    -- rows therefore carry near-identical timestamps and the ordering
    -- degenerates into the recompute's own write order: a fixed rotation
    -- whose head is re-validated forever while its tail starves.
    --
    -- Measured on 2026-07-25: NV had 77 validation runs and SD 61, while DE,
    -- WY and AK -- all sitting at 100% of obtainable full text and eligible
    -- for promotion -- had 8-11 runs and had not been validated in 17 hours.
    -- Thirteen rows met the GREEN full-text bar and could not be promoted
    -- because the scheduler kept re-picking the same handful. The crawl was
    -- not the bottleneck; this ordering was.
    ORDER BY priority ASC, last_validated_at ASC NULLS FIRST, abbreviation ASC
    LIMIT :batch
"""


@dataclass
class ValidationCandidate:
    jurisdiction_id: object
    jurisdiction_abbr: str
    status: str
    last_attempt_at: datetime | None
    priority: int
    # When this jurisdiction was ACTUALLY last validated (max
    # validation_runs.finished_at), as distinct from last_attempt_at, which
    # every coverage recompute restamps. This is what the round-robin must
    # order on; see the comment on _VALIDATION_PRIORITY_SQL's ORDER BY.
    last_validated_at: datetime | None = None


def plan_validation(
    db: OrmSession,
    *,
    batch: int = DEFAULT_VALIDATE_BATCH,
    degraded_recheck_age_hours: int = DEFAULT_DEGRADED_RECHECK_AGE_HOURS,
    now: datetime | None = None,
) -> list[ValidationCandidate]:
    """Read-only pass: pick up to `batch` jurisdictions to validate next, by
    priority (FULL_TEXT_SEARCHABLE-not-yet-GREEN, then stale DEGRADED, then
    oldest-checked-other), deduped against any jurisdiction that already has
    a queued/running `validate` job. One jurisdiction is only ever a
    candidate once per DB row even if it has multiple jurisdiction_coverage
    rows (e.g. multi-session); duplicates are collapsed keeping the
    highest-priority (lowest number) row. Does not enqueue anything -- see
    `enqueue_validation_jobs` for the write side."""
    now = now or datetime.now(timezone.utc)
    recheck_cutoff = now - timedelta(hours=degraded_recheck_age_hours)

    rows = db.execute(
        text(_VALIDATION_PRIORITY_SQL),
        {
            "recheck_cutoff": recheck_cutoff,
            "validate_kind": VALIDATE_KIND,
            "green_fulltext_threshold": GREEN_FULLTEXT_COVERAGE_THRESHOLD,
            # Over-fetch a little before per-jurisdiction dedup collapses
            # multi-session rows -- batch is a final cap applied after.
            "batch": batch * 4 + 10,
        },
    ).all()

    seen: dict[str, ValidationCandidate] = {}
    for jurisdiction_id, abbr, status, last_attempt_at, last_validated_at, priority in rows:
        candidate = ValidationCandidate(
            jurisdiction_id=jurisdiction_id,
            jurisdiction_abbr=abbr,
            status=status,
            last_attempt_at=last_attempt_at,
            priority=priority,
            last_validated_at=last_validated_at,
        )
        existing = seen.get(abbr)
        if existing is None or candidate.priority < existing.priority:
            seen[abbr] = candidate

    # Must mirror the SQL's ORDER BY exactly. Sorting here on last_attempt_at
    # silently RE-IMPOSED the starvation the SQL ordering was fixed to remove:
    # the query returned least-recently-validated first and this re-sorted it
    # straight back into recompute-write order. Order on the real last
    # validation, oldest (and never-validated) first.
    ordered = sorted(
        seen.values(),
        key=lambda c: (
            c.priority,
            c.last_validated_at or datetime.min.replace(tzinfo=timezone.utc),
            c.jurisdiction_abbr,
        ),
    )
    return ordered[:batch]


def enqueue_validation_jobs(
    db: OrmSession,
    batch: int = DEFAULT_VALIDATE_BATCH,
    *,
    degraded_recheck_age_hours: int = DEFAULT_DEGRADED_RECHECK_AGE_HOURS,
    sample_size: int | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Enqueue up to `batch` `validate` ingest_jobs rows (payload
    `{"jurisdiction": "XX"}` (+ `"sample": N` if `sample_size` given)),
    chosen by `plan_validation`'s priority order, deduped against any
    jurisdiction that already has a queued/running `validate` job. Caller
    commits. Returns the list of jurisdiction abbreviations enqueued.

    Guarded by `VALIDATE_ENQUEUE_ADVISORY_LOCK_KEY`
    (`pg_try_advisory_xact_lock`, transaction-scoped) for the exact same
    reason `run_schedule_pass` is guarded by its own key: this is a
    read-then-insert with a gap in between, so two concurrent callers (two
    worker processes' periodic validation-enqueue passes) could both read
    "no queued validate job for XX yet" and both insert one, double-hitting
    the production search API + the jurisdiction's official site for the
    same sample. A caller that can't acquire the lock immediately skips this
    pass entirely and returns an empty list -- safe, since the next
    scheduled pass will pick up anything still due.
    """
    acquired = db.execute(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": VALIDATE_ENQUEUE_ADVISORY_LOCK_KEY}
    ).scalar_one()
    if not acquired:
        return []

    candidates = plan_validation(
        db, batch=batch, degraded_recheck_age_hours=degraded_recheck_age_hours, now=now
    )
    enqueued = []
    for candidate in candidates:
        payload = {"jurisdiction": candidate.jurisdiction_abbr}
        if sample_size is not None:
            payload["sample"] = sample_size
        queue_mod.enqueue(db, VALIDATE_KIND, payload)
        enqueued.append(candidate.jurisdiction_abbr)
    return enqueued
