#!/usr/bin/env python3
"""Bill Commons webhook dispatcher: a continuous Railway worker.

Runs OFF the box (unlike workers/alerts/send_alerts.py) because webhooks need
no box-only secret -- there is no Resend key here -- and an SSRF escape from a
box process reaches the home LAN/tailnet/local model servers, while an SSRF
escape from a disposable Railway container reaches only that container. See
billcommons_shared.safe_http for the actual SSRF guard; this module is the
only caller of it.

One tick every TICK_SECONDS, singleton-safe via a session-scoped Postgres
advisory lock re-checked at the top of every tick (`pg_try_advisory_lock` is
idempotent within one session/connection -- calling it again once already
held just re-confirms ownership; a second replica that never acquired it
keeps getting `false` and idles, doing no work, until this process's
connection closes and Postgres auto-releases the lock). Deliberately the
plain session-scoped lock, not `_xact_lock`: a tick is many short
transactions (see the module docstring on "transactions never span HTTP"
below), and the lock has to outlive every one of them for the whole tick, not
just one.

Per-tick order (verify round-1 fix #1 reordered steps 2/3 from the original
spec -- see run_challenges' docstring for why: challenges running FIRST with
no bound of their own could burn the whole tick before a single real
delivery got a turn):
  1. snapshot ONE watermark (same safety-lag rule as /changes -- see
     COMMIT_SAFETY_LAG_SECONDS)
  2. eligible deliveries (active+verified+due), rotated so a 51st subscriber
     is never permanently starved by a fixed ordering
  3. verification challenges (up to MAX_CHALLENGES_PER_TICK, oldest first,
     due per next_attempt_at), bounded to their own CHALLENGE_BUDGET_SECONDS
     slice of whatever tick budget deliveries left
  4. prune webhook_deliveries older than 30 days
  5. one stdout summary line

Transactions NEVER span HTTP (§4.5): every helper below that performs an
outbound POST returns to the caller BETWEEN the read that produced the
payload and the write that records the outcome, and the caller commits each
piece separately. `billcommons_shared.db`'s
`idle_in_transaction_session_timeout=300s` exists specifically to kill a
transaction held open across something slow like an outbound HTTP call; one
open across 50 slow POSTs in a single tick would get killed mid-run and lose
every cursor advance already made in that transaction.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import random
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, select, text, update
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm.exc import ObjectDeletedError

from billcommons_schema.models import (
    Bill,
    BillEvent,
    Jurisdiction,
    Session as SessionModel,
    WebhookDelivery,
    WebhookSubscription,
)
from billcommons_shared.cursor import encode_cursor
from billcommons_shared.db import get_engine, get_session
from billcommons_shared.safe_http import (
    DnsFailure,
    SafeHttpError,
    SsrfRejected,
    TimeoutFailure,
    TlsFailure,
    TooLarge,
    new_safe_http_client,
)
from billcommons_shared.topics import TOPICS, membership_clause
from billcommons_shared.watermark import COMMIT_SAFETY_LAG_SECONDS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TICK_SECONDS = 120
TICK_BUDGET_SECONDS = 90
PER_SUB_DRAIN_BUDGET_SECONDS = 15

# Arbitrary but fixed 63-bit-safe key, distinct from every key
# workers/ingest/billcommons_ingest/scheduler.py already uses (those are
# small hand-picked ints; this one is namespaced via a string hash so a
# future key added to either module can't collide by coincidence).
ADVISORY_LOCK_KEY = int.from_bytes(hashlib.sha256(b"billcommons.webhooks.dispatcher").digest()[:8], "big") & 0x7FFFFFFFFFFFFFFF

MAX_CHALLENGES_PER_TICK = 10
CHALLENGE_GC_AFTER = timedelta(hours=24)

#: Verify round-6 fix #1 (codex MED #2): CHALLENGE_GC_AFTER above only ever
#: GC's a sub that is STILL active=True (`run_challenges`' own SELECT below
#: filters on `active.is_(True)`) -- a promotion-time quota disable
#: (`_attempt_challenge`'s domain_quota_exceeded/global_quota_exceeded
#: branches, both `sub.active = False; sub.verified` left False) drops OUT
#: of that query entirely and, being unverified, was never touched by any
#: OTHER GC path either (`prune_deliveries`/`prune_creation_events` prune
#: different tables). Left alone, a burst of quota-rejected creations at a
#: saturated host/global cap grows `webhook_subscriptions` forever. 7 days,
#: not 24h, is deliberately long enough for a legitimate user to reactivate
#: (still exposed via `POST /webhooks/{id}/reactivate` for an
#: inactive-but-not-yet-GC'd row) once capacity frees up elsewhere on that
#: host/globally -- but still bounded forever.
UNVERIFIED_INACTIVE_RETENTION_DAYS = 7
#: Verification challenges are bounded to their OWN slice of the 90s tick
#: budget (verify round-1 fix #1) so up to 10 x 15s of serial challenge
#: traffic can never eat the whole tick and starve deliveries -- challenges
#: now run AFTER deliveries (see run_tick) and stop as soon as either this
#: budget or the tick deadline itself is exhausted, whichever comes first.
CHALLENGE_BUDGET_SECONDS = 20
#: Verify round-3 fix #10: a GUARANTEED floor on the challenge slice, even
#: when deliveries alone already consumed the entire 90s tick budget (a
#: broad receiver outage keeps every delivery worker busy the whole tick,
#: every tick). Without a floor, `challenge_deadline` computed as
#: `min(now + CHALLENGE_BUDGET_SECONDS, tick_deadline)` collapses to a value
#: already in the past whenever `tick_deadline` has already elapsed --
#: challenges get processed==0 EVERY tick, a brand-new subscription never
#: gets its first verification attempt, and it is GC'd unverified at the
#: 24h mark having never had a chance. This is a DELIBERATE, bounded
#: overrun of the nominal 120s tick cadence: worst case
#: TICK_BUDGET_SECONDS (90s) + CHALLENGE_MIN_SECONDS (15s) = 105s, still
#: under the 120s tick period, so the singleton advisory-lock loop never
#: overlaps itself.
CHALLENGE_MIN_SECONDS = 15

# r11 fix #5 (opus C, HIGH): a fixed `ORDER BY created_at` starves newer
# unverified subs behind a page of slow/stuck ones forever, AND (per that
# same finding) GC can't reclaim them because CHALLENGE_GC_AFTER's 24h
# window is measured from `created_at`, not from actual attempt activity --
# a sub that never got a turn under the old fixed ordering could sit due,
# unattempted, well past 24h. `run_challenges` now rotates on
# `challenge_attempted_at` (least-recently-attempted first, NULLS FIRST --
# migration 0015), AND disables outright after this many TOTAL failed
# attempts, independent of `created_at` age, so the GC path (which only
# reaps `active=False` rows -- see `gc_unverified_inactive`) applies
# reliably even if rotation is what finally gives a long-starved sub its
# first attempt at hour 23.
#
# N=10, sized from THIS module's own backoff schedule (`backoff_delay`:
# min(2^k minutes, 360) after the k-th attempt), not picked arbitrarily:
# summing the un-jittered base delays after attempts 1..10 --
# 2+4+8+16+32+64+128+256+360+360 = 1230 minutes (~20.5h) -- lands just
# under the existing 24h CHALLENGE_GC_AFTER bound, and attempt 11 would
# push cumulative time past it (1230+360=1590min, ~26.5h). N=10 is
# therefore the largest attempt count reachable under this backoff curve
# that still disables STRICTLY BEFORE the 24h mark the docs already
# promise, keeping "24h-bounded lifetime" true from actual attempt
# behavior rather than from `created_at` alone (which the starvation bug
# had decoupled from attempt behavior in the first place).
CHALLENGE_ATTEMPT_DISABLE_AFTER = 10

# Verify round-3 fix #2: duplicated BY VALUE from billcommons_api.routers.
# webhooks.MAX_ACTIVE_PER_HOST (this worker's container does not ship
# apps/api -- see the module docstring's note on COMMIT_SAFETY_LAG_SECONDS
# for the identical constraint) -- kept in sync by
# apps/api/tests/test_webhooks_payload_contract.py's own contract-test
# convention (verify round-4 fix #9: that contract test did not actually
# exist until this round -- see
# test_dispatcher_quota_constants_match_the_api_router_by_value). The
# per-domain quota must be re-checked HERE, at the moment a challenge
# succeeds, not just at creation time (round-2 fix #7 already fixed the
# creation-time check to count only verified subs) -- otherwise 11+ subs on
# one domain that were all under quota individually AT CREATION time
# (because none of the others had verified YET) could all go on to verify
# and blow past the cap.
MAX_ACTIVE_PER_HOST = 10

# Verify round-4 fix #1: same duplicated-BY-VALUE story as MAX_ACTIVE_PER_HOST
# above, this time for billcommons_api.routers.webhooks.MAX_ACTIVE_GLOBAL.
# _attempt_challenge's per-host recount (round-3 fix #2) had no GLOBAL
# counterpart -- an attacker spreading unverifiable-until-verified creations
# across 501+ distinct domains (each individually under the 10-per-host cap)
# could all go on to verify in the same tick and blow straight past the
# 500-verified-globally cap, which the creation-time check alone (round-3
# fix #1) cannot prevent for the identical "wasn't verified yet at creation
# time" reason MAX_ACTIVE_PER_HOST's own comment explains.
MAX_ACTIVE_GLOBAL = 500

# Verify round-5 fix #3 (codex HIGH, kimi #3, grok): _attempt_challenge's
# host+global recounts above (round-3 fix #2, round-4 fix #1) took no lock
# of their own -- a promotion racing an API-side reactivate/create (which
# DOES hold billcommons_api.routers.webhooks._QUOTA_LOCK_KEY for the exact
# same "count, then decide" window) could both read under-cap and both
# commit, oversubscribing the quota by however many promotions/creations/
# reactivations race in the same instant. Duplicated BY VALUE from that
# module -- same hash-namespaced-string convention ADVISORY_LOCK_KEY above
# already uses, same "kept in sync by a contract test" convention
# MAX_ACTIVE_PER_HOST/MAX_ACTIVE_GLOBAL use (round-4 fix #9's
# test_dispatcher_quota_constants_match_the_api_router_by_value; this fix's
# own counterpart is test_dispatcher_quota_lock_key_matches_the_api_router_
# by_value, same file). This worker's container does not ship apps/api (see
# MAX_ACTIVE_PER_HOST's own comment), so importing the router's constant
# directly is not an option.
_QUOTA_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"billcommons.webhooks.create_quota").digest()[:8], "big"
) & 0x7FFFFFFFFFFFFFFF

MAX_EVENTS_PER_BATCH = 100
MAX_BODY_BYTES = 512 * 1024
DELIVERY_CONCURRENCY = 8
DELIVERY_RETENTION = timedelta(days=30)

MAX_BACKOFF = timedelta(hours=6)
AUTO_DISABLE_AFTER = timedelta(hours=72)
NON_RETRYABLE_4XX = frozenset({400, 401, 403, 404, 405, 406, 411, 413, 414, 415, 422})

# COMMIT_SAFETY_LAG_SECONDS: imported above, from billcommons_shared.watermark
# (this worker's image DOES ship packages/shared -- see
# infra/docker/Dockerfile.webhooks-worker -- unlike apps/api, which does not
# ship workers/; that boundary runs the other direction, enforced by
# test_container_import_boundary.py).

API_VERSION = "1"
USER_AGENT = "BillCommons-Webhooks/1.0"

VALID_EVENT_KINDS = {"created", "status", "actions", "sponsors", "text", "metadata", "votes"}


# ---------------------------------------------------------------------------
# Pure helpers -- signing, backoff math, payload shape. No DB, no network.
# ---------------------------------------------------------------------------


def sign_body(secret: str, timestamp: int, raw_body: bytes) -> str:
    """X-BillCommons-Signature value: sha256=HMAC_SHA256(secret,
    f"{timestamp}.{raw_body}")."""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def delivery_headers(
    *, secret: str, delivery_id: uuid.UUID, attempt: int, timestamp: int, raw_body: bytes
) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-BillCommons-Signature": sign_body(secret, timestamp, raw_body),
        "X-BillCommons-Timestamp": str(timestamp),
        "X-BillCommons-Delivery": str(delivery_id),
        "X-BillCommons-Delivery-Attempt": str(attempt),
    }


def classify_transport_error(exc: SafeHttpError) -> str:
    """Map a safe_http exception to the error-class taxonomy persisted to
    `last_error`/`webhook_deliveries.error`. safe_http's own `.error_class`
    already IS one of these strings -- this exists as the one place that
    contract is asserted, so a new safe_http exception type without a mapped
    class fails loudly here rather than persisting an unexpected string."""
    if exc.error_class not in {
        "timeout", "tls", "dns", "ssrf_rejected", "too_large", "connection", "transport",
    }:
        raise AssertionError(f"unmapped safe_http error_class: {exc.error_class!r}")
    return exc.error_class


def classify_http_status(status: int) -> tuple[str | None, bool]:
    """Returns (error_class, disable_immediately) for a completed response.
    error_class is None for 2xx (no failure at all). `disable_immediately`
    is true ONLY for 410 Gone (§4.6) -- every other failure goes through the
    consecutive-failures / failing_since machinery instead.

    429 gets its OWN error class, 'http_429' (round-2 fix #5), not
    'http_4xx' -- a rate-limit response must never count toward, nor
    (via `_record_failure`'s "reset on class change" rule) fail to reset,
    the hard-4xx 3-strike NON_RETRYABLE_4XX rule. An endpoint that
    interleaves 429s with genuine hard-4xx responses would otherwise let
    the 429s either silently pad the hard-4xx streak or silently interrupt
    a run of real hard-4xx failures and reset the counter for the wrong
    reason.

    Verify round-4 fix #7: a 4xx status NOT in NON_RETRYABLE_4XX (408
    Request Timeout, 425 Too Early, and anything else in the 4xx range this
    module has no dedicated branch for) gets its OWN class,
    'http_4xx_retryable', distinct from plain 'http_4xx' -- for the exact
    same "reset on class change" reason 429 already needed its own class.
    `_drain_one`'s routing already treats any status not in
    NON_RETRYABLE_4XX/429/410 as an ordinary backoff-and-72h-auto-disable
    failure (same as a 5xx), so behavior there is unchanged; what was wrong
    was `_record_failure`'s streak bookkeeping -- before this fix, a
    retryable 4xx and a genuine hard-4xx shared the SAME 'http_4xx' label,
    so e.g. two 408s (retryable, never meant to count toward the 3-strike
    rule) followed by one real 404 saw no class change across all three
    calls and disabled as `non_retryable_client_error` after only ONE actual
    hard-4xx response.
    """
    if 200 <= status < 300:
        return None, False
    if status == 410:
        return "http_4xx", True
    if status == 429:
        return "http_429", False
    if status in NON_RETRYABLE_4XX:
        return "http_4xx", False
    if 400 <= status < 500:
        return "http_4xx_retryable", False
    return "http_5xx", False


def backoff_delay(consecutive_failures: int, *, rng: random.Random | None = None) -> timedelta:
    """min(2^n minutes, 6h) with +-20% jitter (§4.6). `rng` is a test hook --
    production always uses the module-level `random` functions (default)."""
    rng = rng or random
    base_minutes = min(2**max(consecutive_failures, 0), MAX_BACKOFF.total_seconds() / 60)
    jitter = 1 + rng.uniform(-0.2, 0.2)
    seconds = max(0.0, base_minutes * 60 * jitter)
    return timedelta(seconds=min(seconds, MAX_BACKOFF.total_seconds()))


def retry_after_delay(raw_header: str | None) -> timedelta | None:
    """Honor a 429's Retry-After (seconds only -- HTTP-date is not handled,
    same simplification billcommons_api.rate_limit's own Retry-After emitter
    makes), capped at 6h (§4.6). None means "no valid Retry-After", and the
    caller falls back to the ordinary exponential backoff.

    Verify round-5 fix #7 (opus #4): a non-positive value ("0" or a negative
    integer) is treated as UNPARSEABLE, same as `None`, not as a real
    "retry immediately" instruction -- a `next_attempt_at` of exactly `now`
    (the old behavior: `max(seconds, 0)` let 0 and every negative value
    through as `timedelta(seconds=0)`) makes this sub immediately eligible
    again on the very next tick, forever, for as long as the receiver keeps
    sending a broken Retry-After -- hammered every ~120s for the whole 72h
    auto-disable window instead of backing off like any other 429.
    """
    if not raw_header:
        return None
    try:
        seconds = int(raw_header.strip())
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return timedelta(seconds=min(seconds, MAX_BACKOFF.total_seconds()))


def parse_topic_target(target: str) -> tuple[str, str | None]:
    """kind='topic' target is "{slug}" or "{slug}:{JUR}" -- see
    billcommons_api.routers.webhooks._validate_target's docstring for why the
    scope is folded into `target` instead of a second column."""
    if ":" in target:
        slug, jur = target.split(":", 1)
        return slug, jur or None
    return target, None


def _jurisdiction_exists(db: OrmSession, abbreviation: str) -> bool:
    """Verify round-6 fix #2's existence probe -- one row lookup, used by
    `scope_clause` below for both the kind='jurisdiction' target and a
    kind='topic' scope's optional jurisdiction filter."""
    return db.execute(
        select(Jurisdiction.id).where(Jurisdiction.abbreviation == abbreviation)
    ).scalar_one_or_none() is not None


