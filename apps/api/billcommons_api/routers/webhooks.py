"""/api/v1/webhooks -- push delivery over the change feed.

DB-ONLY. This router performs NO outbound HTTP, ever (see
test_webhooks_no_outbound_http.py, which asserts it by monkeypatching
`socket.socket.connect` for the duration of a request and proving nothing in
this module trips it). `POST /webhooks` creates the row `verified=false` and
returns 201 immediately -- the actual verification challenge and every event
delivery is workers/webhooks/dispatch_webhooks.py's job, running as its own
Railway worker.

Why the synchronous challenge from v1 was removed: it made subscribing an
API-outage switch. A caller could POST 32 subscriptions with a URL that never
responds; each one held a request-handling thread/connection for the full
10s challenge timeout, which is more than enough to saturate the concurrency
limiter (DEFAULT_MAX_CONCURRENT = 32, see billcommons_api.concurrency) and
take the whole service down for every other caller -- the 2026-08-02 outage,
weaponized on purpose instead of by a crawler.

Two secrets, deliberately different (see migration 0012's docstring):
`signing_secret` is returned in plaintext once, at creation, and stored in
plaintext (it has to be readable forever -- it's an HMAC key). The manage
token is ALSO returned in plaintext once, but stored only as a sha256 hash;
everything after creation authenticates against the hash, constant-time.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import publicsuffix2
from pathlib import Path as _Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session as OrmSession

from billcommons_api.deps import get_db
from billcommons_api.errors import bad_request, conflict, not_found
from billcommons_api.rate_limit import client_ip, quota_bucket
from billcommons_api.schemas import (
    WebhookCreateRequest,
    WebhookCreateResponse,
    WebhookDeliveryOut,
    WebhookReactivateResponse,
    WebhookStatusResponse,
)
from billcommons_schema.models import (
    Bill,
    BillEvent,
    Jurisdiction,
    WebhookCreationEvent,
    WebhookDelivery,
    WebhookSubscription,
)
from billcommons_shared.safe_http import SsrfRejected, admit_url
from billcommons_shared.topics import TOPICS
# Re-exported under this name -- see billcommons_shared.watermark's docstring
# for the WHY and the 2026-08-04 empirical basis. Existing importers/tests
# (test_webhooks_watermark_matches_changes_constant) keep working unchanged.
from billcommons_shared.watermark import COMMIT_SAFETY_LAG_SECONDS as COMMIT_SAFETY_LAG_SECONDS  # noqa: F401

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Same email shape as alerts.py -- deliberately loose, see that router's
# comment: real validation is that mail either arrives or it doesn't.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

VALID_EVENT_KINDS = {
    "created", "status", "actions", "sponsors", "text", "metadata", "votes",
}
# r12 fix #1 (opus 1, HIGH -- CONFIRMED by direct probe against the live
# test DB, 2026-08-06): `target` for kind='bills' is comma-joined UUIDs,
# and migration 0012's `uq_webhook_url_kind_target_event_kinds` btree-
# indexes it (part of `UNIQUE (url, kind, target, event_kinds)`) --
# Postgres's own btree tuple ceiling is 2704 bytes (1/3 of an 8KB page).
# A direct INSERT probe at 200 ids (the OLD bound, 7399 bytes of `target`
# alone) raised `psycopg.errors.ProgramLimitExceeded` (SQLSTATE 54000,
# "index row size 3776 exceeds btree version 4 maximum 2704") ESCAPING
# AS A 500 -- the narrowed 23505 handler in `create_webhook` below does
# not, and must not, catch it. A second probe at 100 ids (3699 bytes of
# `target`) reproduced the same failure; at 64 ids (37 bytes/UUID incl.
# comma x 64 = 2367 bytes of `target`, plus a realistic ~80-byte url) the
# INSERT committed cleanly with ~340 bytes of headroom to the 2704 limit.
# 64 is opus's own math -- (2704 - other cols - overhead)/37 ~= 70,
# rounded down for headroom -- not the exact byte-fit boundary, which
# would leave zero margin against a longer `url` (this column has no
# length cap of its own). Re-keying the constraint on a hash of `target`
# instead of `target` itself is the real, post-V1 fix (0012 is already
# applied to prod; no new migration this round).
MAX_BILL_IDS = 64
# r13 fix #1 (codex + grok + opus HIGH, kimi + deepseek MED, 5/7 legs):
# MAX_BILL_IDS above only ever bounded `target`'s OWN byte length -- the
# btree ceiling `uq_webhook_url_kind_target_event_kinds` actually hits is
# on the COMBINED (url, kind, target, event_kinds) tuple. `url` has no
# length cap of its own below the schema's 2000-CHAR limit, and
# `admit_url` percent-encodes it, so a schema-valid 2000-char url (e.g. a
# path full of multi-byte characters, each of which percent-encodes to up
# to 12 bytes) can overflow the tuple ALONE, at any kind, even
# kind='topic' with a two-byte target.
#
# Measured live against the test DB, 2026-08-06 (see
# scratchpad probe -- INSERTs of HIGH-ENTROPY hex payloads, so TOAST
# compression cannot hide the real byte cost the way a repeated-character
# payload would): binary-searched the largest combined
# len(url)+len(kind)+len(target)+len(event_kinds) (byte lengths) that
# still commits, across three different splits of where those bytes live
# (short target/long url; long target/short url; long target AND long
# event_kinds together, forcing every column past the 126-byte
# short-varlena-header cutoff at once). All three converged on the same
# window: sums of 2681-2689 bytes committed cleanly; the smallest failing
# sum was 2685 bytes (`ProgramLimitExceeded`, SQLSTATE 54000, "index row
# size 2712 exceeds btree version 4 maximum 2704" -- 2712 is Postgres's
# MAXALIGN(8)-rounded report, not the raw size, which is why the reported
# number stays fixed near the boundary while the true byte sum still
# moves). Per-tuple overhead (IndexTuple header + per-column varlena
# length prefixes) measured at roughly 19-30 bytes depending on how many
# of the four columns exceed the 126-byte short-header cutoff.
#
# MAX_SUBSCRIPTION_KEY_BYTES=2600 keeps ~81 bytes of headroom below the
# worst (smallest) observed failing sum of 2685 -- covering the
# alignment/header-count variance measured above -- while still
# comfortably fitting MAX_BILL_IDS's own 64-UUID target (2367 bytes) plus
# a realistic url and event_kinds list. Checked at EVERY kind, not just
# 'bills' (opus's kind='topic' long-url case): the combined-bytes budget
# is enforced once, after target/event_kinds/url are all known, before
# any kind-specific bound (like MAX_BILL_IDS) is reached in a request
# whose target is short but whose url alone busts the budget.
MAX_SUBSCRIPTION_KEY_BYTES = 2600
MAX_CREATIONS_PER_IP_PER_DAY = 5
MAX_ACTIVE_PER_HOST = 10
# Verify round-3 fix #1: counts only VERIFIED active subs -- same rationale
# as the per-domain quota's own verified.is_(True) (round-2 fix #7). Without
# it, an attacker could hold the global cap saturated with subs that will
# never verify (nobody controls the target url), locking out every real
# subscriber with no remedy of their own.
MAX_ACTIVE_GLOBAL = 500
# Verify round-7 fix #2: a global unverified cap is itself the outage it was
# meant to prevent. MAX_UNVERIFIED_GLOBAL=250 was reachable with roughly 50
# distinct IPs (5/day each) posting to registrable domains they don't
# control -- exactly the "saturate the pool with subs that will never
# verify" lockout the verified-only caps (MAX_ACTIVE_GLOBAL/
# MAX_ACTIVE_PER_HOST) exist to prevent, just moved one cap over: once full,
# EVERY caller's legitimate creation gets refused, with no remedy of their
# own, until the 24h challenge GC catches up. Replaced with a PER-DOMAIN cap
# instead -- the same shape MAX_ACTIVE_PER_HOST already uses for verified
# subs, so no single domain (attacker-controlled or not) can hold more than
# MAX_UNVERIFIED_PER_HOST unverified rows at once, while unrelated domains'
# creations are never affected by it. The pool is bounded by three OTHER
# mechanisms without needing a global choke point: challenge work itself is
# tick-bounded (MAX_CHALLENGES_PER_TICK, workers/webhooks/
# dispatch_webhooks.py), volume per IP is creation-event-bounded
# (MAX_CREATIONS_PER_IP_PER_DAY), and lifetime is GC-bounded (24h never-
# verified, 7d quota-disabled-while-unverified -- see run_challenges).
# r11 fix #6 (opus D): PER-DOMAIN alone still let an attacker's junk
# unverified subs 403 the real domain owner's own attempt to subscribe --
# both queries that enforce this cap (create_webhook, reactivate) now scope
# it to (host, creator_ip), not host alone. See each call site's own
# comment.
MAX_UNVERIFIED_PER_HOST = 10

# Quota TOCTOU (verify round-1 fix #16): two concurrent creations can both
# read "under quota" before either INSERTs, both pass, and the quota is
# exceeded. Traffic on this router is tiny, so a single global advisory
# transaction lock around "count quotas, then INSERT" -- serializing EVERY
# creation, not just same-IP/same-host ones -- is airtight and cheap. Own
# key, distinct from workers/webhooks/dispatch_webhooks.py's
# ADVISORY_LOCK_KEY (different namespace, same DB): namespaced via a string
# hash so a future key added to either module can't collide by coincidence.
_QUOTA_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"billcommons.webhooks.create_quota").digest()[:8], "big"
) & 0x7FFFFFFFFFFFFFFF

# COMMIT_SAFETY_LAG_SECONDS: imported at the top of this file, from
# billcommons_shared.watermark -- a new subscription's last_seq must be the
# same safety-lag watermark every other reader of bill_events serves from,
# never the raw head.


def _watermark_stmt():
    cutoff = func.now() - timedelta(seconds=COMMIT_SAFETY_LAG_SECONDS)
    return select(func.coalesce(func.max(BillEvent.seq), 0)).where(BillEvent.changed_at <= cutoff)


def _creation_events_table_exists(db: OrmSession) -> bool:
    """Probe-guarded exactly like workers/alerts/send_alerts.py's
    `drain_webhook_notifications` already is for migration 0012 -- a no-op
    fallback until migration 0014 (webhook_creation_events) is applied to
    prod. Verify round-5 fix #4: once the table exists, `create_webhook`
    counts creation ATTEMPTS from it (immune to a subsequent `DELETE`)
    instead of surviving `webhook_subscriptions` rows -- see
    `WebhookCreationEvent`'s own docstring for why that mattered."""
    # Verify round-7 fix #6 (opus LOW #5): schema-qualified. Without
    # `table_schema = current_schema()`, a same-named table visible in
    # ANOTHER schema on the search_path (a staging/shadow copy, a foreign
    # data wrapper, ...) makes this a two-row result -- `scalar_one_or_none`
    # raises `MultipleResultsFound`, an uncaught 500 on every single
    # `POST /webhooks` until whichever schema is at fault is renamed away.
    return db.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'webhook_creation_events' "
            "AND table_schema = current_schema()"
        )
    ).scalar_one_or_none() is not None