def scope_clause(subscription: WebhookSubscription, db: OrmSession | None = None):
    """The WHERE-clause fragment on BillEvent.bill_id selecting this
    subscription's scope. Returns None for an unrecognized/stale target
    (deleted topic, e.g.) -- the caller (`_drain_one`, via `_fetch_batch`)
    treats None as "this subscription must be disabled, visibly, with
    disabled_reason='unknown_scope'" (verify round-5 fix #8), never as
    silent-forever-quiet the way send_alerts.py's own
    `SKIP {email} {target}: unknown topic` case is -- rather than crashing
    the whole tick on one bad row, but also never pretending nothing is
    wrong.

    `db` (verify round-6 fix #2, codex MED #3): optional so every existing
    pure-function test in this module's suite (`SimpleNamespace` subs, no
    DB at all -- see `test_scope_clause_unknown_topic_returns_none` and its
    neighbor) keeps working unchanged; `_fetch_batch` (the only production
    caller) always passes the real session. Without a real `db`, a
    kind='jurisdiction' target whose stored abbreviation resolves to NO
    Jurisdiction row (the jurisdiction was renamed/removed after this
    subscription was created -- `_validate_target` only checks existence
    ONCE, at creation time) built a real, syntactically valid SQL predicate
    that simply matches nothing -- indistinguishable at the SQL level from
    "no new events in this scope yet", which is a perfectly healthy,
    quiet-forever state. That is the exact "reported as nothing to report"
    failure mode round-5 fix #8 (this same function, for topic/bills) was
    already built to prevent; jurisdiction targets had no equivalent check.
    Same reasoning applies to a kind='topic' scope's OPTIONAL jurisdiction
    filter (the "{slug}:{JUR}" form) -- a topic subscription scoped to a
    jurisdiction that no longer resolves must disable too, not silently
    narrow to "nothing", exactly like the bare kind='jurisdiction' case.
    """
    if subscription.kind == "topic":
        slug, jur = parse_topic_target(subscription.target)
        topic = TOPICS.get(slug)
        if topic is None:
            return None
        clause = BillEvent.bill_id.in_(select(Bill.id).where(membership_clause(topic)))
        if jur:
            if db is not None and not _jurisdiction_exists(db, jur):
                return None
            clause = and_(
                clause,
                BillEvent.bill_id.in_(
                    select(Bill.id)
                    .join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id)
                    .where(Jurisdiction.abbreviation == jur)
                ),
            )
        return clause

    if subscription.kind == "jurisdiction":
        if db is not None and not _jurisdiction_exists(db, subscription.target):
            return None
        return BillEvent.bill_id.in_(
            select(Bill.id)
            .join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id)
            .where(Jurisdiction.abbreviation == subscription.target)
        )

    if subscription.kind == "bills":
        try:
            ids = [uuid.UUID(token) for token in subscription.target.split(",") if token]
        except ValueError:
            return None
        if not ids:
            return None
        return BillEvent.bill_id.in_(ids)

    return None


def kind_clause(subscription: WebhookSubscription):
    if not subscription.event_kinds:
        return None
    kinds = [k for k in subscription.event_kinds.split(",") if k]
    if not kinds:
        return None
    return BillEvent.kind.in_(kinds)


def serialize_bill(bill: Bill, jurisdiction_abbr: str | None, session_identifier: str | None) -> dict:
    """The `bill` object embedded in a delivery -- the SAME field set as
    billcommons_api.schemas.BillSummary (the shape /changes serves), built by
    value rather than imported: this worker's container does not ship
    apps/api. Kept in sync by
    apps/api/tests/test_webhooks_payload_contract.py, which diffs this
    function's output against a live BillSummary for the same row -- same
    "one deliberate duplicate + a contract test" pattern
    workers/alerts/send_alerts.py's topic_membership_sql already uses for the
    identical reason.
    """
    return {
        "id": str(bill.id),
        "jurisdiction_id": str(bill.jurisdiction_id),
        "session_id": str(bill.session_id),
        "jurisdiction_abbreviation": jurisdiction_abbr,
        "session_identifier": session_identifier,
        "chamber": bill.chamber,
        "identifier": bill.identifier,
        "identifier_norm": bill.identifier_norm,
        "title": bill.title,
        "short_title": bill.short_title,
        "bill_type": bill.bill_type,
        "status": bill.status,
        "status_date": bill.status_date.isoformat() if bill.status_date else None,
        "introduced_date": bill.introduced_date.isoformat() if bill.introduced_date else None,
        "latest_action_text": bill.latest_action_text,
        "latest_action_date": bill.latest_action_date.isoformat() if bill.latest_action_date else None,
        "source_url": bill.source_url,
        "updated_at": bill.updated_at.isoformat() if bill.updated_at else None,
    }


def build_delivery_payload(
    *,
    events: list[tuple[BillEvent, Bill, str | None, str | None]],
    watermark_seq: int,
    watermark_changed_at: datetime,
    has_more: bool,
    delivery_id: uuid.UUID,
) -> bytes:
    """Serialize the §5 payload contract. `events` is
    (BillEvent, Bill, jurisdiction_abbr, session_identifier) tuples, already
    ORDER BY seq. `id` on each event is the STABLE dedupe key the spec calls
    for -- see the DEVIATION note in this repo's task report: bill_events
    (migration 0005) has no uuid id column, only the bigint `seq` primary
    key, which is already unique/stable/monotonic and serves the identical
    purpose; adding a uuid column to a live append-only log three other
    readers (`/changes`, the Atom feeds, alerts) depend on unchanged was
    judged more invasive than reusing the natural key that already satisfies
    the functional requirement.
    """
    items = []
    for event, bill, jur_abbr, session_ident in events:
        items.append(
            {
                "id": str(event.seq),
                "cursor": encode_cursor(event.seq),
                "kind": event.kind,
                "detail": event.detail,
                "changed_at": event.changed_at.isoformat(),
                "bill": serialize_bill(bill, jur_abbr, session_ident),
            }
        )
    last_changed_at = events[-1][0].changed_at if events else watermark_changed_at
    lag_seconds = max(0.0, (watermark_changed_at - last_changed_at).total_seconds())
    body = {
        "api_version": API_VERSION,
        "events": items,
        "cursor": encode_cursor(events[-1][0].seq) if events else encode_cursor(watermark_seq),
        "has_more": has_more,
        "lag_seconds": lag_seconds,
        "delivery_id": str(delivery_id),
    }
    return json.dumps(body, separators=(",", ":")).encode()


def build_challenge_payload(token: str, subscription_id: uuid.UUID) -> bytes:
    return json.dumps(
        {"challenge": token, "subscription_id": str(subscription_id)}, separators=(",", ":")
    ).encode()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def compute_watermark(db: OrmSession) -> tuple[int, datetime]:
    """(max seq, now) among events old enough to be safely served -- the same
    safety-lag rule as /changes and send_alerts.py. `now` (not the newest
    event's own changed_at) is the reference point for lag_seconds: an idle
    corpus with no recent events should report lag near zero, not grow
    unbounded just because nothing happened."""
    cutoff = func.now() - timedelta(seconds=COMMIT_SAFETY_LAG_SECONDS)
    seq = db.execute(
        select(func.coalesce(func.max(BillEvent.seq), 0)).where(BillEvent.changed_at <= cutoff)
    ).scalar_one()
    now = db.execute(select(func.now())).scalar_one()
    return seq, now


def _labels_for(db: OrmSession, bill_ids: set[uuid.UUID]) -> dict:
    """jurisdiction abbreviation + session identifier per bill, one bulk pass
    -- same shape as billcommons_api.labels.attach_bill_labels, reimplemented
    locally rather than imported (apps/api boundary, see module docstring)."""
    if not bill_ids:
        return {}
    rows = db.execute(
        select(Bill.id, Jurisdiction.abbreviation, SessionModel.identifier)
        .join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id)
        .join(SessionModel, SessionModel.id == Bill.session_id)
        .where(Bill.id.in_(bill_ids))
    ).all()
    return {bill_id: (jur_abbr, sess_ident) for bill_id, jur_abbr, sess_ident in rows}


# ---------------------------------------------------------------------------
# Challenges
# ---------------------------------------------------------------------------


def gc_unverified_inactive(db: OrmSession, *, now: datetime) -> int:
    """Delete unverified, inactive subscriptions whose `disabled_at` is
    older than UNVERIFIED_INACTIVE_RETENTION_DAYS -- fix #1's retention
    half (see that constant's own comment for the leak this closes).

    Scoped to `verified=False` specifically, not every inactive row: a
    subscription that DID verify and later gets disabled (72h auto-disable,
    410 Gone, a hard-4xx streak) is deliberately NOT touched here --
    send_alerts.py's still-pending disable notice, and the subscriber's own
    record of having once verified and delivered, must survive that. Only a
    sub that NEVER verified and is now ALSO inactive (the quota-disable
    case fix #1 exists for, but this also sweeps up unknown_scope/
    non_retryable_client_error/'gone' disables that happened to land on a
    still-unverified row, e.g. via a race) is pure dead weight with nothing
    left worth preserving.

    `disabled_at is not null` in the WHERE clause is deliberate, not
    redundant with the age comparison: an inactive row with `disabled_at`
    somehow NULL (defensively, should not happen -- every disable path in
    this module sets it) is left alone rather than either always-deleted or
    never-deleted by an ambiguous NULL comparison.
    """
    cutoff = now - timedelta(days=UNVERIFIED_INACTIVE_RETENTION_DAYS)
    result = db.execute(
        delete(WebhookSubscription).where(
            WebhookSubscription.verified.is_(False),
            WebhookSubscription.active.is_(False),
            WebhookSubscription.disabled_at.is_not(None),
            WebhookSubscription.disabled_at < cutoff,
        )
    )
    db.commit()
    return result.rowcount or 0


def _challenge_attempted_at_column_exists(db: OrmSession) -> bool:
    """Probe-guarded exactly like `_notify_pending_supports_created_
    disabled`/`_creation_events_table_exists` below (same 0012/0013/0014
    information_schema-probe convention this module already established) --
    migration 0015's own probe. True once `challenge_attempted_at` exists on
    `webhook_subscriptions`; False on a database still on an earlier
    migration. This column is deliberately NOT mapped on
    `billcommons_schema.models.WebhookSubscription` (see that model's own
    comment: a mapped column, even `deferred=True`, is still included in
    every INSERT/UPDATE SQLAlchemy generates, which would break subscription
    creation site-wide before migration 0015 is ever applied) -- every read/
    write of it in this module goes through raw `text()` SQL instead, gated
    behind this probe. Called once per `run_challenges` call and threaded
    down into `_attempt_challenge`, same shape `run_tick`'s own once-per-
    tick 0013 probe already uses."""
    return db.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'webhook_subscriptions' "
            "AND column_name = 'challenge_attempted_at' "
            "AND table_schema = current_schema()"
        )
    ).scalar_one_or_none() is not None


def _challenge_order_by(column_available: bool):
    """The ORDER BY clause `run_challenges`' SELECT uses -- factored out as
    a pure, DB-independent function (same seam `rotation_order` already is
    for the delivery path) so the "rotate on least-recently-attempted, NULLS
    FIRST" choice is unit-testable by compiling the clause to SQL text,
    without a live DB or the migration 0015 column actually existing.
    `column_available=True` -> `challenge_attempted_at` ascending, NULLS
    FIRST, via raw `text()` (not an ORM column reference -- see
    `_challenge_attempted_at_column_exists`'s own comment for why this
    column is unmapped); `column_available=False` -> the OLD fixed
    `created_at` ordering, unchanged, for a database that has not yet run
    migration 0015."""
    if column_available:
        return text("challenge_attempted_at ASC NULLS FIRST")
    return WebhookSubscription.created_at


def _stamp_challenge_attempted_at(db: OrmSession, sub_id: uuid.UUID, *, now: datetime) -> None:
    """Raw `UPDATE` for the one column `WebhookSubscription` deliberately
    does not map (see `_challenge_attempted_at_column_exists`'s own
    comment) -- called by every `_attempt_challenge`/crash-recovery branch
    when `column_available` is True. A plain, single-column, single-row
    UPDATE; folded into the SAME transaction/commit every caller already
    performs for its own attempt-outcome write, so this never adds an extra
    round trip on its own."""
    db.execute(
        text("UPDATE webhook_subscriptions SET challenge_attempted_at = :now WHERE id = :id"),
        {"now": now, "id": sub_id},
    )


def run_challenges(db: OrmSession, http_client, *, now: datetime, deadline: float) -> int:
    """Send/re-send verification challenges for unverified, active,
    currently-due subs (gated on `next_attempt_at` exactly like deliveries --
    verify round-1 fix #1a: that column already exists and was simply unused
    pre-verification), up to MAX_CHALLENGES_PER_TICK. Bounded by `deadline`
    (a time.monotonic() value -- fix #1c), which the caller sets to
    min(CHALLENGE_BUDGET_SECONDS from now, the tick's own deadline) so a run
    of slow/hanging challenge targets can never eat more than its own slice
    of the tick. Returns the count processed.

    r11 fix #5 (opus C, HIGH): ordering is `challenge_attempted_at` NULLS
    FIRST (least-recently-attempted first -- migration 0015), not the prior
    fixed `ORDER BY created_at`, which starved every newer unverified sub
    behind a page of slow/stuck ones forever. Probe-guarded
    (`_challenge_attempted_at_column_exists`): before migration 0015 is
    applied, falls back to the OLD `created_at` ordering unchanged -- same
    graceful-degradation shape `_notify_pending_supports_created_disabled`/
    `_creation_events_table_exists` already use for their own pending
    migrations.

    Also runs `gc_unverified_inactive` (fix #1, round-6) once per call,
    unconditionally and BEFORE the budget-bounded loop below -- a single
    cheap DELETE, same "always runs, never budget-gated" treatment
    `prune_deliveries`/`prune_creation_events` already get in `run_tick`,
    just folded into this function specifically because it is retention
    policy for the exact same 'unverified subscription' rows this
    function's own CHALLENGE_GC_AFTER logic already GCs (see that
    constant's own comment for why quota-disabled rows fall OUTSIDE the
    SELECT just below and therefore outside that existing GC path).

    The initial SELECT is committed (closing its read transaction) before
    the loop starts making POSTs -- fix #3: a challenge's HTTP call must
    never happen inside the same transaction that read the row, the same
    read-commit-POST-write shape `_drain_one` already uses.
    """
    gc_unverified_inactive(db, now=now)

    column_available = _challenge_attempted_at_column_exists(db)
    stmt = select(WebhookSubscription).where(
        WebhookSubscription.verified.is_(False),
        WebhookSubscription.active.is_(True),
        (WebhookSubscription.next_attempt_at.is_(None)) | (WebhookSubscription.next_attempt_at <= now),
    ).order_by(_challenge_order_by(column_available))
    subs = db.execute(stmt.limit(MAX_CHALLENGES_PER_TICK)).scalars().all()
    db.commit()

    processed = 0
    for sub in subs:
        if time.monotonic() >= deadline:
            break
        processed += 1
        # Verify round-5 fix #1 (kimi HIGH #1): captured into a plain local
        # BEFORE entering the per-sub try, and the except-block below uses
        # ONLY this local, never `sub` itself. `db.rollback()` in that
        # except-block EXPIRES every ORM instance in the session
        # (`expire_on_commit=False` on this sessionmaker governs commit, not
        # rollback) -- touching an attribute on `sub` (e.g. `sub.id`) AFTER
        # that rollback, when `sub`'s row was concurrently DELETEd (e.g. via
        # `DELETE /api/v1/webhooks/{id}`) between the SELECT above and here,
        # triggers SQLAlchemy's expired-attribute refresh, which raises
        # ObjectDeletedError from INSIDE the recovery path itself -- killing
        # the whole tick with the exact exception this handler exists to
        # contain. `sub_id` is a plain UUID value, immune to session state.
        sub_id = sub.id
        try:
            # Round-2 fix #8: `created_at` is read INSIDE this try (not
            # before it, and not outside the per-sub try as it was) --
            # `sub` was loaded by the SELECT above, whose own transaction
            # was already committed (closing the read) before this loop
            # started making POSTs; a concurrent API DELETE of this exact
            # row between that commit and this access can make SQLAlchemy's
            # refresh of an expired attribute raise ObjectDeletedError,
            # which must be caught like any other per-sub failure, not
            # allowed to kill the whole tick.
            created_at = sub.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if now - created_at > CHALLENGE_GC_AFTER:
                # Never verified within 24h -- GC, per §4.2.
                db.delete(sub)
                db.commit()
                continue
            _attempt_challenge(db, http_client, sub, now=now, column_available=column_available)
        except ObjectDeletedError:
            # The row is simply gone (deleted concurrently, e.g. via
            # DELETE /api/v1/webhooks/{id}) -- nothing to update or GC;
            # skip it exactly as if it had never come up due this tick.
            db.rollback()
            continue
        except Exception:
            # Crash isolation (fix #2, re-verified round-2 fix #2, hardened
            # round-5 fix #1): one bad row/URL must never escape and crash
            # the tick. Unlike run_deliveries.worker()'s equivalent handler
            # (see its own comment for the bug that was there), THIS branch
            # already routed through the real challenge-failure policy --
            # challenge_attempts incremented and next_attempt_at backed off
            # via `_apply_challenge_backoff`, the same helper a normal
            # challenge HTTP failure uses -- so a crashed challenge attempt
            # was never silently retried on the bare tick cadence with no
            # backoff. (Challenges have no WebhookDelivery audit row on ANY
            # outcome, success or failure -- that table is delivery-only by
            # design, so there is none to add here either.)
            db.rollback()
            try:
                # Round-5 fix #1: the recovery path itself is wrapped in its
                # OWN try/except, log-and-continue -- `db.get` by primary
                # key is a fresh SELECT (never an attribute refresh on the
                # expired `sub`), so it cannot raise ObjectDeletedError, but
                # nothing downstream of it (the attribute writes, the
                # commit) is exempt from ITS own surprises either, and this
                # whole branch already exists specifically so that one bad
                # sub's recovery can never take the tick down with it.
                fresh = db.get(WebhookSubscription, sub_id)
                if fresh is not None:
                    fresh.challenge_attempts += 1
                    fresh.last_error = "internal"
                    if column_available:
                        _stamp_challenge_attempted_at(db, sub_id, now=now)
                    if not _maybe_disable_after_challenge_attempts(fresh, now=now):
                        _apply_challenge_backoff(fresh, now=now)
                    db.commit()
            except Exception as recovery_exc:  # noqa: BLE001 -- log-and-continue by design
                db.rollback()
                print(
                    f"challenge crash-recovery itself failed for sub_id={sub_id}: "
                    f"{recovery_exc.__class__.__name__}: {recovery_exc}",
                    file=sys.stderr,
                    flush=True,
                )
    return processed