def _challenge_attempted_at_column_exists(db: OrmSession) -> bool:
    """Migration 0015's own probe -- duplicated (by pattern, not by value;
    there is no shared constant to import across the apps/api <->
    workers/webhooks container boundary, see this router's module
    docstring) from workers/webhooks/dispatch_webhooks.py's function of
    the same name. `challenge_attempted_at` is deliberately NOT mapped on
    `billcommons_schema.models.WebhookSubscription` (a mapped column, even
    deferred, would be included in every INSERT/UPDATE SQLAlchemy
    generates for this model, breaking subscription creation site-wide
    before migration 0015 is ever applied) -- every read/write of it in
    this router goes through raw `text()` SQL instead, gated behind this
    probe, same as the dispatcher's own."""
    return db.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'webhook_subscriptions' "
            "AND column_name = 'challenge_attempted_at' "
            "AND table_schema = current_schema()"
        )
    ).scalar_one_or_none() is not None


def _creation_quota_stmt(*, events_table_exists: bool, ip: str, since):
    """The per-IP daily creation-quota COUNT query, factored out as a pure
    builder (no `db.execute` inside it) specifically so the routing logic
    -- "count from the new ledger once it exists, else fall back to the old
    surviving-rows count" -- is unit-testable without a live table (verify
    round-5 fix #4's own instruction: "unit-test the count query builder").
    See `WebhookCreationEvent`'s docstring for why the events-table count is
    the real fix and the subscriptions-table count is a pre-migration-0014
    fallback only.
    """
    if events_table_exists:
        return select(func.count()).select_from(WebhookCreationEvent).where(
            WebhookCreationEvent.creator_ip == ip, WebhookCreationEvent.created_at >= since
        )
    return select(func.count()).select_from(WebhookSubscription).where(
        WebhookSubscription.creator_ip == ip, WebhookSubscription.created_at >= since
    )