def _apply_challenge_backoff(
    sub: WebhookSubscription, *, now: datetime, retry_after: timedelta | None = None
) -> None:
    """Same exponential-backoff math as a delivery failure (fix #1a) --
    `challenge_attempts` stands in for `consecutive_failures` since an
    unverified sub has no delivery failure streak of its own yet.

    `retry_after` (batch item 9, muse V): a challenge target's 429 response
    can carry the SAME Retry-After header a delivery target's can, and the
    delivery path (`_apply_backoff`) already honors it (capped at 6h, same
    `retry_after_delay` helper). The challenge path previously ignored it
    entirely and always used the exponential curve, which can retry SOONER
    than the receiver explicitly asked for. Defaults to None (ordinary
    exponential backoff), same "explicit signal wins, else compute" shape
    `_apply_backoff` already uses.
    """
    delay = retry_after if retry_after is not None else backoff_delay(sub.challenge_attempts)
    sub.next_attempt_at = now + delay


def _maybe_disable_after_challenge_attempts(sub: WebhookSubscription, *, now: datetime) -> bool:
    """r11 fix #5: once a sub has accumulated CHALLENGE_ATTEMPT_DISABLE_AFTER
    TOTAL failed challenge attempts (any failure class -- transport error,
    mismatch, or non-2xx), disable it outright (`active=False`,
    `disabled_reason='challenge_timeout'`) instead of scheduling yet another
    backoff, so `gc_unverified_inactive`'s existing GC path (which only
    reaps `active=False` rows) applies. See CHALLENGE_ATTEMPT_DISABLE_AFTER's
    own comment for why 10. Returns True when this call disabled the sub
    (the caller must skip applying an ordinary backoff in that case);
    caller is expected to have already incremented `sub.challenge_attempts`
    for THIS attempt before calling.

    Never verified, so `notify_pending` is set directly here (not via
    `_notify_disabled`) -- same reasoning the domain/global quota-disable
    branches in `_attempt_challenge` already apply: a sub reaching this
    branch has never verified, so it can never have an unsent 'created'
    notice to combine with.
    """
    if sub.challenge_attempts < CHALLENGE_ATTEMPT_DISABLE_AFTER:
        return False
    sub.active = False
    sub.disabled_reason = "challenge_timeout"
    sub.disabled_at = now
    sub.notify_pending = "disabled"
    sub.challenge_token = None
    sub.next_attempt_at = None
    return True


def _attempt_challenge(
    db: OrmSession, http_client, sub: WebhookSubscription, *, now: datetime, column_available: bool = False
) -> None:
    """`column_available` (r11 fix #5): threaded down from `run_challenges`'
    once-per-call `_challenge_attempted_at_column_exists` probe -- True
    means `challenge_attempted_at` is stamped (via `_stamp_challenge_
    attempted_at`'s raw SQL -- this column is deliberately NOT an ORM
    attribute, see `_challenge_attempted_at_column_exists`'s own comment)
    on every attempt below (success, transport failure, mismatch, HTTP
    failure) so the NEXT call's rotation ordering reflects this one; False
    (pre-migration-0015 DB) means it is never touched at all. Defaults to
    False so every existing caller/test in this module's suite not yet
    updated to pass it keeps working unchanged, same default-True/False
    convention `supports_created_disabled` already uses elsewhere in this
    file.

    r12 fix #2 (agy HIGH + grok 1 HIGH + opus 2 HIGH -- convergent, three
    legs independently): the stamp below commits in its OWN transaction,
    BEFORE the outbound POST -- never left open across it. A raw single-
    row UPDATE autobegins a transaction and holds that row's lock until
    SOMETHING commits or rolls back; leaving it open across `http_client.
    fetch` (up to TOTAL_BUDGET_SECONDS=15s in safe_http) held the lock for
    the whole outbound call, violating this module's own §4.5 invariant
    ("transactions never span HTTP") for the one write this function makes
    before the HTTP call instead of after it. Concrete harm, two-fold: (a)
    a concurrent `DELETE`/`POST .../reactivate` on the SAME row blocks for
    up to 15s behind that lock; (b) worse, an exception escaping the fetch
    (a genuine bug, not a `SafeHttpError` -- those are caught below) rolls
    back in `run_challenges`' crash handler, and an uncommitted stamp
    rolls back WITH it -- un-stamping the very attempt that just happened,
    so the next call's rotation (`_challenge_order_by`, NULLS FIRST on
    this column) treats it as never-attempted and gives it another turn
    ahead of subs that actually ARE overdue.
    """
    if column_available:
        _stamp_challenge_attempted_at(db, sub.id, now=now)
        db.commit()
    timestamp = int(time.time())
    delivery_id = uuid.uuid4()
    body = build_challenge_payload(sub.challenge_token, sub.id)
    headers = delivery_headers(
        secret=sub.signing_secret, delivery_id=delivery_id, attempt=sub.challenge_attempts + 1,
        timestamp=timestamp, raw_body=body,
    )
    try:
        response = http_client.fetch(sub.url, method="POST", body=body, headers=headers)
    except SafeHttpError as exc:
        sub.challenge_attempts += 1
        sub.last_error = classify_transport_error(exc)
        # Batch item 10 (opus P): the delivery path's own transport-error
        # branch (`_record_failure(sub, status=None, ...)`) always updates
        # `last_status` to None -- no HTTP response was ever received, so a
        # STALE status from an earlier, different attempt must not linger
        # on the row. This branch previously left `last_status` untouched.
        sub.last_status = None
        if not _maybe_disable_after_challenge_attempts(sub, now=now):
            _apply_challenge_backoff(sub, now=now)
        db.commit()
        return

    # Batch item 11 (deepseek O): a body-less response (`response.body is
    # None` -- an empty body from safe_http's own transport, distinct from
    # an empty-but-present `b""`) used to reach `.strip()` unconditionally
    # below and raise AttributeError, which this function has no try/except
    # of its own for -- `run_challenges`' per-sub `except Exception` catches
    # it, but that path is meant for genuine crashes, not an ordinary
    # not-accepted outcome. Guarded the same way a real body mismatch is
    # handled: treated as `challenge_mismatch`, never as an accepted
    # challenge (`None != sub.challenge_token.encode()` either way, but
    # `None.strip()` would crash before that comparison is ever reached).
    accepted = (
        200 <= response.status < 300
        and response.body is not None
        and response.body.strip() == sub.challenge_token.encode()
    )
    sub.challenge_attempts += 1
    sub.last_status = response.status
    if accepted:
        # Verify round-5 fix #3: hold the SAME advisory lock creation and
        # reactivation take (`_QUOTA_LOCK_KEY`, duplicated by value from
        # billcommons_api.routers.webhooks -- see this module's own copy's
        # comment) for the rest of THIS transaction, before either recount
        # below runs. `pg_advisory_xact_lock` is transaction-scoped and
        # released automatically at the `db.commit()` every branch below
        # reaches -- without it, this promotion's "count, then decide" pair
        # of queries could interleave with a concurrent API-side
        # create/reactivate's own identically-shaped pair (round-1 fix #16/
        # round-4 fix #2), both reading under-cap before either commits.
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _QUOTA_LOCK_KEY})

        # Verify round-3 fix #2: recount verified subs on this HOST before
        # promoting -- see MAX_ACTIVE_PER_HOST's own comment above for why
        # the creation-time check alone is not sufficient. `sub` itself is
        # NOT yet verified in the DB at this point (this branch is what
        # would flip it), so the count below is exactly "how many OTHER
        # subs already hold a seat", with no self-exclusion needed.
        verified_on_host = db.execute(
            select(func.count()).select_from(WebhookSubscription).where(
                WebhookSubscription.host == sub.host,
                WebhookSubscription.active.is_(True),
                WebhookSubscription.verified.is_(True),
            )
        ).scalar_one()
        if verified_on_host >= MAX_ACTIVE_PER_HOST:
            # Honest, visible outcome (not silent GC): the subscriber sees
            # WHY nothing is being delivered via GET /webhooks/{id}, rather
            # than the subscription quietly never verifying and eventually
            # 24h-GC'd with no explanation.
            sub.active = False
            sub.disabled_reason = "domain_quota_exceeded"
            sub.disabled_at = now
            sub.notify_pending = "disabled"
            sub.challenge_token = None
            # Batch item 7 (grok G): cleared like the promote path just
            # below -- this branch is reached from an ACCEPTED challenge
            # response (`accepted` is True to get here at all), so any
            # `last_error` still on the row is a stale value from an
            # EARLIER, unrelated failed attempt. Leaving it set makes
            # `GET /webhooks/{id}` show a misleading transport/HTTP error
            # class for what is actually a quota disable, not a delivery
            # problem at all.
            sub.last_error = None
            sub.next_attempt_at = None
            db.commit()
            return

        # Verify round-4 fix #1: mirror round-3 fix #2's per-host recount
        # with a GLOBAL one -- billcommons_api.routers.webhooks.
        # MAX_ACTIVE_GLOBAL's own creation-time check (round-3 fix #1) only
        # counts ALREADY-verified subs, for the identical reason the
        # per-host creation-time check does (an unverifiable sub must never
        # hold a quota seat forever) -- which means it cannot stop 501+ subs
        # that were all under the global cap individually AT CREATION time
        # from all going on to verify in the same window and blowing past
        # it. `sub` itself is NOT yet verified in the DB at this point
        # (same self-exclusion reasoning as the per-host count above), so no
        # self-exclusion is needed here either.
        verified_globally = db.execute(
            select(func.count()).select_from(WebhookSubscription).where(
                WebhookSubscription.active.is_(True),
                WebhookSubscription.verified.is_(True),
            )
        ).scalar_one()
        if verified_globally >= MAX_ACTIVE_GLOBAL:
            sub.active = False
            sub.disabled_reason = "global_quota_exceeded"
            sub.disabled_at = now
            sub.notify_pending = "disabled"
            sub.challenge_token = None
            # Batch item 7 (grok G): same reasoning as the per-host branch
            # just above -- cleared like the promote path.
            sub.last_error = None
            sub.next_attempt_at = None
            db.commit()
            return
        sub.verified = True
        sub.challenge_token = None
        sub.notify_pending = "created"
        sub.last_error = None
        sub.next_attempt_at = None
    elif 200 <= response.status < 300:
        # Round-2 fix #11: a 2xx whose body just doesn't match the token is
        # not an HTTP failure at all -- `classify_http_status` would map it
        # to `(None, False)` (no failure), and the old fallback
        # `error_class or "http_4xx"` mislabeled it as a hard-4xx, which is
        # actively misleading (it looks like the endpoint rejected the
        # request, when really it accepted the request and just echoed the
        # wrong body). Distinct, dedicated label.
        sub.last_error = "challenge_mismatch"
        if not _maybe_disable_after_challenge_attempts(sub, now=now):
            _apply_challenge_backoff(sub, now=now)
    else:
        error_class, disable_now = classify_http_status(response.status)
        sub.last_error = error_class or "http_4xx"
        # Verify round-7 fix #4 (codex MED #2): a 410 Gone means the
        # endpoint itself told us to stop, permanently -- routing it through
        # the generic backoff below (as every other non-2xx status was)
        # discarded `classify_http_status`'s own `disable_immediately`
        # signal for exactly this case, so a permanently-Gone challenge
        # target kept getting POSTed until the 24h challenge GC caught up,
        # holding a MAX_UNVERIFIED_PER_HOST slot the whole time. Same
        # disable shape the delivery path (`_drain_one`) already gives a
        # 410: immediate `active=False`, a dedicated reason, and a pending
        # notice -- `notify_pending` set directly (not via `_notify_
        # disabled`) because a sub reaching this branch has NEVER verified,
        # so it can never have an unsent 'created' notice to combine with
        # (the same reasoning the domain/global quota-disable branches
        # above already apply).
        if disable_now:
            sub.active = False
            sub.disabled_reason = "endpoint_gone"
            sub.disabled_at = now
            sub.notify_pending = "disabled"
            sub.challenge_token = None
            sub.next_attempt_at = None
        elif not _maybe_disable_after_challenge_attempts(sub, now=now):
            # Batch item 9 (muse V): a 429 challenge response honors
            # Retry-After exactly like a 429 delivery response already does
            # (`_drain_one`'s own `response.status == 429` branch) -- same
            # `retry_after_delay` helper, same 6h cap.
            retry_after = (
                retry_after_delay(response.headers.get("retry-after"))
                if response.status == 429
                else None
            )
            _apply_challenge_backoff(sub, now=now, retry_after=retry_after)
    db.commit()