# publicsuffix2's PACKAGED data is a 2019 snapshot -- it predates dozens of
# hosted-platform entries (e.g. the per-region `s3.<region>.amazonaws.com`
# suffixes), which would collapse unrelated tenants into shared quota
# buckets. We therefore vendor a CURRENT snapshot in-repo and load it once
# here; still zero network fetches at runtime. (Checked while vendoring:
# the current PSL deliberately has NO cloudapp.azure.com entry, so Azure
# regional hosts bucketing to azure.com is standard-aligned, not a gap.)
# Refresh: re-download when touching this file --
#   curl -sL https://publicsuffix.org/list/public_suffix_list.dat \
#     -o apps/api/billcommons_api/data/public_suffix_list.dat
_PSL = publicsuffix2.PublicSuffixList(
    str(_Path(__file__).resolve().parent.parent / "data" / "public_suffix_list.dat")
)


def _registrable_domain(hostname: str) -> str:
    """eTLD+1 (registrable domain) via the REAL public suffix list (Verify
    round-7 fix #3, third round of this finding class -- opus MED #3 +
    codex MED). The prior curated `_MULTI_PART_SUFFIXES` fixed-depth
    approach (round-3 fix #3 through round-6 fix #6) can only ever
    represent suffixes at a depth someone thought to add: a real hostname
    like "mybucket.s3.us-east-1.amazonaws.com" has a VARIABLE-depth suffix
    (the region label varies), and the curated set's one "s3.amazonaws.com"
    entry can never match it -- every AWS bucket in that shape collapsed
    into the plain two-label "amazonaws.com" bucket despite a dedicated
    curated entry existing for the general case, one quota shared by every
    AWS customer on that pattern. `publicsuffix2` ships the ICANN/Mozilla
    public suffix list DATA bundled in the package (no network fetch at
    runtime, no new startup dependency) and answers the variable-depth case
    correctly by construction.

    Verify round-4 fix #4 (kept unchanged): an IP-LITERAL hostname (e.g.
    "198.51.100.42" or an IPv6 literal) is checked FIRST and, if it parses
    as one, its bucket is the FULL literal string -- a public-suffix-list
    lookup has no concept of an IP address and would either return None or
    something nonsensical for one. Two unrelated IPv4 literals that merely
    happen to share their trailing octets are NOT the same registrable
    domain by any definition.

    Verify round-9 fix #2: the literal was returned VERBATIM, un-normalized
    -- the same IPv6 address has many equally-valid textual forms (e.g.
    "::1" vs "0:0:0:0:0:0:0:1"), and each distinct spelling landed in its
    own quota bucket for what is really one host. Brackets are stripped
    first (defensive: `admit_url`'s own `.hostname` already strips them for
    this function's one real caller, but nothing about this helper's
    contract requires an already-unbracketed input), then the parsed
    address's canonical `.compressed` form is returned instead of the raw
    string, so every spelling of the same address collapses to one bucket.
    """
    hostname = hostname.rstrip(".")  # trailing-dot FQDN -- fix #19
    unbracketed = hostname[1:-1] if hostname.startswith("[") and hostname.endswith("]") else hostname
    try:
        addr = ipaddress.ip_address(unbracketed)
    except ValueError:
        pass
    else:
        return addr.compressed
    return _PSL.get_sld(hostname) or hostname