# ---------------------------------------------------------------------------
# Deliveries
# ---------------------------------------------------------------------------


@dataclass
class TickStats:
    subs_processed: int = 0
    events_sent: int = 0
    failures: int = 0


def rotation_order(subs: list, *, limit: int) -> list:
    """last_attempt_at NULLS FIRST, capped at `limit` -- pure/DB-independent
    so the "a 51st subscriber can't starve forever" guarantee is unit
    testable against plain fakes (see
    workers/webhooks/tests/test_dispatch_webhooks.py's rotation test)
    without a live DB. Mirrors the SQL ORDER BY exactly: a sub that has
    never been attempted (`last_attempt_at is None`) always sorts before
    one that has, and within each group, oldest first.

    Applied in Python (not left to Postgres alone) after the WHERE-only
    query in `eligible_deliveries` -- fine at this scale, since
    MAX_ACTIVE_GLOBAL (500, in billcommons_api.routers.webhooks) already
    bounds how many rows can ever be "due" at once.
    """
    ordered = sorted(subs, key=lambda s: (s.last_attempt_at is not None, s.last_attempt_at))
    return ordered[:limit]


def eligible_deliveries(db: OrmSession, *, now: datetime, limit: int = 50) -> list[WebhookSubscription]:
    """active AND verified AND due, ordered last_attempt_at NULLS FIRST --
    under any STABLE ordering a 51st subscriber would starve forever once
    there are more due subs than one tick's concurrency can drain; rotating
    on "least recently attempted" guarantees everyone gets a turn."""
    candidates = db.execute(
        select(WebhookSubscription).where(
            WebhookSubscription.active.is_(True),
            WebhookSubscription.verified.is_(True),
            (WebhookSubscription.next_attempt_at.is_(None))
            | (WebhookSubscription.next_attempt_at <= now),
        )
    ).scalars().all()
    return rotation_order(candidates, limit=limit)


def _record_failure(sub: WebhookSubscription, *, status: int | None, error_class: str | None, now: datetime) -> None:
    """Records one failed attempt. `consecutive_failures` resets to 0 first
    when this failure's class differs from the previous one (fix #7): the
    3-consecutive-hard-4xx-disable rule must count only a streak of the SAME
    failure class, not e.g. a timeout followed by two unrelated 4xxs.
    `failing_since` is untouched by the reset -- the 72h auto-disable clock
    is deliberately independent of the counter (§4.6)."""
    if sub.last_error is not None and error_class != sub.last_error:
        sub.consecutive_failures = 0
    sub.last_attempt_at = now
    sub.last_status = status
    sub.last_error = error_class
    sub.consecutive_failures += 1
    if sub.failing_since is None:
        sub.failing_since = now


#: r12 fix #3(b) (grok 2 MED + codex MED + opus 3 MED -- convergent, three
#: angles): a skipped oversized event is not evidence about receiver
#: health -- the receiver was never even contacted (`_record_skip`, unlike
#: `_record_failure`, deliberately never arms `failing_since`; see that
#: function's own comment) -- so it needs a disable path of its own,
#: independent of the 72h wall clock `_maybe_auto_disable` reads.
#: `consecutive_failures` already gives it one for free: `_record_skip`
#: keeps the same reset-on-class-change rule `_record_failure` uses, so a
#: run of skips uninterrupted by any OTHER failure class already IS a
#: clean, isolated streak. Sized to match the SAME 72h off-ramp
#: docs/api/webhooks.md already promises every other perpetual-failure
#: class (AUTO_DISABLE_AFTER): 72h * 3600s / TICK_SECONDS(120s) = 2160,
#: i.e. the worst case of one FRESH oversized event surfacing every
#: single tick reaches this streak in the same 72h a real delivery
#: failure would auto-disable in; a sparser stream of oversized events
#: (the common case) reaches it proportionally slower, never sooner --
#: still bounded, never "forever".
OVERSIZED_SKIP_DISABLE_AFTER = 2160


def _record_skip(sub: WebhookSubscription, *, error_class: str, now: datetime) -> None:
    """Like `_record_failure`, minus arming `failing_since`. A skip means
    the receiver was never contacted at all -- it is not a signal about
    whether the receiver is healthy, and must not start (or otherwise
    touch) the SAME wall clock a real delivery failure's `failing_since`
    drives (`_maybe_auto_disable`'s 72h check). Concretely: without this
    split, a narrow subscription that skips ONE oversized event (arming
    `failing_since`) and then sees a single, ordinary, transient failure
    days later gets treated as though it had been failing continuously
    the WHOLE time in between -- instantly auto-disabled off a single
    real failure it otherwise would have shrugged off.
    `consecutive_failures` bookkeeping (reset-on-class-change, then
    increment) is untouched -- see OVERSIZED_SKIP_DISABLE_AFTER's own
    comment for why this failure class disables off THAT streak instead.

    r13 fix #4 (grok MED 2): also CLEARS an already-armed `failing_since`.
    A skip is not evidence the receiver is still failing -- the receiver
    was never contacted -- so a long skip run must not let an OLDER armed
    `failing_since` (from a real failure well before the skip run started)
    keep accumulating wall-clock time it never actually earned: fail@T0
    arms `failing_since`; a skip run spans past the 72h `AUTO_DISABLE_AFTER`
    window; one late, ordinary 5xx at T0+80h would otherwise trip the 72h
    disable off a clock that was armed 80h ago even though the receiver was
    never even contacted for nearly all of that span. Clearing it here
    means the next REAL failure (`_record_failure`'s own `if
    sub.failing_since is None` re-arm) restarts the 72h clock from that
    failure's own timestamp -- exactly the receiver-health-only semantics
    `failing_since` is meant to have.
    """
    if sub.last_error is not None and error_class != sub.last_error:
        sub.consecutive_failures = 0
    sub.last_attempt_at = now
    sub.last_status = None
    sub.last_error = error_class
    sub.consecutive_failures += 1
    sub.failing_since = None


def _record_success(sub: WebhookSubscription, *, status: int, now: datetime) -> None:
    sub.last_attempt_at = now
    sub.last_success_at = now
    sub.last_status = status
    sub.last_error = None
    sub.consecutive_failures = 0
    sub.failing_since = None
    sub.next_attempt_at = None


def _delivery_attempt(sub: WebhookSubscription) -> int:
    """The `X-BillCommons-Delivery-Attempt` ordinal for the NEXT real
    (receiver-facing) delivery -- see fix #9 (this file's docstring) for
    why it is `consecutive_failures + 1` rather than a hardcoded 1.

    r13 fix #5 (deepseek LOW 3): a run of oversized-skip events (never
    receiver-facing -- see `_record_skip`) still increments
    `consecutive_failures`, the SAME counter this header is derived from --
    without this guard, a receiver that has never once been contacted could
    see its first REAL delivery arrive stamped e.g. attempt=6, wrongly
    implying five prior receiver-facing tries it never got. No new column
    (no migrations this round): `sub.last_error == 'payload_too_large'`
    (the skip error class -- the same discriminator `_record_skip`/
    `_record_failure`'s own reset-on-class-change logic already uses) means
    the CURRENT streak is entirely skip-class, so the next real attempt
    starts the header fresh at 1 instead of inheriting that streak's
    length. A streak that MIXES skips with real receiver-facing failures
    ends in whichever class attempted last (the reset-on-class-change rule
    already collapses `consecutive_failures` to just that trailing streak),
    so this same one-column check stays correct for the mixed case too.
    """
    if sub.last_error == "payload_too_large":
        return 1
    return sub.consecutive_failures + 1


def _apply_backoff(sub: WebhookSubscription, *, now: datetime, retry_after: timedelta | None) -> None:
    delay = retry_after if retry_after is not None else backoff_delay(sub.consecutive_failures)
    sub.next_attempt_at = now + delay


def _notify_disabled(sub: WebhookSubscription, supports_created_disabled: bool = True) -> None:
    """Sets `notify_pending` for an auto-disable event -- verify round-3
    fix #13. A plain `sub.notify_pending = "disabled"` would silently
    overwrite an unsent 'created' notice if the subscription verified
    earlier in the SAME nightly window and then failed hard enough to get
    auto-disabled before send_alerts.py's next run ever drained it -- the
    owner would only ever hear "auto-disabled", never learning it had
    verified and started delivering at all. `notify_pending` is a single-
    slot column (one pending notice at a time), so the two facts are
    combined into one sentinel, 'created_disabled', that
    workers/alerts/send_alerts.py's `render_webhook_notice` renders as one
    combined message -- ordering preserved honestly, nothing silently
    dropped.

    Verify round-5 fix #10 (deepseek #3): mapped BOTH 'created' AND
    'created_disabled' -> 'created_disabled', not just 'created' -- a
    SECOND disable cycle (reactivate, then auto-disable again before
    send_alerts.py's next nightly run ever drains the still-pending
    'created_disabled' notice from the first cycle) previously fell
    through to the plain `else: "disabled"` branch, overwriting and
    losing the fact that this subscription ever verified at all.

    Verify round-6 fix #4 (opus MED #3): `supports_created_disabled`
    defaults to True (the pre-0013 caller shape -- every caller not yet
    updated to thread the per-tick probe through, including every existing
    pure-function test in this module's test suite, keeps writing
    'created_disabled' exactly as before). `run_tick` probes migration
    0013's presence ONCE per tick (see `_notify_pending_supports_created_
    disabled`) and threads the real answer down through `run_deliveries` ->
    `worker` -> `_drain_one`/`_maybe_auto_disable` -> here. When it is
    False, writing 'created_disabled' would raise IntegrityError at COMMIT
    against `ck_webhook_notify_pending`'s pre-0013 definition -- the crash
    handler around every caller of this function then rolls back the
    ENTIRE disable (active/disabled_reason/disabled_at discarded with it),
    leaving a dead endpoint that retries forever and can never actually be
    disabled. Falling back to plain 'disabled' (0012-legal) sacrifices only
    the "verified earlier, then disabled" combined notice, not the disable
    itself.
    """
    if sub.notify_pending in ("created", "created_disabled"):
        if supports_created_disabled:
            sub.notify_pending = "created_disabled"
            return
        print(
            "migration 0013 not applied -- falling back to plain 'disabled' "
            "notify_pending (losing the unsent 'created' notice combination) "
            f"for sub_id={sub.id}",
            file=sys.stderr,
            flush=True,
        )
    sub.notify_pending = "disabled"


def _maybe_auto_disable(
    sub: WebhookSubscription, *, now: datetime, reason: str, supports_created_disabled: bool = True
) -> bool:
    if sub.failing_since is not None and now - sub.failing_since > AUTO_DISABLE_AFTER:
        sub.active = False
        sub.disabled_reason = reason
        sub.disabled_at = now
        _notify_disabled(sub, supports_created_disabled)
        return True
    return False


def _advance_last_seq(db: OrmSession, sub: WebhookSubscription, new_value: int) -> None:
    """Advance `sub.last_seq` to `max(current, new_value)` via a GREATEST SQL
    UPDATE -- verify round-3 fix #11. A plain `sub.last_seq = new_value`
    assignment here can drag the cursor BACKWARD relative to a concurrent
    `POST /webhooks/{id}/reactivate?mode=skip` (which fast-forwards
    `last_seq` to a NEWER watermark, on a different session, possibly mid-
    tick) racing this in-flight drain -- whichever write lands last would
    otherwise win outright, and "last write wins" can mean "the OLDER,
    smaller value wins" depending purely on timing. `GREATEST` makes the
    write commutative regardless of ordering.

    `db.refresh(sub, attribute_names=["last_seq"])` afterward, NOT a plain
    attribute assignment from a locally-computed value -- so any LATER read
    of `sub.last_seq` within this same session (e.g. `_fetch_batch`'s next
    call in `_drain_one`'s loop) sees the authoritative post-GREATEST value
    from the database instead of whatever this process itself guessed the
    outcome was. Scoped to ONLY `last_seq` so it does not clobber any other
    attribute this call's caller already set on `sub` but has not yet
    committed (`autoflush=False` on the shared sessionmaker -- see
    billcommons_shared.db -- means those pending writes are not visible to
    `refresh`'s own SELECT either way, and are unaffected by it).
    """
    db.execute(
        update(WebhookSubscription)
        .where(WebhookSubscription.id == sub.id)
        .values(last_seq=func.greatest(WebhookSubscription.last_seq, new_value))
    )
    db.refresh(sub, attribute_names=["last_seq"])


def _fetch_batch(
    db: OrmSession, sub: WebhookSubscription, *, watermark_seq: int
) -> tuple[list, bool, int | None] | None:
    """One DB round-trip: up to MAX_EVENTS_PER_BATCH+1 (BillEvent, Bill,
    jurisdiction_abbr, session_identifier) tuples for `sub`'s scope, seq
    strictly after its current last_seq and at or before the watermark.
    Returns (events, has_more, last_raw_seq), or None if `sub`'s scope is
    unrecognized/stale (see `scope_clause`) -- the caller (`_drain_one`)
    disables the subscription with disabled_reason='unknown_scope' on None
    (verify round-5 fix #8), rather than quiet-running forever.

    `last_raw_seq` is the seq of the last RAW row this call fetched (before
    the `row.bill_id in bills` join-filter below can drop any), or None when
    the raw fetch itself returned zero rows. Fix #5 (filtered-batch
    at-most-once bug): the join-filter below is the one place a non-empty
    raw fetch can still yield an empty `events` list -- `_drain_one` needs
    `last_raw_seq` to distinguish that case (advance to the last RAW row and
    keep draining -- there may be more within the window) from a genuinely
    empty raw fetch (advance straight to the watermark; nothing more to see,
    ever, in this window).

    Isolated from `_drain_one`'s control flow (POST once per batch, record
    the outcome, loop until `has_more` is false or the deadline) specifically
    so that control flow is unit-testable against a fake in-memory event
    source with no DB at all -- see `_drain_one`'s `fetch_batch` parameter
    and workers/webhooks/tests/test_dispatch_webhooks.py's truncation/drain
    test.
    """
    clause = scope_clause(sub, db)
    if clause is None:
        return None

    kclause = kind_clause(sub)
    stmt = select(BillEvent).where(
        BillEvent.seq > sub.last_seq, BillEvent.seq <= watermark_seq, clause
    )
    if kclause is not None:
        stmt = stmt.where(kclause)
    rows = db.execute(
        stmt.order_by(BillEvent.seq).limit(MAX_EVENTS_PER_BATCH + 1)
    ).scalars().all()
    has_more = len(rows) > MAX_EVENTS_PER_BATCH
    rows = rows[:MAX_EVENTS_PER_BATCH]
    if not rows:
        return [], False, None

    last_raw_seq = rows[-1].seq
    bill_ids = {r.bill_id for r in rows}
    bills = {b.id: b for b in db.execute(select(Bill).where(Bill.id.in_(bill_ids))).scalars()}
    labels = _labels_for(db, bill_ids)
    events = [
        (row, bills[row.bill_id], *labels.get(row.bill_id, (None, None)))
        for row in rows
        if row.bill_id in bills
    ]
    return events, has_more, last_raw_seq