def _validate_target(
    db: OrmSession, kind: str, target: str, jurisdiction: str | None
) -> str:
    """Validate+normalize `target` per `kind`, same rigor as alerts.py's
    subscribe validation (422 on anything that would silently subscribe to
    nothing). Returns the canonical string stored in the `target` column.

    kind='topic' folds an optional jurisdiction SCOPE into `target` as
    "{slug}:{JUR}" -- migration 0012 has one `target` column, not a second
    nullable jurisdiction column like alert_subscriptions (migration 0011),
    so the scope has to live inside it. Unscoped stays a bare slug, so an
    existing bare-slug subscription's stored value never changes shape.
    """
    target = (target or "").strip()
    if kind == "topic":
        if target not in TOPICS:
            raise bad_request(
                "unknown_topic", f"unknown topic {target!r}; see /api/v1/topics"
            )
        if jurisdiction is not None and jurisdiction.strip():
            jur = jurisdiction.strip().upper()
            known = db.execute(
                select(Jurisdiction.abbreviation).where(Jurisdiction.abbreviation == jur)
            ).scalar_one_or_none()
            if known is None:
                raise bad_request(
                    "unknown_jurisdiction",
                    f"unknown jurisdiction {jur!r}; use a two-letter state abbreviation.",
                )
            return f"{target}:{jur}"
        return target

    if kind == "jurisdiction":
        if jurisdiction is not None and jurisdiction.strip():
            raise bad_request(
                "jurisdiction_field_not_used",
                "the `jurisdiction` field only applies to kind='topic'; for "
                "kind='jurisdiction' put the abbreviation in `target`.",
            )
        jur = target.upper()
        known = db.execute(
            select(Jurisdiction.abbreviation).where(Jurisdiction.abbreviation == jur)
        ).scalar_one_or_none()
        if known is None:
            raise bad_request(
                "unknown_jurisdiction",
                f"unknown jurisdiction {jur!r}; use a two-letter state abbreviation.",
            )
        return jur

    if kind == "bills":
        if jurisdiction is not None and jurisdiction.strip():
            raise bad_request(
                "jurisdiction_field_not_used",
                "the `jurisdiction` field only applies to kind='topic'.",
            )
        raw_ids = [t.strip() for t in target.split(",") if t.strip()]
        if not raw_ids:
            raise bad_request("no_bill_ids", "at least one bill id is required for kind='bills'.")
        if len(raw_ids) > MAX_BILL_IDS:
            raise bad_request(
                "too_many_bill_ids",
                f"at most {MAX_BILL_IDS} bill ids per subscription, got {len(raw_ids)}.",
            )
        parsed = []
        for token in raw_ids:
            try:
                parsed.append(str(uuid.UUID(token)))
            except ValueError:
                raise bad_request("invalid_bill_id", f"{token!r} is not a valid bill id.") from None
        deduped = sorted(set(parsed))

        # Verify round-6 fix #5 (deepseek MED #5): a UUID that parses fine
        # but names no real bill used to quiet-run FOREVER -- `_fetch_batch`
        # (workers/webhooks/dispatch_webhooks.py) filters `BillEvent.bill_id
        # .in_(ids)`, which is a perfectly healthy-looking empty match for a
        # typo'd id, exactly the "reported as nothing to report" failure
        # mode this codebase's other scope-validity fixes (round-5 fix #8,
        # this round's fix #2) all exist to prevent. Checked HERE, at
        # creation, not by the dispatcher: bills are never deleted by ingest
        # (see this function's own module-level assumptions elsewhere in
        # this file), so a bill id that exists at creation time is
        # guaranteed to still exist for the life of the subscription --
        # no dispatcher-side re-check is needed the way the jurisdiction
        # scope re-check (fix #2) is, because THAT can go stale (a
        # jurisdiction row itself is never expected to disappear either, but
        # the stored abbreviation could reference one that was renamed/never
        # existed -- see that fix's own comment).
        found = set(
            db.execute(
                select(Bill.id).where(Bill.id.in_(uuid.UUID(i) for i in deduped))
            ).scalars()
        )
        missing = [i for i in deduped if uuid.UUID(i) not in found]
        if missing:
            raise bad_request(
                "invalid_webhook_target",
                f"unknown bill id(s): {missing}",
            )
        return ",".join(deduped)

    raise bad_request(
        "unknown_kind", f"kind must be one of topic, jurisdiction, bills; got {kind!r}."
    )


def _validate_event_kinds(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    kinds = [k.strip() for k in raw.split(",") if k.strip()]
    if not kinds:
        # Whitespace- or comma-only input (e.g. " ", ",,") -- normalize to
        # NULL, never "" (fix #18): an empty string is a distinct, wrong
        # value from NULL for the UNIQUE (url, kind, target, event_kinds)
        # constraint (nulls_not_distinct=True treats every NULL as the same
        # "no filter" row; two "" rows would NOT collide there and would
        # silently coexist as duplicates).
        return None
    unknown = sorted(set(kinds) - VALID_EVENT_KINDS)
    if unknown:
        raise bad_request(
            "unknown_event_kind",
            f"unknown event kind(s) {unknown}; valid values: {sorted(VALID_EVENT_KINDS)}.",
        )
    return ",".join(sorted(set(kinds)))


def _require_manage_token(row: WebhookSubscription, authorization: str | None) -> None:
    """403 unless `authorization` is `Bearer <manage_token>` whose sha256
    matches the stored hash, compared constant-time.

    The row itself is looked up by caller BEFORE this runs (404 if no such
    id) -- this only ever fires once the id is known to exist, so "missing
    header" and "wrong token" both mean the same thing from here: not
    authorized to manage this (real) subscription. 403, not 404, per the
    spec's own testing requirement -- unlike the id lookup, this is not an
    existence question.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=403,
            detail={"code": "webhook_auth_required", "message": "Authorization: Bearer <manage_token> is required."},
        )
    presented = authorization[len("bearer "):].strip()
    presented_hash = hashlib.sha256(presented.encode()).hexdigest()
    if not hmac.compare_digest(presented_hash, row.manage_token_hash):
        raise HTTPException(
            status_code=403,
            detail={"code": "webhook_auth_invalid", "message": "invalid manage token."},
        )


@router.post("", response_model=WebhookCreateResponse, status_code=201)
def create_webhook(
    body: WebhookCreateRequest, request: Request, db: OrmSession = Depends(get_db)
) -> WebhookCreateResponse:
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise bad_request("invalid_email", "that does not look like an email address.")

    try:
        admitted = admit_url(body.url.strip())
    except SsrfRejected as exc:
        raise bad_request(
            "invalid_webhook_url",
            "the webhook url must be https, on the default port (443), with "
            f"no userinfo and no fragment ({exc.reason}).",
        ) from None

    target = _validate_target(db, body.kind, body.target, body.jurisdiction)
    event_kinds = _validate_event_kinds(body.event_kinds)
    # Store the ADMITTED/normalized url, never the raw input (fix #17):
    # lowercased+IDNA host (already what `admit_url` returns), no explicit
    # ":443" (there never is one in `admitted.path_and_query`/hostname to
    # begin with -- `urlsplit.hostname` strips the port), no trailing-dot
    # host. Without this, two subscriptions differing only by e.g. host
    # case or a trailing FQDN dot both bypass the (url, kind, target,
    # event_kinds) uniqueness constraint AND count against DIFFERENT
    # per-host quota buckets for what is really the same host.
    normalized_host = admitted.hostname.rstrip(".")
    # IPv6 literal brackets (round-2 fix #4): `urlsplit.hostname` (what
    # `admit_url` returns as `.hostname`) strips the `[...]` around an IPv6
    # literal -- reconstructing the url from the bare address alone
    # produces something like "https://2001:db8::1/hook", which is not a
    # valid URL at all (the colons are indistinguishable from a port
    # separator) and can never be delivered to or challenged. Re-bracket
    # whenever the host contains ':' -- the one character that can never
    # appear in a DNS hostname/IDNA label, so this is an unambiguous IPv6
    # literal test, not a heuristic.
    url_host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    normalized_url = f"https://{url_host}{admitted.path_and_query}"
    host = _registrable_domain(normalized_host)

    # r13 fix #1a: reject BEFORE the INSERT is even attempted, for every
    # kind -- see MAX_SUBSCRIPTION_KEY_BYTES's own comment for the probe
    # this budget is measured from.
    subscription_key_bytes = (
        len(normalized_url.encode())
        + len(body.kind.encode())
        + len((target or "").encode())
        + len((event_kinds or "").encode())
    )
    if subscription_key_bytes > MAX_SUBSCRIPTION_KEY_BYTES:
        raise bad_request(
            "subscription_key_too_large",
            "this subscription's url/kind/target/event_kinds combination is "
            f"too large to store (at most {MAX_SUBSCRIPTION_KEY_BYTES} "
            "combined bytes); use a shorter url or fewer bill ids.",
        )

    # Quota TOCTOU (fix #16): hold a global advisory lock for the rest of
    # this transaction so no second concurrent creation can read "under
    # quota" before this one's INSERT commits. Released automatically at
    # commit/rollback (pg_advisory_xact_lock is transaction-scoped) -- see
    # apps/api/billcommons_api/deps.get_db, which always closes (and so
    # rolls back anything uncommitted on) this session at request end.
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _QUOTA_LOCK_KEY})

    # r12 fix #4 (opus 4, MED but gutting): `quota_bucket` collapses an
    # IPv6 caller to its /64 network before it is stored as `creator_ip`
    # or used to count against ANY per-IP quota below -- without it, a
    # caller with a routed /64 mints a fresh, never-reused address per
    # request, and every one of these quotas (this daily count,
    # MAX_CREATIONS_PER_IP_PER_DAY, AND the (host, creator_ip) unverified
    # cap further down) mints a fresh bucket right along with it. See
    # `quota_bucket`'s own comment (billcommons_api.rate_limit) for why
    # this wraps `client_ip`'s result rather than changing `client_ip`
    # itself.
    ip = quota_bucket(client_ip(request))
    since = func.now() - timedelta(days=1)
    # Verify round-5 fix #4: count creation ATTEMPTS from
    # `webhook_creation_events` (immune to a subsequent DELETE/GC of the
    # subscription it corresponds to), not surviving `webhook_subscriptions`
    # rows -- see `WebhookCreationEvent`'s own docstring. Probe-guarded so
    # this router keeps working before migration 0014 is applied (see
    # `_creation_events_table_exists`); the fallback branch is the OLD,
    # exploitable count and should stop being exercised in production the
    # moment 0014 ships -- smoke-test that post-migration.
    events_table_exists = _creation_events_table_exists(db)
    creations_today = db.execute(
        _creation_quota_stmt(events_table_exists=events_table_exists, ip=ip, since=since)
    ).scalar_one()
    if creations_today >= MAX_CREATIONS_PER_IP_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "webhook_creation_quota_exceeded",
                "message": (
                    f"at most {MAX_CREATIONS_PER_IP_PER_DAY} new webhook subscriptions per "
                    "IP per day. Try again tomorrow, or manage your existing "
                    "subscriptions with the manage_token you were given at creation."
                ),
            },
        )

    # Round-2 fix #7: only VERIFIED subscriptions count against the
    # per-domain quota. Without `verified.is_(True)` here, an attacker who
    # controls neither the domain nor an endpoint that will ever answer the
    # verification challenge could still POST `MAX_ACTIVE_PER_HOST`
    # subscriptions naming a victim's domain (unverifiable, so they never
    # do anything -- but they still hold the `active=true` row until the
    # dispatcher's 24h GC removes them) and lock the real domain owner out
    # of ever subscribing a webhook, with no remedy of their own. Unverified
    # volume against one domain stays bounded some other way already: the
    # per-IP creation quota (5/day), the per-domain quota still applying
    # ONCE those unverified subs verify, and the dispatcher's own 24h GC of
    # never-verified subs (see run_challenges).
    active_for_host = db.execute(
        select(func.count()).select_from(WebhookSubscription).where(
            WebhookSubscription.host == host,
            WebhookSubscription.active.is_(True),
            WebhookSubscription.verified.is_(True),
        )
    ).scalar_one()
    if active_for_host >= MAX_ACTIVE_PER_HOST:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "webhook_domain_quota_exceeded",
                "message": f"at most {MAX_ACTIVE_PER_HOST} active webhook subscriptions per domain.",
            },
        )

    # Verify round-3 fix #1: verified.is_(True) added -- an unverifiable sub
    # (nobody controls the target url) must never occupy a global-quota seat
    # forever; see MAX_ACTIVE_GLOBAL's own comment above.
    active_global = db.execute(
        select(func.count()).select_from(WebhookSubscription).where(
            WebhookSubscription.active.is_(True),
            WebhookSubscription.verified.is_(True),
        )
    ).scalar_one()
    if active_global >= MAX_ACTIVE_GLOBAL:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "webhook_global_quota_exceeded",
                "message": "Bill Commons webhooks are at global capacity for now.",
            },
        )

    # Verify round-7 fix #2: the companion cap on the UNVERIFIED pool is
    # PER-DOMAIN, not global -- see MAX_UNVERIFIED_PER_HOST's own comment
    # above for why a global cap here is itself a kill switch.
    #
    # r11 fix #6 (opus D): PER-DOMAIN alone was still a victim-lockout --
    # an attacker's 10 junk unverified subs naming a real domain owner's
    # host filled that domain's ENTIRE unverified pool, 403ing the real
    # owner's own attempt to subscribe a webhook for their own domain, with
    # no remedy until the 24h challenge GC (fix #5) caught up. Scoped to
    # (host, creator_ip) instead: an attacker can still fill up to
    # MAX_UNVERIFIED_PER_HOST slots FOR ITS OWN IP against that domain, but
    # a different caller's IP -- e.g. the real owner's -- gets its own
    # independent MAX_UNVERIFIED_PER_HOST budget against the same host.
    unverified_for_host = db.execute(
        select(func.count()).select_from(WebhookSubscription).where(
            WebhookSubscription.host == host,
            WebhookSubscription.creator_ip == ip,
            WebhookSubscription.active.is_(True),
            WebhookSubscription.verified.is_(False),
        )
    ).scalar_one()
    if unverified_for_host >= MAX_UNVERIFIED_PER_HOST:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "webhook_domain_quota_exceeded",
                "message": (
                    f"at most {MAX_UNVERIFIED_PER_HOST} unverified webhook "
                    "subscriptions per domain at once."
                ),
            },
        )

    watermark = db.execute(_watermark_stmt()).scalar_one()

    manage_token = secrets.token_urlsafe(32)
    row = WebhookSubscription(
        url=normalized_url,
        host=host,
        email=email,
        creator_ip=ip,
        signing_secret=secrets.token_urlsafe(32),
        manage_token_hash=hashlib.sha256(manage_token.encode()).hexdigest(),
        kind=body.kind,
        target=target,
        event_kinds=event_kinds,
        last_seq=watermark,
        challenge_token=secrets.token_urlsafe(24),
    )
    db.add(row)
    if events_table_exists:
        # Verify round-5 fix #4: logged in the SAME transaction as the
        # subscription itself, so an IntegrityError below (a duplicate
        # url/kind/target/event_kinds) rolls this back too -- a 409 never
        # consumes a slot of the caller's daily quota, only a genuine new
        # subscription does.
        db.add(WebhookCreationEvent(creator_ip=ip))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Batch item 8 (grok H): this used to catch EVERY IntegrityError and
        # map it to a 409 "already exists" -- including one this table's
        # OTHER constraints could just as easily raise (`ck_webhook_kind`,
        # `ck_webhook_notify_pending`), which would then lie to the caller
        # about WHY their request failed. Narrowed to the exact
        # unique-violation this constraint is: SQLSTATE 23505 (the
        # Postgres-standard code, stable across psycopg major versions,
        # unlike relying on a specific exception class) AND the specific
        # constraint name -- anything else re-raises as an ordinary
        # uncaught 500, same as any other genuinely-unexpected DB error.
        orig = exc.orig
        sqlstate = getattr(orig, "sqlstate", None)
        constraint_name = getattr(getattr(orig, "diag", None), "constraint_name", None)
        if sqlstate == "23505" and constraint_name == "uq_webhook_url_kind_target_event_kinds":
            raise conflict(
                "webhook_already_exists",
                "a webhook with this url/kind/target/event_kinds already exists.",
            ) from None
        raise
    except DBAPIError as exc:
        db.rollback()
        # r13 fix #1b: safety net for the same btree tuple-size ceiling
        # MAX_SUBSCRIPTION_KEY_BYTES's pre-insert check exists to prevent --
        # SQLSTATE 54000 (ProgramLimitExceeded) is not an IntegrityError (it
        # is not a constraint violation at all), so it never reached the
        # `except IntegrityError` branch above and previously escaped as an
        # uncaught 500. The pre-insert budget check is sized with headroom
        # and should make this branch unreachable in practice; kept as a
        # safety net (a future budget miscalibration, or a Postgres/schema
        # change that alters the per-tuple overhead, degrades to a 400
        # instead of a 500). Narrowed to exactly 54000 -- anything else
        # re-raises as an ordinary uncaught 500, same as the IntegrityError
        # branch's own narrowing.
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate == "54000":
            raise bad_request(
                "subscription_key_too_large",
                "this subscription's url/kind/target/event_kinds combination "
                "is too large to store; use a shorter url or fewer bill ids.",
            ) from None
        raise

    return WebhookCreateResponse(
        id=row.id,
        manage_token=manage_token,
        signing_secret=row.signing_secret,
        verified=False,
        note=(
            "Shown once -- store manage_token and signing_secret now. A "
            "verification challenge will be sent to your url within the next "
            "few minutes; the subscription starts delivering events once it "
            "answers correctly."
        ),
    )


@router.get("/{sub_id}", response_model=WebhookStatusResponse)
def get_webhook(
    sub_id: uuid.UUID,
    request: Request,
    authorization: str | None = Header(default=None),
    db: OrmSession = Depends(get_db),
) -> WebhookStatusResponse:
    row = db.get(WebhookSubscription, sub_id)
    if row is None:
        raise not_found("webhook_not_found", "no such webhook subscription.")
    _require_manage_token(row, authorization)

    watermark = db.execute(_watermark_stmt()).scalar_one()
    deliveries = db.execute(
        select(WebhookDelivery)
        .where(WebhookDelivery.subscription_id == sub_id)
        .order_by(WebhookDelivery.attempted_at.desc())
        .limit(10)
    ).scalars().all()

    return WebhookStatusResponse(
        id=row.id,
        url=row.url,
        kind=row.kind,
        target=row.target,
        event_kinds=row.event_kinds,
        verified=row.verified,
        active=row.active,
        last_success_at=row.last_success_at,
        last_status=row.last_status,
        last_error=row.last_error,
        consecutive_failures=row.consecutive_failures,
        failing_since=row.failing_since,
        disabled_reason=row.disabled_reason,
        disabled_at=row.disabled_at,
        cursor_lag_seq=max(0, watermark - row.last_seq),
        recent_deliveries=[
            WebhookDeliveryOut(
                delivery_id=d.delivery_id,
                first_seq=d.first_seq,
                last_seq=d.last_seq,
                event_count=d.event_count,
                status=d.status,
                error=d.error,
                duration_ms=d.duration_ms,
                attempted_at=d.attempted_at,
            )
            for d in deliveries
        ],
        meta={"api_version": "v1", "request_id": request.state.request_id},
    )


@router.delete("/{sub_id}", status_code=204)
def delete_webhook(
    sub_id: uuid.UUID,
    authorization: str | None = Header(default=None),
    db: OrmSession = Depends(get_db),
) -> None:
    row = db.get(WebhookSubscription, sub_id)
    if row is None:
        raise not_found("webhook_not_found", "no such webhook subscription.")
    _require_manage_token(row, authorization)
    db.delete(row)
    db.commit()
    return None


@router.post("/{sub_id}/reactivate", response_model=WebhookReactivateResponse)
def reactivate_webhook(
    sub_id: uuid.UUID,
    request: Request,
    mode: str | None = Query(
        default=None,
        description=(
            "resume: keep last_seq, the backlog since disable drains normally. "
            "skip: fast-forward last_seq to the current watermark, dropping "
            "the backlog. Required -- there is no default."
        ),
    ),
    authorization: str | None = Header(default=None),
    db: OrmSession = Depends(get_db),
) -> WebhookReactivateResponse:
    row = db.get(WebhookSubscription, sub_id)
    if row is None:
        raise not_found("webhook_not_found", "no such webhook subscription.")
    _require_manage_token(row, authorization)

    # Verify round-3 fix #14: guards on `disabled_reason is None` ALONE, not
    # `disabled_reason is None and row.active` -- only auto-disabled subs are
    # reactivatable per spec, full stop. `DELETE /webhooks/{id}` is a hard
    # delete (see delete_webhook below), so there is no code path today that
    # leaves a row `active=false` with `disabled_reason IS NULL` -- but every
    # `active=False` write in this codebase sets `disabled_reason` in the
    # same assignment (see workers/webhooks/dispatch_webhooks.py's disable
    # branches), so asserting it here is cheap insurance against a future one
    # that forgets to, not a fix for an exploitable gap today.
    if row.disabled_reason is None:
        raise conflict(
            "webhook_not_disabled", "this webhook is not auto-disabled; nothing to reactivate."
        )
    if mode not in ("resume", "skip"):
        raise conflict(
            "reactivate_mode_required",
            "pass ?mode=resume (keep the backlog) or ?mode=skip (drop it and "
            "start from now) -- there is no default.",
        )

    # Verify round-4 fix #2: reactivate must re-check the SAME quotas
    # creation and verification-promotion already enforce -- previously this
    # endpoint wrote `active=True` unconditionally, no quota check at all.
    # Take the SAME advisory lock creation uses (`_QUOTA_LOCK_KEY`) for the
    # rest of this transaction first, so a reactivate racing a concurrent
    # creation/reactivate/promotion can't both read "under quota" before
    # either commits (identical TOCTOU shape to create_webhook's own fix
    # #16). Only a row that is ALREADY verified can retake a verified-quota
    # seat by reactivating -- an unverified row goes through the re-
    # challenge path below instead (fix #3), and its own eventual promotion
    # is where the worker-side quota checks (round-3 fix #2, round-4 fix #1)
    # apply.
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _QUOTA_LOCK_KEY})

    if row.verified:
        # `row` is currently `active=False` (it is auto-disabled -- the
        # `disabled_reason is None` guard above already proved that), so it
        # is NOT counted by either query below; no self-exclusion needed,
        # same reasoning as the worker's own promotion-time recounts.
        verified_on_host = db.execute(
            select(func.count()).select_from(WebhookSubscription).where(
                WebhookSubscription.host == row.host,
                WebhookSubscription.active.is_(True),
                WebhookSubscription.verified.is_(True),
            )
        ).scalar_one()
        if verified_on_host >= MAX_ACTIVE_PER_HOST:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "webhook_quota_exceeded",
                    "message": (
                        f"cannot reactivate: this registrable domain is at "
                        f"its {MAX_ACTIVE_PER_HOST}-verified-subscription "
                        "quota right now."
                    ),
                },
            )
        verified_globally = db.execute(
            select(func.count()).select_from(WebhookSubscription).where(
                WebhookSubscription.active.is_(True),
                WebhookSubscription.verified.is_(True),
            )
        ).scalar_one()
        if verified_globally >= MAX_ACTIVE_GLOBAL:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "webhook_quota_exceeded",
                    "message": "cannot reactivate: Bill Commons webhooks are at global capacity right now.",
                },
            )
    else:
        # Verify round-5 fix #5 (opus #3, deepseek #4), replaced round-7
        # fix #2: reactivating an UNVERIFIED row re-arms its challenge (a
        # fresh token + a reset 24h GC clock -- see the `if not
        # row.verified:` block below) and puts it back in the unverified
        # pool WITHOUT ever recounting that pool -- `MAX_UNVERIFIED_PER_HOST`
        # exists precisely to bound it (see its own comment above
        # `create_webhook`), and a reactivate can re-inflate it exactly as
        # effectively as a fresh creation can. Recount under the SAME
        # advisory lock already held above, and 409 rather than let it
        # through -- `row` is currently `active=False` (proved by the
        # `disabled_reason is None` guard earlier), so it is not counted by
        # this query; no self-exclusion needed, same reasoning the verified
        # branch above already uses.
        #
        # r11 fix #6: scoped to (host, creator_ip) -- same reasoning and
        # same shape as `create_webhook`'s identical query above.
        # `row.creator_ip` (the ORIGINAL creator's IP, recorded at creation
        # time), not the reactivating caller's own IP: reactivation is
        # authenticated by manage_token, not by IP, and a legitimate owner
        # reactivating from a new IP must be scoped by which IP's junk
        # subscriptions actually fill this domain's pool, not by whichever
        # IP happens to be reactivating right now.
        unverified_for_host = db.execute(
            select(func.count()).select_from(WebhookSubscription).where(
                WebhookSubscription.host == row.host,
                WebhookSubscription.creator_ip == row.creator_ip,
                WebhookSubscription.active.is_(True),
                WebhookSubscription.verified.is_(False),
            )
        ).scalar_one()
        if unverified_for_host >= MAX_UNVERIFIED_PER_HOST:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "webhook_quota_exceeded",
                    "message": (
                        "cannot reactivate: this registrable domain is at its "
                        f"{MAX_UNVERIFIED_PER_HOST}-unverified-subscription "
                        "quota right now."
                    ),
                },
            )

    now = datetime.now(timezone.utc)

    if mode == "skip":
        # Verify round-4 fix #6: GREATEST, not a plain assignment -- a
        # racing drain that already advanced `last_seq` past this stale
        # watermark read would otherwise get dragged BACKWARD (duplicate
        # deliveries), the exact same race
        # workers/webhooks/dispatch_webhooks.py's `_advance_last_seq`
        # (round-3 fix #11) already guards against on the worker side; this
        # endpoint writes `last_seq` too and needs the identical SQL shape.
        watermark = db.execute(_watermark_stmt()).scalar_one()
        db.execute(
            update(WebhookSubscription)
            .where(WebhookSubscription.id == row.id)
            .values(last_seq=func.greatest(WebhookSubscription.last_seq, watermark))
        )
        db.refresh(row, attribute_names=["last_seq"])

    if not row.verified:
        # Verify round-4 fix #3 (kimi Finding 1 / codex HIGH #2): a
        # domain/global-quota disable AT PROMOTION time (see
        # workers/webhooks/dispatch_webhooks.py's `_attempt_challenge`)
        # leaves `verified=False` but ALREADY clears `challenge_token` to
        # None -- reactivating that row with no further action would
        # re-enable a row whose next challenge attempt POSTs
        # `{"challenge": null, ...}` and whose 2xx-body comparison does
        # `sub.challenge_token.encode()`, an AttributeError on a None. That
        # crash routes through `run_challenges`' generic exception handler,
        # which backs off and retries -- forever, on a token that will
        # never exist -- until the 24h-since-`created_at` challenge GC (see
        # `run_challenges`) silently hard-deletes the row, possibly within
        # minutes of this very reactivation if it happened close to the
        # original 24h mark. Three-part fix, all in this branch:
        # (a) regenerate a REAL token, same length (24 bytes) `create_webhook`
        #     uses, so the next challenge attempt has something real to send
        #     and compare against.
        # (b) reset the clock the challenge GC actually reads --
        #     `run_challenges` keys the 24h GC off `sub.created_at` (there is
        #     no separate "challenge started at" column), so bumping
        #     `created_at` forward to now is what actually resets that
        #     clock; anything else (e.g. only clearing `next_attempt_at`,
        #     done below anyway) leaves the GC counting from the ORIGINAL
        #     creation time and can still delete the row almost immediately.
        # (c) `consecutive_failures`/`next_attempt_at` are already reset
        #     unconditionally a few lines below for every reactivation --
        #     listed here only to record that fix #3's three-part
        #     requirement is fully satisfied, not because this branch needs
        #     to repeat it.
        # Verify round-7 fix #7: `challenge_attempts` reset alongside the
        # token/clock above. Without this, a fresh 24h GC window still
        # inherits the OLD backoff schedule (`backoff_delay` is a function
        # of `challenge_attempts`, workers/webhooks/dispatch_webhooks.py) --
        # a sub reactivated after e.g. 10 prior attempts starts its new 24h
        # window already backed off to a 6h cap, getting only ~4 more
        # attempts instead of the ~14 a genuinely fresh challenge gets.
        row.challenge_token = secrets.token_urlsafe(24)
        row.challenge_attempts = 0
        row.created_at = now
        # r12 fix #6 (grok 3): `challenge_attempted_at` (migration 0015,
        # probe-guarded like every other touch of this column -- see
        # `_challenge_attempted_at_column_exists`) is reset to NULL
        # alongside the token/clock/attempts above. `run_challenges`
        # rotates least-recently-attempted FIRST, NULLS FIRST -- leaving
        # this row's stale attempted-at timestamp in place after a
        # reactivation would put it back at the BACK of that rotation
        # instead of the front a genuinely fresh challenge gets, the same
        # starvation r11 fix #5 exists to prevent, just re-introduced via
        # this one code path fix #5 didn't touch.
        if _challenge_attempted_at_column_exists(db):
            db.execute(
                text("UPDATE webhook_subscriptions SET challenge_attempted_at = NULL WHERE id = :id"),
                {"id": row.id},
            )

    # `verified` is deliberately untouched -- reactivation never re-verifies.
    row.active = True
    row.consecutive_failures = 0
    row.failing_since = None
    row.next_attempt_at = None
    row.disabled_reason = None
    row.disabled_at = None
    # Verify round-3 fix #12: clear a STALE 'disabled' lifecycle notice --
    # this reactivation makes it moot (send_alerts.py would otherwise still
    # mail "your webhook was auto-disabled" for a subscription that is
    # active again by the time the nightly run drains it). Only the exact
    # value 'disabled' is cleared -- never 'created' (an unsent "webhook
    # created" notice from the ORIGINAL verification is unrelated to this
    # reactivation and must still go out) and never 'created_disabled'
    # (fix #13's combined notice -- reactivating does not un-happen the
    # auto-disable it is reporting; it still needs to be told about both).
    if row.notify_pending == "disabled":
        row.notify_pending = None
    db.commit()

    return WebhookReactivateResponse(
        id=row.id,
        active=True,
        mode=mode,
        meta={"api_version": "v1", "request_id": request.state.request_id},
    )