def _drain_one(
    db: OrmSession,
    http_client,
    sub: WebhookSubscription,
    *,
    watermark_seq: int,
    watermark_dt: datetime,
    deadline: float,
    fetch_batch=_fetch_batch,
    advance_last_seq=_advance_last_seq,
    supports_created_disabled: bool = True,
) -> tuple[int, bool]:
    """Drain `sub` from its last_seq up to `watermark_seq`, bounded by
    `deadline` (a time.monotonic() value). Returns (events_sent, failed).

    Read, close/commit; POST; short write transaction -- never one
    transaction spanning the HTTP call (§4.5/module docstring).

    `fetch_batch` is a test hook ONLY -- production code never overrides it;
    it defaults to `_fetch_batch` (the real DB query). Tests substitute an
    in-memory fake to exercise this loop's batching/has_more/cursor-advance
    logic without a live DB, the same injectable-seam pattern
    billcommons_shared.safe_http's `resolver` uses.

    `advance_last_seq` is the SAME kind of seam, for the SAME reason: it
    defaults to `_advance_last_seq` (the real GREATEST SQL write, fix #11),
    which needs a real DB connection, so pure-logic tests exercising this
    loop's control flow against `fetch_batch` fakes inject a plain-Python
    stand-in instead.

    `supports_created_disabled` (fix #4, round-6) is threaded straight
    through to every `_notify_disabled`/`_maybe_auto_disable` call this
    function makes -- see `_notify_pending_supports_created_disabled`'s own
    docstring for the once-per-tick probe this is the result of.
    """
    events_sent = 0
    while time.monotonic() < deadline:
        now = datetime.now(timezone.utc)
        fetched = fetch_batch(db, sub, watermark_seq=watermark_seq)
        if fetched is None:
            # Verify round-5 fix #8 (opus #5): an unrecognized/stale target
            # (a retired topic slug, an unparseable bill-id list) used to
            # quiet-run FOREVER -- same as a real empty match, advancing the
            # cursor with `last_error` left `null`, looking exactly as
            # healthy as a subscription with nothing new to report. That is
            # the precise "reported as nothing to report" worst case this
            # codebase's own docstrings call out elsewhere: the subscriber
            # never gets ANY signal that their scope is now unrecognized.
            # Fix: disable outright, with a dedicated reason, through the
            # exact same disable/notify machinery every other disable path
            # uses -- `GET /webhooks/{id}` now tells the subscriber WHY
            # nothing is being delivered instead of leaving them to guess.
            # The cursor is deliberately NOT advanced (the row is disabled,
            # not draining) -- `reactivate` after fixing the target/topic
            # picks up wherever it left off, per the usual resume/skip modes.
            sub.active = False
            sub.disabled_reason = "unknown_scope"
            sub.disabled_at = now
            sub.last_attempt_at = now
            _notify_disabled(sub, supports_created_disabled)
            db.commit()
            return events_sent, True

        events, has_more, last_raw_seq = fetched
        db.commit()  # close the read before the POST -- see module docstring

        if not events:
            if last_raw_seq is not None:
                # Filtered-batch bug (fix #5): raw rows existed in this
                # window but the join-filter in `_fetch_batch` dropped every
                # one of them. Advancing straight to the watermark here
                # would silently skip whatever comes AFTER this batch but
                # still within the window -- an at-most-once bug. Advance
                # only to the last RAW row's seq and keep draining.
                advance_last_seq(db, sub, last_raw_seq)
                sub.last_attempt_at = now
                db.commit()
                if has_more:
                    continue
                return events_sent, False
            # send_alerts.py's quiet-run rule: the raw fetch itself was
            # empty (nothing in the window at all) -- without this a narrow
            # subscription re-scans a forever-growing window every tick
            # until statement_timeout kills it, silently. Stamp
            # last_attempt_at same as a real attempt (fix #4).
            advance_last_seq(db, sub, watermark_seq)
            sub.last_attempt_at = now
            db.commit()
            return events_sent, False

        rows = [event for event, *_ in events]
        delivery_id = uuid.uuid4()
        timestamp = int(time.time())
        body = build_delivery_payload(
            events=events, watermark_seq=watermark_seq, watermark_changed_at=watermark_dt,
            has_more=has_more, delivery_id=delivery_id,
        )
        while len(body) > MAX_BODY_BYTES and len(events) > 1:
            # Split the batch in half and retry with the smaller slice --
            # simplest correct response to "serialized body over cap".
            # Looped, not a single halving (fix #8): one halving is not
            # enough when a batch of very large bills is still over cap at
            # half size -- looping down to length 1 is the only way to
            # guarantee the POSTed body is never over cap (a single event's
            # serialized body is assumed to fit; if it somehow doesn't, the
            # loop bottoms out at length 1 and the check just below this
            # loop -- round-5 fix #6 -- skips that one event with an audit
            # row instead of ever POSTing an over-cap batch that would draw
            # a receiver 413 and wrongly count toward the hard-4xx disable
            # strike).
            half = len(events) // 2 or 1
            events = events[:half]
            rows = rows[:half]
            has_more = True
            body = build_delivery_payload(
                events=events, watermark_seq=watermark_seq, watermark_changed_at=watermark_dt,
                has_more=has_more, delivery_id=delivery_id,
            )

        if len(events) == 1 and len(body) > MAX_BODY_BYTES:
            # Verify round-5 fix #6 (codex MED): a single event whose own
            # serialized body still exceeds the cap after halving as far as
            # it can (the halving loop above's own comment used to call
            # this "delivered as-is, oversized" -- it is not: POSTing an
            # over-cap body draws a receiver 413, which the hard-4xx
            # 3-strike rule then disables on, and a subsequent
            # `reactivate?mode=resume` just retries this SAME stuck event
            # forever, permanently wedged). Skip it instead: write a
            # WebhookDelivery audit row (error class 'payload_too_large',
            # no status/duration -- no HTTP attempt was ever made), advance
            # the cursor PAST this one event (GREATEST shape, same as any
            # other cursor write), stamp `last_error`, and keep draining --
            # the /changes pull API is the completeness escape hatch for a
            # skipped event (documented in docs/api/webhooks.md).
            advance_last_seq(db, sub, rows[-1].seq)
            # r12 fix #3 (grok 2 MED + codex MED + opus 3 MED -- convergent,
            # three angles): `_record_skip`, not `_record_failure` -- a
            # skip never contacted the receiver at all, so it must not arm
            # `failing_since` (see `_record_skip`'s own comment for the
            # insta-disable scenario that otherwise creates). It still
            # needs to eventually disable a subscription whose SCOPE keeps
            # generating fresh oversized events forever (grok/codex: the
            # stated 72h off-ramp was unreachable because nothing here
            # called `_maybe_auto_disable` at all) -- done via THIS class's
            # own `consecutive_failures` streak instead of the wall clock;
            # see OVERSIZED_SKIP_DISABLE_AFTER's own comment for the bound
            # and its sizing.
            _record_skip(sub, error_class="payload_too_large", now=now)
            disabled = sub.consecutive_failures >= OVERSIZED_SKIP_DISABLE_AFTER
            if disabled:
                sub.active = False
                sub.disabled_reason = "payload_too_large"
                sub.disabled_at = now
                _notify_disabled(sub, supports_created_disabled)
            db.add(WebhookDelivery(
                subscription_id=sub.id, delivery_id=delivery_id,
                first_seq=rows[0].seq, last_seq=rows[-1].seq, event_count=len(events),
                status=None, error="payload_too_large", duration_ms=None,
            ))
            db.commit()
            if disabled:
                return events_sent, True
            if has_more:
                continue
            return events_sent, False

        # Attempt ordinal within the CURRENT failing streak (fix #9), not a
        # hardcoded 1 -- a receiver logging X-BillCommons-Delivery-Attempt
        # can otherwise never tell a first try from a fifth retry. Documented
        # in docs/api/webhooks.md. `_delivery_attempt` (r13 fix #5), not a
        # bare `sub.consecutive_failures + 1` -- see that function's own
        # comment for why a preceding oversized-skip streak must not
        # inflate this header.
        headers = delivery_headers(
            secret=sub.signing_secret, delivery_id=delivery_id, attempt=_delivery_attempt(sub),
            timestamp=timestamp, raw_body=body,
        )
        start = time.monotonic()
        now = datetime.now(timezone.utc)
        try:
            response = http_client.fetch(sub.url, method="POST", body=body, headers=headers, require_body=False)
        except SafeHttpError as exc:
            error_class = classify_transport_error(exc)
            _record_failure(sub, status=None, error_class=error_class, now=now)
            _apply_backoff(sub, now=now, retry_after=None)
            _maybe_auto_disable(sub, now=now, reason="too_many_failures", supports_created_disabled=supports_created_disabled)
            db.add(WebhookDelivery(
                subscription_id=sub.id, delivery_id=delivery_id,
                first_seq=rows[0].seq, last_seq=rows[-1].seq, event_count=len(events),
                status=None, error=error_class, duration_ms=int((time.monotonic() - start) * 1000),
            ))
            db.commit()
            return events_sent, True

        duration_ms = int((time.monotonic() - start) * 1000)
        error_class, disable_now = classify_http_status(response.status)
        if error_class is None:
            _record_success(sub, status=response.status, now=now)
            advance_last_seq(db, sub, rows[-1].seq)
            db.add(WebhookDelivery(
                subscription_id=sub.id, delivery_id=delivery_id,
                first_seq=rows[0].seq, last_seq=rows[-1].seq, event_count=len(events),
                status=response.status, error=None, duration_ms=duration_ms,
            ))
            db.commit()
            events_sent += len(events)
            if not has_more:
                return events_sent, False
            continue

        # Failure. The cursor does NOT advance -- next tick retries the same
        # window (at-least-once, per §7's crash-between-POST-and-advance
        # test).
        _record_failure(sub, status=response.status, error_class=error_class, now=now)
        if disable_now:
            sub.active = False
            sub.disabled_reason = "gone"
            sub.disabled_at = now
            _notify_disabled(sub, supports_created_disabled)  # fix #13 -- preserve an unsent 'created' notice
        elif response.status == 429:
            _apply_backoff(sub, now=now, retry_after=retry_after_delay(response.headers.get("retry-after")))
            # 429 must also be subject to the 72h wall-clock auto-disable
            # (fix #6) -- previously only the 5xx/timeout/tls/dns branch
            # called this, so a permanently-429ing endpoint retried forever.
            _maybe_auto_disable(sub, now=now, reason="too_many_failures", supports_created_disabled=supports_created_disabled)
        elif response.status in NON_RETRYABLE_4XX:
            if sub.consecutive_failures >= 3:
                sub.active = False
                sub.disabled_reason = "non_retryable_client_error"
                sub.disabled_at = now
                _notify_disabled(sub, supports_created_disabled)  # fix #13 -- preserve an unsent 'created' notice
            else:
                _apply_backoff(sub, now=now, retry_after=None)
                # r13 fix #6 (kimi LOW 3): every OTHER failure branch
                # (transport error, 429, generic 5xx) already calls
                # `_maybe_auto_disable` here -- this was the one branch that
                # didn't, so a hard-4xx receiver that a skip (or any other
                # failure-class change, see `_record_failure`'s reset-on-
                # class-change rule) kept resetting below the 3-strike
                # `consecutive_failures` threshold could evade disabling
                # forever, streak after streak, never once hitting either
                # rule. The 72h wall clock (`failing_since`, armed by
                # `_record_failure` just above) now backstops it: a
                # receiver that is hard-4xx-ing consistently enough to
                # dodge the 3-strike rule this way is, by definition, still
                # failing every time it IS actually contacted, so
                # `failing_since` keeps accumulating across the whole
                # interleave (fix #4 only clears it for SKIP-class gaps,
                # never for this branch's own real receiver-facing 4xx).
                _maybe_auto_disable(sub, now=now, reason="too_many_failures", supports_created_disabled=supports_created_disabled)
        else:
            _apply_backoff(sub, now=now, retry_after=None)
            _maybe_auto_disable(sub, now=now, reason="too_many_failures", supports_created_disabled=supports_created_disabled)

        db.add(WebhookDelivery(
            subscription_id=sub.id, delivery_id=delivery_id,
            first_seq=rows[0].seq, last_seq=rows[-1].seq, event_count=len(events),
            status=response.status, error=error_class, duration_ms=duration_ms,
        ))
        db.commit()
        return events_sent, True

    return events_sent, False


def run_deliveries(
    db_factory, http_client, *, watermark_seq: int, watermark_dt: datetime, tick_deadline: float,
    supports_created_disabled: bool = True,
) -> TickStats:
    """`db_factory` is a zero-arg callable returning a fresh Session -- each
    worker thread gets its OWN session/connection; SQLAlchemy Sessions are
    not thread-safe to share.

    `supports_created_disabled` (fix #4, round-6) is the per-tick 0013
    probe result (see `_notify_pending_supports_created_disabled`),
    computed once by `run_tick` and threaded straight through to every
    worker's `_drain_one`/`_maybe_auto_disable` call."""
    stats = TickStats()
    lead_db = db_factory()
    try:
        subs = eligible_deliveries(lead_db, now=datetime.now(timezone.utc))
    finally:
        lead_db.close()
    if not subs:
        return stats
    sub_ids = [s.id for s in subs]

    def worker(sub_id: uuid.UUID) -> tuple[int, bool]:
        thread_db = db_factory()
        try:
            sub = thread_db.get(WebhookSubscription, sub_id)
            if sub is None or not sub.active:
                return 0, False
            deadline = min(
                time.monotonic() + PER_SUB_DRAIN_BUDGET_SECONDS, tick_deadline
            )
            try:
                return _drain_one(
                    thread_db, http_client, sub, watermark_seq=watermark_seq,
                    watermark_dt=watermark_dt, deadline=deadline,
                    supports_created_disabled=supports_created_disabled,
                )
            except Exception:
                # Crash isolation (fix #2, hardened round-2 fix #2): one bad
                # row/URL must never escape and crash the tick, but the
                # generic handler must ALSO still apply the full delivery
                # failure policy -- not just stamp last_attempt_at/
                # last_error and move on. Previously that stamping was ALL
                # this branch did: no next_attempt_at backoff, no
                # consecutive_failures, no failing_since (so the 72h
                # auto-disable never fires), no WebhookDelivery audit row
                # -- an invisible 2-minute retry forever, since safe_http's
                # own belt-and-suspenders (fix #1) now guarantees anything
                # reaching here is a genuine bug in THIS module, not a
                # leaked transport exception. Routes through the exact same
                # `_record_failure`/`_apply_backoff`/`_maybe_auto_disable`
                # helpers a normal HTTP failure uses, with error_class
                # 'internal', plus a WebhookDelivery row so
                # GET /webhooks/{id} shows it happened.
                #
                # Verify round-5 fix #1 audit (kimi HIGH #1): this recovery
                # block already used the OUTER `sub_id` parameter (a plain
                # UUID, never `sub.id`) for the `db.get` below, even before
                # this round -- unlike run_challenges' equivalent handler
                # (see that function's own fix #1 comment for the bug that
                # WAS there), this one never touches an attribute on the
                # rolled-back-and-expired `sub` object, so it was never
                # exposed to that specific ObjectDeletedError-from-inside-
                # recovery failure mode. Still wrapped in its own try/except
                # below (log-and-continue) for the SAME belt-and-suspenders
                # reason run_challenges' recovery path now is: the recovery
                # path's OWN writes (the attribute sets, the `add`, the
                # `commit`) are not exempt from some other surprise, and
                # this whole outer handler exists specifically so that one
                # bad sub can never take the tick down with it.
                thread_db.rollback()
                try:
                    fresh = thread_db.get(WebhookSubscription, sub_id)
                    if fresh is not None:
                        now = datetime.now(timezone.utc)
                        _record_failure(fresh, status=None, error_class="internal", now=now)
                        _apply_backoff(fresh, now=now, retry_after=None)
                        _maybe_auto_disable(
                            fresh, now=now, reason="too_many_failures",
                            supports_created_disabled=supports_created_disabled,
                        )
                        thread_db.add(WebhookDelivery(
                            subscription_id=fresh.id, delivery_id=uuid.uuid4(),
                            first_seq=None, last_seq=None, event_count=0,
                            status=None, error="internal", duration_ms=None,
                        ))
                        thread_db.commit()
                except Exception as recovery_exc:  # noqa: BLE001 -- log-and-continue by design
                    thread_db.rollback()
                    print(
                        f"delivery crash-recovery itself failed for sub_id={sub_id}: "
                        f"{recovery_exc.__class__.__name__}: {recovery_exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                return 0, True
        finally:
            thread_db.close()

    with ThreadPoolExecutor(max_workers=DELIVERY_CONCURRENCY) as pool:
        futures = {pool.submit(worker, sub_id): sub_id for sub_id in sub_ids}
        for future in as_completed(futures):
            stats.subs_processed += 1
            try:
                sent, failed = future.result()
            except Exception:  # noqa: BLE001 -- one bad sub must not kill the tick
                stats.failures += 1
                continue
            stats.events_sent += sent
            if failed:
                stats.failures += 1
    return stats


def prune_deliveries(db: OrmSession) -> int:
    cutoff = datetime.now(timezone.utc) - DELIVERY_RETENTION
    result = db.execute(text("DELETE FROM webhook_deliveries WHERE attempted_at < :cutoff"), {"cutoff": cutoff})
    db.commit()
    return result.rowcount or 0


def _notify_pending_supports_created_disabled(db: OrmSession) -> bool:
    """Probe-guarded exactly like `_creation_events_table_exists` below
    (same 0012/0014 information_schema-probe convention) -- verify round-6
    fix #4 (opus MED #3), migration 0013's own probe. True once migration
    0013 (packages/schema/alembic/versions/0013_webhook_notify_pending_
    created_disabled.py) has widened `ck_webhook_notify_pending` to admit
    'created_disabled'; False on a database that still only has 0012's
    original two-value constraint. Called ONCE per tick, in `run_tick`, and
    threaded down through every caller of `_notify_disabled`/`_maybe_auto_
    disable` -- see that function's own docstring for why a per-disable
    probe (instead of a per-tick one) is not worth the extra round-trips."""
    # Verify round-7 fix #6 (opus LOW #5): schema-qualified via
    # `constraint_schema = current_schema()` -- a same-named constraint
    # visible in another schema on the search_path would otherwise make
    # this a two-row result, and `scalar_one_or_none` raises
    # `MultipleResultsFound` (an uncaught crash inside `run_tick`) instead
    # of the clean True/False this probe exists to answer.
    return db.execute(
        text(
            "SELECT 1 FROM information_schema.check_constraints "
            "WHERE constraint_name = 'ck_webhook_notify_pending' "
            "AND check_clause LIKE '%created_disabled%' "
            "AND constraint_schema = current_schema()"
        )
    ).scalar_one_or_none() is not None


def _creation_events_table_exists(db: OrmSession) -> bool:
    """Probe-guarded exactly like workers/alerts/send_alerts.py's
    `drain_webhook_notifications` already is for migration 0012 -- a no-op
    until migration 0014 (webhook_creation_events) is applied to prod.
    Duplicated (by pattern, not by value -- there is no shared constant
    here) from `billcommons_api.routers.webhooks._creation_events_table_
    exists`, which this worker's container cannot import (no apps/api
    ship, see MAX_ACTIVE_PER_HOST's own comment above)."""
    # Verify round-7 fix #6 (opus LOW #5): schema-qualified, same reasoning
    # as `_notify_pending_supports_created_disabled` above.
    return db.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'webhook_creation_events' "
            "AND table_schema = current_schema()"
        )
    ).scalar_one_or_none() is not None


#: Verify round-5 fix #4's GC half: twice the 24h quota window the API reads
#: (`billcommons_api.routers.webhooks.create_webhook`'s `creations_today`
#: query) -- a request straddling midnight must always still see its own
#: full recent history, so pruning at exactly 24h would be one interpretation
#: short of "the last 24 hours" for a row created just under a day ago.
CREATION_EVENT_RETENTION = timedelta(hours=48)


def prune_creation_events(db) -> int:
    """Delete webhook_creation_events (migration 0014) rows older than
    `CREATION_EVENT_RETENTION`. `webhook_creation_events` is an append-only
    audit ledger from the API's perspective (see that table's own model
    docstring for why -- verify round-5 fix #4) -- this dispatcher is the
    ONLY thing that ever deletes from it, same division of responsibility
    `prune_deliveries` above has for `webhook_deliveries`.

    Probe-guarded (see `_creation_events_table_exists`) so this is a clean
    no-op before migration 0014 is applied -- `db` only needs an `.execute`
    method for that probe, which is what lets this be pure-unit-tested with
    a fake session (see workers/webhooks/tests/test_dispatch_webhooks.py)
    without a live table.
    """
    if not _creation_events_table_exists(db):
        return 0
    cutoff = datetime.now(timezone.utc) - CREATION_EVENT_RETENTION
    result = db.execute(
        text("DELETE FROM webhook_creation_events WHERE created_at < :cutoff"), {"cutoff": cutoff}
    )
    db.commit()
    return result.rowcount or 0


# ---------------------------------------------------------------------------
# Tick / main loop
# ---------------------------------------------------------------------------


def _challenge_deadline(*, now: float, tick_deadline: float) -> float:
    """Verify round-3 fix #10 -- pure so it's unit-testable without a live
    tick: see CHALLENGE_MIN_SECONDS' own comment for the full rationale.
    `now` is a `time.monotonic()` reading taken right before challenges
    start (i.e. AFTER deliveries have already run and possibly consumed the
    whole `tick_deadline`)."""
    return max(min(now + CHALLENGE_BUDGET_SECONDS, tick_deadline), now + CHALLENGE_MIN_SECONDS)


def run_tick(db_factory, http_client) -> str:
    tick_deadline = time.monotonic() + TICK_BUDGET_SECONDS
    db = db_factory()
    try:
        watermark_seq, watermark_dt = compute_watermark(db)
        # Fix #4 (round-6): the migration-0013 probe, ONCE per tick -- see
        # `_notify_pending_supports_created_disabled`'s own docstring.
        supports_created_disabled = _notify_pending_supports_created_disabled(db)
    finally:
        db.close()

    stats = run_deliveries(
        db_factory, http_client, watermark_seq=watermark_seq, watermark_dt=watermark_dt,
        tick_deadline=tick_deadline, supports_created_disabled=supports_created_disabled,
    )

    # Challenges run AFTER deliveries, bounded by their OWN sub-budget of the
    # tick (fix #1b/#1c) -- up to 10 x 15s of serial challenge traffic with
    # no backoff previously ran FIRST every tick and could burn the entire
    # 90s tick budget before a single real delivery got a turn.
    #
    # Verify round-3 fix #10: the `min(..., tick_deadline)` alone gives
    # challenges ZERO budget on any tick where deliveries already consumed
    # the whole tick_deadline (see CHALLENGE_MIN_SECONDS' own comment) --
    # `max(..., now + CHALLENGE_MIN_SECONDS)` guarantees a floor slice
    # regardless, at the cost of a small, deliberate, bounded overrun of the
    # tick's own budget.
    challenge_deadline = _challenge_deadline(now=time.monotonic(), tick_deadline=tick_deadline)
    db = db_factory()
    try:
        challenges = run_challenges(
            db, http_client, now=datetime.now(timezone.utc), deadline=challenge_deadline
        )
    finally:
        db.close()

    db = db_factory()
    try:
        pruned = prune_deliveries(db)
    finally:
        db.close()

    db = db_factory()
    try:
        pruned_creation_events = prune_creation_events(db)
    finally:
        db.close()

    return (
        f"tick done: {challenges} challenge(s), {stats.subs_processed} sub(s) processed, "
        f"{stats.events_sent} event(s) sent, {stats.failures} failure(s), {pruned} delivery row(s) pruned, "
        f"{pruned_creation_events} creation-event row(s) pruned"
    )


def _try_acquire_lock(conn, key: int) -> bool:
    """`pg_try_advisory_lock(key)` on `conn`, immediately followed by a
    commit -- verify round-5 fix #2 (kimi #2). `conn.execute` autobegins a
    transaction on its first statement in this connection's session; a
    connection sitting inside an open (never-committed) transaction is
    exactly what `idle_in_transaction_session_timeout=300s`
    (billcommons_shared.db, engine-wide) exists to kill after 5 minutes of
    no activity ON THAT CONNECTION -- and this connection does nothing else
    between calls (it holds the SESSION-scoped advisory lock, not a
    transaction-scoped one; see `main`'s own docstring note on why session-
    scoped). ANY tick running long (a broad receiver outage keeping every
    delivery worker busy near the 90s budget, back-to-back, repeatedly) can
    exceed that 300s idle window on `lock_conn` while it waits for the next
    re-check, silently killing the session and releasing the singleton
    lock out from under a tick that is still running -- a standby replica's
    next `pg_try_advisory_lock` call then succeeds and starts a SECOND,
    concurrent tick. Committing immediately after every acquire/re-check
    keeps this connection's own transaction lifetime at effectively zero,
    so it is never a candidate for the idle-in-transaction killer at all
    (the SESSION-scoped lock itself is unaffected by the commit -- only a
    transaction-scoped `_xact_lock` would release on commit, which is
    exactly why this dispatcher deliberately does NOT use that variant;
    see `main`'s docstring)."""
    acquired = conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar_one()
    conn.commit()
    return acquired


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run a single tick and exit (for tests/manual runs)")
    args = parser.parse_args(argv)

    engine = get_engine()
    lock_conn = engine.connect()
    http_client = new_safe_http_client()

    try:
        while True:
            tick_start = time.monotonic()
            acquired = _try_acquire_lock(lock_conn, ADVISORY_LOCK_KEY)
            if acquired:
                summary = run_tick(get_session, http_client)
                print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {summary}", flush=True)
            else:
                print(
                    f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} "
                    "another replica holds the dispatcher lock; idling this tick",
                    flush=True,
                )
            if args.once:
                return 0
            elapsed = time.monotonic() - tick_start
            time.sleep(max(0.0, TICK_SECONDS - elapsed))
    finally:
        lock_conn.close()


if __name__ == "__main__":
    sys.exit(main())
