"""/api/v1/billing -- Stripe checkout, portal, and webhook (2026-08-21
monetization spec Phase 2, `SPEC-LOCKED.md` R7/A1-A4/B1/B4-B7/C1-C10/D1-D7).

**One shared Stripe account, restricted key, `metadata.app` filter.** The
owner runs other sub-businesses (FLHQ) as Products on the SAME Stripe
account; every object this router creates is tagged
`metadata.app == "billcommons"`, and the webhook handler treats an event as
OURS only if that tag is found on the event's object or, failing that, on
its Stripe "parent" (subscription -> customer, invoice -> subscription ->
customer, charge -> payment_intent -> customer) -- amendment A2. Anything
that fails every lookup is `skipped_foreign_app`, never processed. A
genuine Stripe API error during one of those parent lookups is NOT the same
as "confirmed foreign" -- it raises, producing a 500 so Stripe retries
(amendment C4).

**Idempotency.** First statement on any webhook delivery is `INSERT INTO
stripe_events (id, type, payload) VALUES (...) ON CONFLICT (id) DO NOTHING
RETURNING id` -- no row back means this event id was already processed;
return `200` immediately without touching anything else. Everything else
for a NEW event runs in that same DB session/transaction, so if this
handler raises anywhere on the way to genuinely-500ing, the inserted
`stripe_events` row rolls back with it (nothing is ever left half-recorded)
-- `billcommons_api.deps.get_db` never calls `commit()` on your behalf, so
an uncommitted session that hits an exception and gets `.close()`d in the
`finally` discards everything written on it, stripe_events row included.

**Permanent vs. transient errors (D4).** `stripe.InvalidRequestError`
with code `resource_missing`, and "the Stripe customer object has no
email" (C6's on-the-fly customer creation needs an email; round-3 D4
supersedes C6's earlier "500, retry" draft for this specific case) are
PERMANENT: retrying will never fix them. Recorded as `stripe_events.outcome
= 'permanent_error'` + `last_error`, `200` returned (stop Stripe's
retries), and the operator is emailed immediately (D4 -- the daily digest
also surfaces every `permanent_error` row, per base spec's alerts worker,
extended separately). Every OTHER `stripe.error.StripeError` (rate limits,
connection errors, anything not classified above) is transient -- it
propagates and 500s.

**Checkout is guest-capable (C2).** Neither checkout endpoint requires the
`bc_session` cookie -- only the B7 Origin check. If a session cookie IS
present, the customer's existing `stripe_customer_id`/email is passed to
Stripe so Checkout doesn't ask for an email Stripe (or we) already have;
otherwise Stripe collects it and `checkout.session.completed`'s customer
upsert (by lower(email), A1) reconciles it afterward.

**2026-08-21 fix-pass additions (post-verify decisions E1-E4, SPEC-LOCKED):**

* **E1 (plan on churn).** `customer_plan()` is the ONE function that decides
  `api_keys.plan`: a customer's newest non-`_TERMINAL_SUB_STATUSES` row's
  plan if `status in ('active','trialing','past_due')`, else `"developer"`
  -- a `canceled`/`incomplete_expired`/absent subscription NEVER 402s, it
  just drops the customer to the free tier (item 1; supersedes the
  earlier "call `_recompute_plan_onto_keys` with the event's own plan"
  approach, which could rewrite a churned customer's keys back to a paid
  plan on a late-arriving event for an old subscription).
* **E2 (checkout abuse).** `/checkout` and `/checkout/snapshot` are
  unauthenticated writes against the Stripe account this app SHARES with
  FLHQ (C2) -- `QuotaMiddleware` exempts this whole router (A5) so its
  per-customer counters never apply here. `_checkout_rate_limit` enforces
  its OWN strict per-IP + per-/24 fixed window in front of both endpoints
  (item 4).
* **E3 (Stripe API version).** `stripe.api_version` is pinned explicitly at
  import time to the basil+ shape this file's handlers are written
  against (`invoice.parent.subscription_details.subscription`,
  `subscription.items.data[0].current_period_end` -- neither top-level
  field exists on `stripe==13.2.0`'s installed models, item 7).
* **E4 (webhook atomicity).** The handler never commits mid-transaction.
  `_send_magic_link`/`_notify_operator`/the buyer-facing order email are
  all queued via `BackgroundTasks` and run AFTER `stripe_webhook`'s own
  `db.commit()` (item 24) -- a send failure is logged, never a 500, and
  never rolls back anything already committed.

**2026-08-21 fix-pass round 2 (SPEC-LOCKED "Post-verify decisions round 2",
E6-E8; consolidated fixlist items 1-13):**

* **E6 (mint-on-first-paid, Gate A).** `checkout.session.completed` is no
  longer the only minter of a paid key -- `_ensure_provisioned_for_
  subscription()` is called from BOTH `_handle_checkout_session_completed`
  and `_apply_subscription_event`/`_handle_invoice_event`'s `invoice.paid`
  path, and mints the FIRST time a given subscription is observed reaching
  `active`/`trialing`, from whichever of those events arrives first (out-
  of-order-safe, same as A4's existing subscription-row idempotency).
  `incomplete`/`incomplete_expired` are NEVER paid states: `_BLOCKING_SUB_
  STATUSES` (== `_PLAN_AUTHORITY_STATUSES`) replaces the old "is it
  terminal?" predicate everywhere a checkout-conflict/duplicate-subscription
  probe or a mint decision is made (item 1), so an `incomplete` subscription
  no longer mints a paid key, no longer 409-locks a retry, no longer gets
  its guest retry's money captured-then-canceled by the duplicate branch,
  and `invoice.payment_failed` against a first invoice that never paid no
  longer promotes it to `past_due` (item 1(a)-(d)). Idempotent per
  subscription via `api_keys.subscription_id` (migration 0021) -- a race
  between the two triggering events mints exactly once.
* **E7 (misconfiguration is transient, Gate B -- rolls back round-1 item
  23).** `_configure_stripe()`/`_price_id()` raise `_ConfigurationError`
  (a plain exception, not `HTTPException`) instead of `bad_request(...)`:
  it propagates past every `except` clause in `stripe_webhook` to FastAPI's
  generic 500 handler, so Stripe retries (for up to 3 days) instead of the
  event being recorded `permanent_error` + `200`'d and then unreachable
  (Stripe won't redeliver a 200, and a Dashboard "Resend" reuses the same
  event id and is swallowed by the idempotency `ON CONFLICT DO NOTHING`).
  `permanent_error` is reserved for genuinely Stripe-confirmed-permanent
  conditions (`resource_missing`, email-less customer) -- never our own
  faults. A 500 here rolls the just-inserted `stripe_events` row back with
  the rest of the uncommitted transaction (same mechanism the module
  docstring above already describes for any other exception).
* **E8 (webhook transaction contract).** No helper called from inside
  `stripe_webhook` may `commit()` or `rollback()` its caller's transaction
  -- `_issue_login_token` (`routers/account.py`) takes a `commit: bool`
  parameter (default `True` for its own router's `request_magic_link`,
  `False` from `_send_magic_link` here) and otherwise only flushes. A
  failure inside it now propagates instead of being swallowed, so a
  transient DB error minting the login token rolls back everything the
  webhook wrote earlier in the same delivery (item 2) -- `request_magic_
  link` wraps its own call in its own `try/except` to keep its documented
  always-202 contract (item 10).
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Literal

import stripe
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from billcommons_api.api_keys import (
    KeyLimitExceeded,
    REVEAL_TOKEN_TTL,
    _PLAN_AUTHORITY_STATUSES,
    _aware,
    encrypt_for_reveal,
    invalidate,
    _invalidate_after_commit,
    mint_key,
    revoke_key,
)
from billcommons_api.deps import get_db
from billcommons_api.errors import bad_request, conflict, not_found, too_many_requests
from billcommons_api.rate_limit import _BoundedFixedWindowCounter, client_ip, subnet_bucket
from billcommons_api.routers.account import _check_origin, _send_email, _verify_session
from billcommons_schema.models import (
    ApiCustomer,
    ApiKey,
    ApiSubscription,
    SnapshotEntitlement,
    StripeEvent,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

_SESSION_COOKIE = "bc_session"

# E3/item 7: pin the API version this file's OUTBOUND calls (Checkout
# Session/Customer/Subscription/Refund create-or-modify, etc.) are written
# against. Set at import time, not inside `_configure_stripe()`, so it's
# asserted even before the first API key is configured.
#
# Item 13 fix (found in this fix pass -- no leg flagged it): this pin does
# NOT, as an earlier version of this comment and the runbook both
# incorrectly claimed, make WEBHOOK payload shapes stop being ambient --
# `stripe.api_version` is an SDK-local setting that only affects calls THIS
# process makes outward. An incoming webhook event is rendered at whatever
# API version is configured on the *Stripe Dashboard webhook endpoint*
# itself (or the account default if unset), which nothing in this file can
# influence. The handlers below are written against the basil+ shape
# (`invoice.parent.subscription_details.subscription`,
# `subscription.items.data[0].current_period_end`) with a pre-basil
# fallback specifically BECAUSE of that: the webhook endpoint's own
# Dashboard-configured version must be pinned separately (see the runbook's
# Stripe-setup section) and kept in sync with what these handlers parse.
stripe.api_version = "2025-03-31.basil"


def _site_url() -> str:
    return os.environ.get("BILLCOMMONS_PUBLIC_SITE_URL", "https://billcommons.org").rstrip("/")


def _operator_email() -> str | None:
    return os.environ.get("OPERATOR_ALERT_EMAIL")


def _notify_operator(background_tasks: BackgroundTasks | None, subject: str, body: str) -> None:
    """Item 24: queued via `BackgroundTasks` (runs after the response, in
    the threadpool, before worker shutdown) instead of a daemon `Thread`
    (killed at interpreter exit -- a deploy/restart mid-send silently drops
    the alert). `background_tasks` is `None` only for call sites this fix
    pass could not thread a request-scoped `BackgroundTasks` into (there
    are none left in this module); kept optional so a future non-request
    caller doesn't have to fake one."""
    to = _operator_email()
    if not to:
        logger.warning("OPERATOR_ALERT_EMAIL unset -- operator alert dropped: %s: %s", subject, body)
        return
    if background_tasks is None:
        _send_email(to, subject, body)
        return
    background_tasks.add_task(_send_email, to, subject, body)


def _stripe_cancel_already_terminal(exc: stripe.InvalidRequestError) -> bool:
    return getattr(exc, "code", None) == "resource_missing" or bool(
        re.search(r"already (been )?cancel|No such subscription|canceled subscription can only", str(exc), re.IGNORECASE)
    )


_PRICE_ENV = {
    ("builder", "monthly"): "STRIPE_PRICE_BUILDER_MONTHLY",
    ("builder", "annual"): "STRIPE_PRICE_BUILDER_ANNUAL",
    ("scale", "monthly"): "STRIPE_PRICE_SCALE_MONTHLY",
    ("scale", "annual"): "STRIPE_PRICE_SCALE_ANNUAL",
}

_SNAPSHOT_PRICE_ENV = {
    "state": "STRIPE_PRICE_SNAPSHOT_STATE",
    "full": "STRIPE_PRICE_SNAPSHOT_FULL",
}

# Item 2: reverse map -- {stripe_price_id: (plan, interval)}. Used to
# derive a subscription's CURRENT plan from its CURRENT price at call
# time, so a Portal plan switch (which changes `items[0].price` but never
# rewrites `subscription.metadata`) is reflected immediately instead of
# only at the next `metadata.plan`-stamped event. Recomputed from env on
# every call rather than cached -- six env lookups is not a hot path (one
# per webhook subscription event), and caching would go stale the moment
# an operator rotates a price id without restarting the process (and,
# separately, made this untestable across cases in the same process).
def _price_id_to_plan_map() -> dict[str, tuple[str, str]]:
    mapping: dict[str, tuple[str, str]] = {}
    for (plan, interval), env_name in _PRICE_ENV.items():
        price_id = os.environ.get(env_name)
        if price_id:
            mapping[price_id] = (plan, interval)
    return mapping


def _plan_from_subscription(sub: dict) -> str | None:
    """Item 2: prefer the subscription's CURRENT price id (reflects a
    Portal plan switch immediately); fall back to `metadata.plan` (stamped
    once at Checkout creation, never rewritten by a Portal switch) only
    when the price id is unrecognized (e.g. a Dashboard-created
    subscription with no matching env price -- logged, not raised, since
    this is a sync concern, not a request-blocking one)."""
    items = ((sub.get("items") or {}).get("data")) or []
    price_id = None
    if items:
        price_id = (items[0].get("price") or {}).get("id")
    if price_id:
        mapped = _price_id_to_plan_map().get(price_id)
        if mapped is not None:
            return mapped[0]
        logger.warning("subscription %s has unrecognized price id %s", sub.get("id"), price_id)
    metadata_plan = (sub.get("metadata") or {}).get("plan")
    if metadata_plan in ("builder", "scale", "enterprise"):
        return metadata_plan
    if metadata_plan is not None:
        logger.warning("subscription %s has invalid metadata plan %r", sub.get("id"), metadata_plan)
    return None


class _ConfigurationError(Exception):
    """E7 (SPEC-LOCKED "Post-verify decisions round 2", rolls back round-1
    item 23): our OWN misconfiguration -- a missing `STRIPE_SECRET_KEY` or
    price env var -- is TRANSIENT, not permanent: it self-heals the moment
    an operator sets the env var, and Stripe will keep retrying a genuine
    500 for up to 3 days. Deliberately NOT `HTTPException` -- item 23's
    `except Exception: ... isinstance(exc, HTTPException) -> permanent_
    error, 200` used to catch exactly the `bad_request(...)` this used to
    raise, burying every webhook delivered during a misconfiguration
    window (Stripe never redeploys after a 200, and a Dashboard "Resend"
    reuses the same event id, landing on the idempotency `ON CONFLICT DO
    NOTHING` as a silent duplicate). A plain exception here propagates
    straight past `stripe_webhook`'s `except _PermanentWebhookError` clause
    to FastAPI's generic 500 handler -- and, for the checkout/portal
    endpoints that also call these two functions, produces a clean 500
    instead of the old 400 `stripe_not_configured`: this is a SERVER fault
    either way, not a malformed request."""


def _configure_stripe() -> None:
    key = os.environ.get("STRIPE_SECRET_KEY")
    if not key:
        raise _ConfigurationError("STRIPE_SECRET_KEY is not set.")
    stripe.api_key = key
    # Item 6: `stripe==13.2.0` defaults to `max_network_retries=2` and an
    # 80s per-call HTTP timeout -- a single hung Stripe request could
    # otherwise hold the DB row lock this module takes for up to ~240s.
    # `requests` is already a transitive dependency (`stripe`'s own
    # default client when installed); a bounded 10s timeout with zero
    # SDK-level retries (Stripe's own retry-storm protection is unrelated
    # to ours -- a retried webhook delivery is Stripe's job, not this
    # client's) keeps that window small. Configured once per process, not
    # per call.
    if not isinstance(stripe.default_http_client, stripe._http_client.RequestsClient):
        stripe.default_http_client = stripe._http_client.RequestsClient(timeout=10)
    stripe.max_network_retries = 0


def _price_id(env_name: str) -> str:
    price_id = os.environ.get(env_name)
    if not price_id:
        raise _ConfigurationError(f"{env_name} is not set.")
    return price_id


def _customer_from_session(request: Request, db: OrmSession) -> ApiCustomer | None:
    cookie_value = request.cookies.get(_SESSION_COOKIE, "")
    if not cookie_value:
        return None
    customer_id = _verify_session(cookie_value)
    if customer_id is None:
        return None
    return db.execute(select(ApiCustomer).where(ApiCustomer.id == customer_id)).scalar_one_or_none()


# Item 3: `incomplete_expired` is a TERMINAL Stripe status (Checkout in
# subscription mode creates the subscription `incomplete` when the first
# payment needs action/fails, and it expires to `incomplete_expired` after
# ~23h) -- that row is never `canceled`, so treating only `canceled` as
# terminal 409'd a customer's retry forever while a guest retry's money was
# captured and then silently canceled+unrefunded by the duplicate branch.
# Also backs migration 0020's re-created partial unique index.
_TERMINAL_SUB_STATUSES = ("canceled", "incomplete_expired")

# E1 (SPEC-LOCKED post-verify decision, resolves item 0's A3-vs-B4
# conflict): a subscription in one of these statuses counts as the
# customer's PLAN authority; anything else (terminal, or no subscription
# at all) is Developer. `unpaid` intentionally stays a "still counts, but
# `payment_required()` 402s it" status -- distinct from terminal.
#
# Item 1 fix (round-2 fixlist, HIGH): this is the ONE set every "is this
# subscription live/blocking/mint-worthy?" question in this file must ask
# now -- not "is it terminal?" (`_TERMINAL_SUB_STATUSES`, which only ever
# covered `canceled`/`incomplete_expired` and left `incomplete` -- the
# status Stripe assigns while a brand-new subscription's first invoice is
# unpaid -- treated as live everywhere). An `incomplete` subscription is
# NEVER billing-authoritative: it must not 409 a Checkout retry
# (`_active_subscription`), must not trip the duplicate-subscription
# probe (`_apply_subscription_event`'s `existing_other`), must not mint a
# paid key (`_ensure_provisioned_for_subscription`), and must not be
# promoted to `past_due` by an `invoice.payment_failed` against its
# never-paid first invoice (`_handle_invoice_event`). `_BLOCKING_SUB_
# STATUSES` is an alias of this tuple -- same set, named for its OTHER use
# (checkout-conflict/duplicate-subscription blocking) so both call sites
# read intent-first.
_BLOCKING_SUB_STATUSES = _PLAN_AUTHORITY_STATUSES


def _active_subscription(db: OrmSession, customer_id: uuid.UUID) -> ApiSubscription | None:
    """Item 1 fix: was `.status.not_in(_TERMINAL_SUB_STATUSES)`, which
    treats `incomplete` as "active" -- 409-locking a customer's Checkout
    retry for the ~23h until Stripe expires the unpaid subscription, and
    pointing them at a Portal that cannot fix an unpaid first invoice.
    `incomplete` is not in `_BLOCKING_SUB_STATUSES`, so it no longer blocks
    a retry."""
    return db.execute(
        select(ApiSubscription)
        .where(ApiSubscription.customer_id == customer_id)
        .where(ApiSubscription.status.in_(_BLOCKING_SUB_STATUSES))
    ).scalars().first()


def customer_plan(db: OrmSession, customer_id: uuid.UUID) -> str:
    """Item 1 fix: THE authority for `api_keys.plan`. B4/E1: select
    directly on `_PLAN_AUTHORITY_STATUSES` (the partial unique index
    guarantees at most one BLOCKING row per customer, but a customer can
    ALSO have a non-blocking `incomplete`/`incomplete_expired`/`canceled`
    row coexisting with it since migration 0021 loosened the index -- this
    query must never let one of those non-authoritative rows win a
    `.first()` race against the one that matters); its plan if a
    plan-authority row exists, else `"developer"`.

    Trap for the implementer (fixlist item 1): this NEVER writes into
    `api_subscriptions.plan` -- that column keeps whatever paid plan the
    row was sold at (the CHECK constraint `ck_api_subscriptions_plan` only
    allows `builder`/`scale`/`enterprise`, `models.py:1035`-ish). Only the
    KEY's plan becomes `"developer"`.
    """
    row = db.execute(
        select(ApiSubscription)
        .where(ApiSubscription.customer_id == customer_id)
        .where(ApiSubscription.status.in_(_PLAN_AUTHORITY_STATUSES))
        .order_by(ApiSubscription.updated_at.desc())
    ).scalars().first()
    if row is not None:
        return row.plan
    return "developer"


# ---------------------------------------------------------------------------
# E2/item 4: checkout endpoints are unauthenticated Stripe-account writes --
# their own strict rate limiter, independent of QuotaMiddleware's exemption
# list (which must stay exempt for `/billing/webhook`'s signature-verified
# and `/billing/portal`'s cookie-authed paths).
# ---------------------------------------------------------------------------

_MAX_TRACKED_CHECKOUT_BUCKETS = 100_000


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


_checkout_ip_limiter = _BoundedFixedWindowCounter(
    _env_int("BILLCOMMONS_API_RATE_LIMIT_CHECKOUT", 5),
    60.0,
    time.monotonic,
    _MAX_TRACKED_CHECKOUT_BUCKETS,
)
_checkout_subnet_limiter = _BoundedFixedWindowCounter(
    _env_int("BILLCOMMONS_API_RATE_LIMIT_CHECKOUT_SUBNET", 20),
    60.0,
    time.monotonic,
    _MAX_TRACKED_CHECKOUT_BUCKETS,
)


def _enforce_checkout_rate_limit(request: Request) -> None:
    ip = client_ip(request)
    ip_ok, retry_after, *_ = _checkout_ip_limiter.allow(ip)
    if not ip_ok:
        raise too_many_requests(
            "rate_limited", "Too many checkout attempts -- please try again shortly.", retry_after
        )
    subnet_ok, retry_after, *_ = _checkout_subnet_limiter.allow(subnet_bucket(ip))
    if not subnet_ok:
        raise too_many_requests(
            "rate_limited", "Too many checkout attempts -- please try again shortly.", retry_after
        )


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class CheckoutRequest(BaseModel):
    plan: Literal["builder", "scale"]
    interval: Literal["monthly", "annual"]


class SnapshotCheckoutRequest(BaseModel):
    scope: Literal["state", "full"]
    # Item 25 (Phase-3 landmine): was a free `str | None` capped at 8
    # chars, uppercased and stored verbatim -- Phase 3's snapshot builder
    # is specified to turn this into a file path, and `"../AL"` fits in 8
    # characters. A real 2-letter jurisdiction code is the only valid
    # shape; anything else 422s here instead of reaching a filesystem path
    # later.
    jurisdiction: str | None = Field(default=None, pattern=r"^[A-Za-z]{2}$")


class CheckoutResponse(BaseModel):
    url: str


# ---------------------------------------------------------------------------
# Checkout / portal
# ---------------------------------------------------------------------------


def _stripe_customer_id_for_checkout(db: OrmSession, customer: ApiCustomer | None) -> str | None:
    """Route-only helper; MUST NOT be called from webhook handlers (E8).

    Item 17: A2 requires `metadata.app=billcommons` on the Stripe
    Customer object itself, not just the Checkout Session/subscription --
    this router never created one, relying on Checkout's own
    auto-created (untagged) Customer, so the customer-metadata parent
    fallback in `_has_app_metadata`/webhook resolution could never match
    one of our own customers. For a logged-in customer with no Stripe
    Customer yet, create one now, tagged, and persist its id; for a
    logged-in customer who already has one, tag it (idempotent) so an
    older, pre-fix Customer object gets the tag retroactively.

    Item 6 fix (two bugs):

    Defect A -- the docstring above already promised "persist its id", but
    the pre-fix version returned `remote["id"]` and never assigned
    `customer.stripe_customer_id` (and neither checkout route commits for
    you -- `get_db` doesn't). Every checkout attempt by a logged-in
    customer with no Stripe Customer created ANOTHER Stripe Customer on the
    shared account, and `POST /billing/portal` kept 400ing
    `no_stripe_customer` until a webhook reconciled by email. Fixed by
    assigning + flushing + committing here, before the Checkout Session is
    even created (accepting one orphan Stripe Customer if the Checkout call
    itself then fails -- cheaper than leaving the local row permanently
    stripe-customer-less).

    Defect B -- `stripe.Customer.modify(...)` was unguarded: a stale
    `stripe_customer_id` (Customer deleted in the Dashboard, or a test/live
    key swap) raised `InvalidRequestError` -> unhandled 500 on both
    checkout endpoints, with no self-service escape (the Portal fails on
    the same dead id). Now a `resource_missing` clears the stale column and
    falls through to creating a fresh, tagged Customer; any other Stripe
    error still propagates (transient, same convention as every other
    Stripe call in this file)."""
    if customer is None:
        return None
    if customer.stripe_customer_id:
        try:
            stripe.Customer.modify(customer.stripe_customer_id, metadata={"app": "billcommons"})
            return customer.stripe_customer_id
        except stripe.InvalidRequestError as exc:
            if getattr(exc, "code", None) != "resource_missing":
                raise
            customer.stripe_customer_id = None
    remote = stripe.Customer.create(email=customer.email, metadata={"app": "billcommons"})
    customer.stripe_customer_id = remote["id"]
    db.add(customer)
    db.flush()
    # Route endpoints own this commit; webhook handlers must never call
    # this helper because their Stripe-event ledger is one transaction.
    db.commit()
    return remote["id"]


@router.post("/checkout", response_model=CheckoutResponse)
def create_checkout(
    body: CheckoutRequest, request: Request, db: OrmSession = Depends(get_db)
) -> CheckoutResponse:
    """C2: guest-capable, Origin-checked only (plus E2/item 4's own strict
    rate limit -- this endpoint is an unauthenticated write against the
    shared Stripe account, `QuotaMiddleware` never sees it). A4/B4: 409 if
    the (optional) logged-in customer already has an active/non-terminal
    subscription (item 3: `incomplete_expired` counts as terminal, not
    just `canceled`) -- they should use the Portal to change plans, not
    open a second Checkout."""
    _check_origin(request)
    _enforce_checkout_rate_limit(request)
    _configure_stripe()
    price_id = _price_id(_PRICE_ENV[(body.plan, body.interval)])

    customer = _customer_from_session(request, db)
    if customer is not None and _active_subscription(db, customer.id) is not None:
        raise conflict(
            "active_subscription_exists",
            "You already have an active subscription. Manage it from your account page.",
        )

    kwargs: dict = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": f"{_site_url()}/account/welcome",
        "cancel_url": f"{_site_url()}/pricing",
        "allow_promotion_codes": True,
        "metadata": {"app": "billcommons", "plan": body.plan, "interval": body.interval},
        "subscription_data": {"metadata": {"app": "billcommons", "plan": body.plan}},
    }
    stripe_customer_id = _stripe_customer_id_for_checkout(db, customer)
    if stripe_customer_id:
        kwargs["customer"] = stripe_customer_id
    elif customer is not None:
        kwargs["customer_email"] = customer.email

    session = stripe.checkout.Session.create(**kwargs)
    return CheckoutResponse(url=session["url"])


@router.post("/checkout/snapshot", response_model=CheckoutResponse)
def create_snapshot_checkout(
    body: SnapshotCheckoutRequest, request: Request, db: OrmSession = Depends(get_db)
) -> CheckoutResponse:
    """R2/A10: one-time snapshot purchase, guest-capable (C2, plus E2's
    strict rate limit), no subscription conflict check -- a customer can
    buy any number of snapshots."""
    _check_origin(request)
    _enforce_checkout_rate_limit(request)
    _configure_stripe()
    if body.scope == "state" and not body.jurisdiction:
        raise bad_request("jurisdiction_required", "A state snapshot requires a jurisdiction code.")
    price_id = _price_id(_SNAPSHOT_PRICE_ENV[body.scope])

    customer = _customer_from_session(request, db)
    metadata = {"app": "billcommons", "scope": body.scope}
    if body.jurisdiction:
        metadata["jurisdiction"] = body.jurisdiction.upper()

    kwargs: dict = {
        "mode": "payment",
        # P0 fulfillment consumes checkout.session.completed only. Restrict
        # this Checkout Session to synchronous card payments so a delayed bank
        # debit cannot settle later without an async-payment webhook handler.
        "payment_method_types": ["card"],
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": f"{_site_url()}/account/welcome",
        "cancel_url": f"{_site_url()}/docs/bulk",
        "metadata": metadata,
        "payment_intent_data": {"metadata": metadata},
    }
    stripe_customer_id = _stripe_customer_id_for_checkout(db, customer)
    if stripe_customer_id:
        kwargs["customer"] = stripe_customer_id
    elif customer is not None:
        kwargs["customer_email"] = customer.email

    session = stripe.checkout.Session.create(**kwargs)
    return CheckoutResponse(url=session["url"])


@router.post("/portal", response_model=CheckoutResponse)
def create_portal_session(request: Request, db: OrmSession = Depends(get_db)) -> CheckoutResponse:
    """Cookie-authed (unlike Checkout -- managing an existing subscription
    requires knowing WHICH customer you are)."""
    _check_origin(request)
    _configure_stripe()
    customer = _customer_from_session(request, db)
    if customer is None:
        raise not_found("session_required", "Log in via a magic link first.")
    if not customer.stripe_customer_id:
        raise bad_request("no_stripe_customer", "This account has no billing history yet.")

    session = stripe.billing_portal.Session.create(
        customer=customer.stripe_customer_id,
        return_url=f"{_site_url()}/account",
    )
    return CheckoutResponse(url=session["url"])


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


class _PermanentWebhookError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _cancel_subscription_or_classify(
    subscription_id: str, background_tasks: BackgroundTasks | None
) -> None:
    """Cancel at Stripe, stopping only terminal and permanent request errors.

    Connection, rate-limit, and general Stripe API errors deliberately
    propagate so Stripe retries the webhook and its transaction rolls back.
    """
    try:
        stripe.Subscription.cancel(subscription_id)
    except stripe.InvalidRequestError as exc:
        if _stripe_cancel_already_terminal(exc):
            logger.warning("stripe.Subscription.cancel(%s) was already terminal", subscription_id)
            return
        _notify_operator(
            background_tasks,
            "Bill Commons: Stripe subscription cancel request rejected",
            f"subscription {subscription_id}: {exc}",
        )
        raise _PermanentWebhookError(f"Stripe subscription cancel rejected for {subscription_id}: {exc}") from exc


def _has_app_metadata(obj: dict | None) -> bool:
    return bool(obj) and (obj.get("metadata") or {}).get("app") == "billcommons"


def _has_app_metadata_with_customer_fallback(obj: dict | None, customer_stripe_id: str | None) -> bool:
    """Item 17: A2 says an event is OURS if the object's own metadata
    carries the tag OR its Stripe "parent" does (subscription -> customer).
    Pre-fix, the `customer.subscription.*` webhook branches checked only
    `_has_app_metadata(obj)` with no parent fallback, contradicting this
    file's own module docstring. A real Stripe customer lookup here is a
    genuine API call (same permanent/transient split as everywhere else in
    this file: `resource_missing` -> permanent, anything else -> raise so
    Stripe retries)."""
    if _has_app_metadata(obj):
        return True
    if not customer_stripe_id:
        return False
    try:
        remote_customer = stripe.Customer.retrieve(customer_stripe_id)
    except stripe.InvalidRequestError as exc:
        if getattr(exc, "code", None) == "resource_missing":
            raise _PermanentWebhookError(f"resource_missing: customer {customer_stripe_id}") from exc
        raise
    return _has_app_metadata(remote_customer)


def _stripe_customer_holds_plan_authority(stripe_customer_id: str) -> bool:
    """Item 7 helper: does `stripe_customer_id` have at least one
    subscription in a `_PLAN_AUTHORITY_STATUSES` state? Used to decide
    whether an INCOMING Checkout Customer id is the one that should
    become a local row's billing-authoritative id (see
    `_upsert_customer_by_email` below)."""
    for status in _PLAN_AUTHORITY_STATUSES:
        subs = stripe.Subscription.list(customer=stripe_customer_id, status=status, limit=1)
        if subs.get("data"):
            return True
    return False


def _upsert_customer_by_email(db: OrmSession, email: str, stripe_customer_id: str | None) -> ApiCustomer:
    """A1: upsert key is lower(email).

    Item 7 fix (SPEC-TENSION, A1's "overwrite" narrowed): A1 literally said
    "attach/overwrite `stripe_customer_id`" whenever Checkout supplies a
    different one -- but Stripe guest Checkout creates a NEW Customer per
    session even when the email matches an existing Customer, so
    overwriting unconditionally re-points a subscriber's local row at a
    Customer that does not hold their subscription (worst case: the C9
    duplicate-cancel flow's now-canceled duplicate Customer) -- "Manage
    billing" then opens a Portal with no subscription in it. Narrowed to:
    attach when the local column is empty (unchanged, common case);
    otherwise only repoint when the OLD id is provably dead (404s at
    Stripe) or the INCOMING Customer is the one that actually holds a
    plan-authority subscription *and the local customer has no incumbent
    plan-authority subscription*.  The latter guard is important: an
    active guest Checkout Customer necessarily holds its just-created
    subscription too, but it must not replace the incumbent Customer id
    before `_apply_subscription_event` cancels that duplicate. Otherwise
    the Billing Portal would point at the canceled guest Customer. Keep the
    existing id in that case; the incoming Customer is a guest duplicate or
    snapshot-only purchase, not the subscriber's real one."""
    _configure_stripe()
    email = email.strip().lower()
    customer = db.execute(select(ApiCustomer).where(ApiCustomer.email == email)).scalar_one_or_none()
    if customer is None:
        customer = ApiCustomer(email=email, stripe_customer_id=stripe_customer_id)
        db.add(customer)
        db.flush()
        return customer
    if not stripe_customer_id or customer.stripe_customer_id == stripe_customer_id:
        return customer
    if not customer.stripe_customer_id:
        customer.stripe_customer_id = stripe_customer_id
        return customer
    old_is_stale = False
    try:
        remote = stripe.Customer.retrieve(customer.stripe_customer_id)
        old_is_stale = remote.get("deleted") is True
    except stripe.InvalidRequestError as exc:
        if getattr(exc, "code", None) != "resource_missing":
            raise
        old_is_stale = True
    # Preserve recovery when the old Stripe Customer was deleted, even if a
    # stale local subscription row still exists.  Otherwise the LOCAL
    # authority is decisive before inspecting the incoming Customer: both
    # sides of a duplicate guest Checkout can be active in Stripe, but only
    # the incumbent is the portal/customer mapping we must retain.
    if old_is_stale or (
        _active_subscription(db, customer.id) is None
        and _stripe_customer_holds_plan_authority(stripe_customer_id)
    ):
        customer.stripe_customer_id = stripe_customer_id
    return customer


def _customer_by_stripe_id(db: OrmSession, stripe_customer_id: str) -> ApiCustomer | None:
    return db.execute(
        select(ApiCustomer).where(ApiCustomer.stripe_customer_id == stripe_customer_id)
    ).scalar_one_or_none()


def _stripe_customer_or_create(db: OrmSession, stripe_customer_id: str) -> ApiCustomer:
    """C6: any subscription/invoice event whose customer has no local
    `api_customers` row creates one on the fly from the Stripe Customer's
    email -- never deferred. D4 (supersedes C6's own draft): a Stripe
    customer with no email is a PERMANENT error, not a 500-and-retry.

    Item 14 fix: `_configure_stripe()` FIRST -- `stripe.api_key` is a
    module global set only there, and every OTHER Stripe-touching path in
    this file configures before its first call. This one didn't, so a
    freshly restarted process whose first Stripe touch is a subscription
    webhook for a not-yet-locally-known customer (A4's out-of-order
    delivery case) hit `AuthenticationError("No API key provided")`."""
    _configure_stripe()
    local = _customer_by_stripe_id(db, stripe_customer_id)
    if local is not None:
        return local
    try:
        remote = stripe.Customer.retrieve(stripe_customer_id)
    except stripe.InvalidRequestError as exc:
        if getattr(exc, "code", None) == "resource_missing":
            raise _PermanentWebhookError(f"resource_missing: customer {stripe_customer_id}") from exc
        raise
    email = remote.get("email")
    if not email:
        raise _PermanentWebhookError(f"stripe customer {stripe_customer_id} has no email")
    return _upsert_customer_by_email(db, email, stripe_customer_id)


def _recompute_plan_onto_keys(db: OrmSession, customer_id: uuid.UUID, plan: str) -> None:
    """B4/item 1: `api_keys.plan` is derived. Callers MUST pass
    `customer_plan(db, customer_id)` -- never a Stripe event's own
    `metadata.plan` -- so a late-arriving event for an OLD/churned
    subscription can never rewrite plan onto keys governed by the
    customer's CURRENT billing state. Writes onto ALL usable
    (active/rotating) keys of that customer in one pass."""
    keys = db.execute(
        select(ApiKey).where(ApiKey.customer_id == customer_id, ApiKey.status.in_(("active", "rotating")))
    ).scalars().all()
    for key in keys:
        if key.plan != plan:
            key.plan = plan
            invalidate(key.key_hash)
            _invalidate_after_commit(db, key.key_hash)


def _mint_paid_key_with_reveal(
    db: OrmSession,
    customer: ApiCustomer,
    plan: str,
    background_tasks: BackgroundTasks | None,
    subscription_id: uuid.UUID | None = None,
) -> ApiKey:
    """B1: the checkout-minted key is never revealed inline -- it is
    Fernet-encrypted into `reveal_ciphertext` (24h TTL) and the customer
    gets a magic-link email, never the plaintext key itself. D5: if the
    3-usable ceiling is already full, the oldest active key is
    auto-revoked to make room (the digest notes it -- Phase 2 doesn't wire
    the digest itself, this comment documents the intended behavior).

    Item 21 fix: the revocation candidate query used to look for
    `status == "active"` only, but the ceiling counts `active` AND
    `rotating` (`api_keys.MAX_USABLE_KEYS_PER_CUSTOMER`) -- with zero
    active and three rotating keys, `oldest` was always `None` and the
    bare `raise` re-raised `KeyLimitExceeded` unclassified, 500-looping
    Stripe's retries on a permanently-unprovisionable event. Now widened
    to `active`/`rotating`, oldest-first, and re-raised as
    `_PermanentWebhookError` (recorded + alerted, never retried forever)
    when there is still nothing to revoke.

    E6/Gate A: `subscription_id` (migration 0021) links the minted key to
    the `api_subscriptions` row it was minted FOR, so `_ensure_provisioned_
    for_subscription` can make "did we already mint a key for THIS
    subscription" idempotent per subscription rather than per customer --
    two racing events (`checkout.session.completed` and a subscription
    webhook) observing the same subscription's first `active`/`trialing`
    transition must mint exactly once."""
    try:
        row, full_key = mint_key(db, customer.id, environment="live", plan=plan)
    except KeyLimitExceeded:
        oldest = db.execute(
            select(ApiKey)
            .where(ApiKey.customer_id == customer.id, ApiKey.status.in_(("active", "rotating")))
            .order_by(ApiKey.created_at.asc())
        ).scalars().first()
        if oldest is None:
            raise _PermanentWebhookError(
                f"customer {customer.id} at the key ceiling with nothing revocable"
            )
        revoke_key(db, oldest.id, customer.id)
        _notify_operator(
            background_tasks,
            "Bill Commons: key ceiling reached",
            f"Customer {customer.email} hit the usable-key ceiling; auto-revoked {oldest.key_prefix}.",
        )
        row, full_key = mint_key(db, customer.id, environment="live", plan=plan)
    row.reveal_ciphertext = encrypt_for_reveal(full_key)
    row.reveal_expires_at = datetime.now(timezone.utc) + REVEAL_TOKEN_TTL
    row.subscription_id = subscription_id
    db.flush()
    return row


def _customer_has_usable_key(db: OrmSession, customer_id: uuid.UUID) -> bool:
    now = datetime.now(timezone.utc)
    return (
        db.execute(
            select(ApiKey.id).where(
                ApiKey.customer_id == customer_id,
                (ApiKey.status == "active")
                | ((ApiKey.status == "rotating") & (ApiKey.revoke_at > now)),
            )
        ).first()
        is not None
    )


@dataclass(frozen=True)
class _ProvisioningResult:
    minted: bool
    link_queued: bool

    def __bool__(self) -> bool:
        raise TypeError("Read _ProvisioningResult.minted or .link_queued explicitly")


def _send_magic_link(
    db: OrmSession,
    customer: ApiCustomer,
    background_tasks: BackgroundTasks | None,
    request_ip: str = "webhook",
) -> None:
    from billcommons_api.routers.account import _issue_login_token

    # E8: `commit=False` -- this call sits INSIDE the webhook's own single
    # all-or-nothing transaction (the default caller of this function).
    # `_issue_login_token` now raises rather than swallowing a failure, so
    # a transient DB error here propagates all the way out of
    # `stripe_webhook`, rolling back everything the webhook wrote earlier
    # in the same delivery (fixlist item 2) instead of silently discarding
    # it under a `200 processed`.
    token = _issue_login_token(db, customer.email, request_ip, commit=False)
    link = f"{_site_url()}/account/login?token={token}"
    subject = "Your Bill Commons account is ready"
    body = f"Sign in to reveal your API key and manage billing: {link}\n\nExpires in 15 minutes."
    if background_tasks is None:
        _send_email(customer.email, subject, body)
    else:
        background_tasks.add_task(_send_email, customer.email, subject, body)


def _ensure_provisioned_for_subscription(
    db: OrmSession,
    customer: ApiCustomer,
    subscription: ApiSubscription,
    background_tasks: BackgroundTasks | None,
    *,
    send_magic_link: bool = True,
) -> _ProvisioningResult:
    """E6/Gate A (SPEC-LOCKED "Post-verify decisions round 2"): mint the
    paid key + send the magic link the FIRST time `subscription` is
    observed reaching `active`/`trialing`, from WHICHEVER event notices
    the transition first -- `_apply_subscription_event` (a `customer.
    subscription.*` webhook), `_handle_checkout_session_completed`
    (`checkout.session.completed`), or `_handle_invoice_event`
    (`invoice.paid`) may all call this for the same subscription; only the
    first call does anything. Returns `(minted, link_queued)`: `minted` is
    true only when this call created the paid key, while `link_queued` is
    true only when this call also queued a magic-link email.

    `incomplete`/`incomplete_expired` (and anything else outside
    `_PLAN_AUTHORITY_STATUSES`, e.g. `canceled`) are NEVER paid states --
    the guard below is the single place that decision is made for minting,
    matching `_BLOCKING_SUB_STATUSES`'s use everywhere else in this file.

    Idempotent two ways: (1) per SUBSCRIPTION, via `api_keys.subscription_
    id` (migration 0021) -- a key already minted for this exact
    subscription short-circuits regardless of which triggering event races
    in first; (2) per CUSTOMER (E5, unchanged): if the customer already has
    ANY other usable key (e.g. a Developer key from an earlier login), no
    second key is minted at all -- `_recompute_plan_onto_keys` (called by
    every subscription-event caller) upgrades that existing key's plan in
    place instead, avoiding key sprawl on a repeat/renewal event."""
    # Every activation path must serialize on this same customer row. The
    # subscription event path already holds it; PostgreSQL treats that
    # repeated lock as re-entrant, while checkout and invoice.paid acquire
    # it here before their check-then-mint sequence.
    db.execute(select(ApiCustomer.id).where(ApiCustomer.id == customer.id).with_for_update())

    if subscription.status not in ("active", "trialing"):
        return _ProvisioningResult(False, False)
    already_for_sub = db.execute(
        select(ApiKey.id).where(ApiKey.subscription_id == subscription.id)
    ).first()
    if already_for_sub is not None:
        return _ProvisioningResult(False, False)
    if _customer_has_usable_key(db, customer.id):
        return _ProvisioningResult(False, False)
    try:
        # A savepoint confines a losing unique-index race to this mint. The
        # webhook's outer transaction (including its idempotency row) stays
        # usable for the re-check below.
        with db.begin_nested():
            _mint_paid_key_with_reveal(
                db, customer, subscription.plan, background_tasks, subscription_id=subscription.id
            )
    except IntegrityError:
        if db.execute(select(ApiKey.id).where(ApiKey.subscription_id == subscription.id)).first() is None:
            raise
        return _ProvisioningResult(False, False)
    if send_magic_link:
        _send_magic_link(db, customer, background_tasks)
        return _ProvisioningResult(True, True)
    return _ProvisioningResult(True, False)


def _live_subscription_status(sub_id: str) -> str | None:
    """R8 (row-None swap guard) helper: current status of `sub_id` at Stripe,
    or `None` if Stripe no longer knows about it. Only `InvalidRequestError`
    (missing subscription) is swallowed; every other Stripe error propagates
    so the webhook retries instead of silently trusting a possibly-transient
    failure."""
    _configure_stripe()
    try:
        return stripe.Subscription.retrieve(sub_id).get("status")
    except stripe.InvalidRequestError:
        return None


def _apply_subscription_event(
    db: OrmSession,
    customer: ApiCustomer,
    sub: dict,
    event_created: int,
    background_tasks: BackgroundTasks | None,
    *,
    send_magic_link: bool = True,
) -> str:
    """A4/B3/B4/D2/D1: upserts `api_subscriptions` by `stripe_subscription_id`,
    applying the event only if newer than the last one applied. C9/D2: a
    SECOND active subscription for the same customer is refused -- the new
    one is canceled at Stripe and RECORDED locally as canceled (item 8, see
    below) so follow-up events for it take the normal update path instead
    of re-entering this branch forever.

    Item 1/E1 fix: `api_keys.plan` is written from `customer_plan(customer)`
    -- computed AFTER this row is upserted -- never from this event's own
    `plan`. A late-arriving event for an OLD subscription (e.g. a stale
    `customer.subscription.updated` for a subscription the customer has
    since canceled and replaced) can no longer rewrite paid quota onto keys
    a churned customer shouldn't have."""
    sub_id = sub["id"]

    metadata_plan = (sub.get("metadata") or {}).get("plan")
    items = ((sub.get("items") or {}).get("data")) or []
    price_id = (items[0].get("price") or {}).get("id") if items else None
    invalid_metadata_plan = metadata_plan is not None and metadata_plan not in ("builder", "scale", "enterprise") and not (
        price_id and price_id in _price_id_to_plan_map()
    )

    db.execute(select(ApiCustomer.id).where(ApiCustomer.id == customer.id).with_for_update())

    row = db.execute(
        select(ApiSubscription).where(ApiSubscription.stripe_subscription_id == sub_id)
    ).scalar_one_or_none()
    incoming_status = sub.get("status", "active")
    # Decide freshness before any incumbent-swap side effect.  A stale
    # active event for a canceled former subscription must not cancel the
    # customer's current dunning subscription.
    if row is not None and row.last_event_created_at is not None:
        last_ts = int(_aware(row.last_event_created_at).timestamp())
        if event_created < last_ts:
            return "processed"  # D1: stale event, ignored
        # Stripe's `created` has one-second resolution.  On a tie, a
        # terminal transition wins over a non-terminal resurrection.
        if (
            event_created == last_ts
            and row.status in _TERMINAL_SUB_STATUSES
            and incoming_status not in _TERMINAL_SUB_STATUSES
        ):
            return "processed"
    if invalid_metadata_plan and (row is None or incoming_status not in _TERMINAL_SUB_STATUSES):
        logger.warning("ignoring subscription %s with invalid metadata plan %r", sub_id, metadata_plan)
        return "ignored_invalid_plan"

    # Item 1 fix: was `.status.not_in(_TERMINAL_SUB_STATUSES)`, which
    # treated an `incomplete` OTHER subscription as blocking -- a guest
    # retry (`sub_B`) while an earlier attempt (`sub_A`) sat `incomplete`
    # entered THIS branch and had its money captured, then canceled+
    # unrefunded, with no local record. `incomplete` is not in
    # `_BLOCKING_SUB_STATUSES`, so only a genuinely billing-authoritative
    # OTHER subscription trips the duplicate probe now.
    incumbent_to_cancel: str | None = None
    existing_other = db.execute(
        select(ApiSubscription)
        .where(ApiSubscription.customer_id == customer.id)
        .where(ApiSubscription.stripe_subscription_id != sub_id)
        .where(ApiSubscription.status.in_(_BLOCKING_SUB_STATUSES))
    ).scalars().first()
    swap_needed = (
        existing_other is not None
        and existing_other.status in ("past_due", "unpaid")
        and incoming_status in ("active", "trialing")
    )
    # R8 (row-None swap guard): when this Stripe subscription id has never
    # been seen locally (`row is None`), the staleness check above -- gated
    # on `row is not None` -- never ran, so a delayed/out-of-order
    # active|trialing event for a subscription that is ALREADY canceled at
    # Stripe would otherwise sail straight into the incumbent swap below and
    # cancel the customer's live past_due/unpaid subscription. Consult
    # Stripe directly for this one row-None case before touching the
    # incumbent; if it's no longer active/trialing there, leave the
    # incumbent alone -- the newcomer's own terminal event (already
    # delivered or still in flight) handles its own row.
    if swap_needed and row is None:
        live = _live_subscription_status(sub_id)
        if live not in ("active", "trialing"):
            logger.warning(
                "ignoring stale swap-triggering event for subscription %s: live Stripe status is %r",
                sub_id,
                live,
            )
            return "processed"

    # R7 fix (finding #1): the incumbent-swap savepoint must stay open
    # across the newcomer's own write AND the deferred incumbent Stripe
    # cancel below -- a `_PermanentWebhookError` raised by that cancel call
    # (or by the duplicate-handling cancel inside the `except IntegrityError`
    # branch) must unwind the WHOLE swap, not just the piece that happened
    # to be flushed in its own now-already-closed savepoint. When no swap is
    # in play (`swap_needed` False), `incumbent_to_cancel` never gets set and
    # this is a no-op context, behaving exactly as before.
    with (db.begin_nested() if swap_needed else contextlib.nullcontext()):
        if swap_needed:
            existing_other.status = "canceled"
            existing_other.past_due_since = None
            watermark = datetime.fromtimestamp(event_created, tz=timezone.utc)
            if existing_other.last_event_created_at is None or _aware(existing_other.last_event_created_at) < watermark:
                existing_other.last_event_created_at = watermark
            # Flush the old authoritative row before the newcomer is added
            # or activated: PostgreSQL checks the partial unique index per
            # flush.  The incoming row is written and flushed below before
            # the Stripe cancel is issued, so any failure rolls the
            # complete local swap back and retry redoes both sides.
            db.flush()
            incumbent_to_cancel = existing_other.stripe_subscription_id
            existing_other = None

        if existing_other is not None and row is None:
            # D2: only refuse when this Stripe subscription id has never been
            # seen locally before -- a resync/update of an ALREADY-accepted
            # subscription is not a duplicate.
            _configure_stripe()
            if sub.get("status") not in _TERMINAL_SUB_STATUSES:
                # Item 8 fix: `stripe.Subscription.cancel` on a subscription
                # Stripe already considers terminal (a re-delivery of this same
                # duplicate's OWN follow-up events, before the local record
                # below existed) raises `InvalidRequestError` -- mapped to a
                # no-op here instead of propagating to an unclassified 500
                # that Stripe would retry forever.
                _cancel_subscription_or_classify(sub_id, background_tasks)
            # Item 8 fix: record the duplicate locally as `canceled` -- the
            # partial unique index permits it (status IS in
            # `_TERMINAL_SUB_STATUSES`) -- so Stripe's follow-up
            # `customer.subscription.updated`/`.deleted` events for this same
            # `sub_id` find `row is not None` and take the normal update path
            # instead of re-entering this branch (and re-calling `.cancel` on
            # an already-canceled subscription) forever.
            db.add(
                ApiSubscription(
                    customer_id=customer.id,
                    stripe_subscription_id=sub_id,
                    plan=_plan_from_subscription(sub) or "builder",
                    status="canceled",
                    last_event_created_at=datetime.fromtimestamp(event_created, tz=timezone.utc),
                )
            )
            db.flush()
            _notify_operator(
                background_tasks,
                "Bill Commons: duplicate subscription canceled",
                f"Customer {customer.email} already had an active subscription; "
                f"canceled the newer Stripe subscription {sub_id}. If they were "
                f"charged, issue a refund from the Stripe Dashboard (C9).",
            )
            # C9: email the CUSTOMER too, not just the operator.
            buyer_subject = "About your Bill Commons subscription"
            buyer_body = (
                "You already had an active Bill Commons subscription, so we canceled "
                "the duplicate one before it was fully set up. If you were charged for "
                "it, reply to this email and we'll refund it right away."
            )
            if background_tasks is None:
                _send_email(customer.email, buyer_subject, buyer_body)
            else:
                background_tasks.add_task(_send_email, customer.email, buyer_subject, buyer_body)
            return "duplicate_subscription_canceled"

        now = datetime.now(timezone.utc)
        status = sub.get("status", "active")
        if row is not None and existing_other is not None and status in _BLOCKING_SUB_STATUSES:
            _configure_stripe()
            _cancel_subscription_or_classify(sub_id, background_tasks)
            row.status = "canceled"
            row.past_due_since = None
            watermark = datetime.fromtimestamp(event_created, tz=timezone.utc)
            if row.last_event_created_at is None or _aware(row.last_event_created_at) < watermark:
                row.last_event_created_at = watermark
            db.flush()
            _notify_operator(
                background_tasks,
                "Bill Commons: duplicate subscription canceled",
                f"Customer {customer.email} already had an active subscription; canceled {sub_id}.",
            )
            return "duplicate_subscription_canceled"
        # Item 2 fix: prefer the subscription's CURRENT price id (reflects a
        # Portal plan switch immediately -- Stripe never rewrites
        # `subscription.metadata` when `items[0].price` changes there); fall
        # back to `metadata.plan` (stamped once at Checkout creation) only when
        # the price id doesn't match a known plan.
        plan = _plan_from_subscription(sub) or (row.plan if row is not None else "builder")
        # Item 7/E3 fix: read both the pre-basil top-level shape (this file's
        # own test fixtures/replayed old events) and the basil+ shape
        # (`items.data[0].current_period_end` -- `stripe==13.2.0` has no
        # top-level `Subscription.current_period_end` at all).
        period_end_ts = sub.get("current_period_end")
        if period_end_ts is None:
            items = ((sub.get("items") or {}).get("data")) or []
            if items:
                period_end_ts = items[0].get("current_period_end")
        period_end = datetime.fromtimestamp(period_end_ts, tz=timezone.utc) if period_end_ts else None
        cancel_at_period_end = bool(sub.get("cancel_at_period_end", False))

        try:
            with db.begin_nested():
                if row is None:
                    row = ApiSubscription(
                        customer_id=customer.id,
                        stripe_subscription_id=sub_id,
                        plan=plan,
                        status=status,
                        current_period_end=period_end,
                        cancel_at_period_end=cancel_at_period_end,
                    )
                    db.add(row)
                else:
                    row.plan = plan
                    row.status = status
                    row.current_period_end = period_end
                    row.cancel_at_period_end = cancel_at_period_end
                if status == "past_due" and row.past_due_since is None:
                    row.past_due_since = now
                elif status != "past_due":
                    row.past_due_since = None
                watermark = datetime.fromtimestamp(event_created, tz=timezone.utc)
                if row.last_event_created_at is None or _aware(row.last_event_created_at) < watermark:
                    row.last_event_created_at = watermark
                db.flush()
        except IntegrityError:
            # A concurrent or late transition may have made another
            # subscription authoritative after the first probe.  Re-check and
            # route it through the duplicate policy instead of returning a bare
            # 500 that Stripe would redeliver forever.
            existing_other = db.execute(
                select(ApiSubscription)
                .where(ApiSubscription.customer_id == customer.id)
                .where(ApiSubscription.stripe_subscription_id != sub_id)
                .where(ApiSubscription.status.in_(_BLOCKING_SUB_STATUSES))
            ).scalars().first()
            if existing_other is None:
                raise
            _configure_stripe()
            _cancel_subscription_or_classify(sub_id, background_tasks)
            if row not in db:
                db.add(row)
            row.status = "canceled"
            row.past_due_since = None
            watermark = datetime.fromtimestamp(event_created, tz=timezone.utc)
            if row.last_event_created_at is None or _aware(row.last_event_created_at) < watermark:
                row.last_event_created_at = watermark
            db.flush()
            _notify_operator(
                background_tasks,
                "Bill Commons: duplicate subscription canceled",
                f"Customer {customer.email} already had an active subscription; canceled {sub_id}.",
            )
            # R7 fix (finding #2): a duplicate found on the NEWCOMER's own
            # upsert must not silently swallow an already-decided incumbent
            # swap -- the incumbent still needs its deferred Stripe cancel,
            # or it bills forever with no local record ever revisiting it.
            if incumbent_to_cancel is not None:
                _configure_stripe()
                _cancel_subscription_or_classify(incumbent_to_cancel, background_tasks)
                _notify_operator(
                    background_tasks,
                    "Bill Commons: dunning subscription canceled for new paid subscription",
                    f"Customer {customer.email} had dunning subscription {incumbent_to_cancel}; "
                    f"canceled it so {sub_id} can become authoritative.",
                )
            return "duplicate_subscription_canceled"

        if incumbent_to_cancel is not None:
            # Stripe is deliberately last in the incumbent/newcomer swap: both
            # local rows have been flushed successfully before this
            # irreversible call, inside the SAME savepoint opened above --
            # a `_PermanentWebhookError` here unwinds the entire swap (both
            # rows) rather than leaving the incumbent `canceled` locally
            # while still live at Stripe. A transient failure rolls back the
            # outer webhook transaction and Stripe retries the full swap
            # (there are no mid-webhook commits either way).
            _configure_stripe()
            _cancel_subscription_or_classify(incumbent_to_cancel, background_tasks)
            _notify_operator(
                background_tasks,
                "Bill Commons: dunning subscription canceled for new paid subscription",
                f"Customer {customer.email} had dunning subscription {incumbent_to_cancel}; "
                f"canceled it so {sub_id} can become authoritative.",
            )

    # C3/item 9 fix: the pre-fix condition was `if status == "canceled":
    # ... elif plan == "scale":` -- a Scale -> Builder DOWNGRADE arrives as
    # `status="active", plan="builder"` and matched NEITHER branch, so the
    # customer kept a full-corpus `kind='subscription'` entitlement with no
    # expiry while paying Builder rates. Inverted: expire whenever the
    # subscription is terminal OR no longer Scale; create/extend only when
    # it's live AND Scale.
    if status in _TERMINAL_SUB_STATUSES or plan != "scale":
        # Bound through the ORM `update()` construct (not a raw
        # `text(":cid")` bind of `str(customer.id)`) so the mapped
        # `UUID(as_uuid=True)` column's bind processor handles the
        # dialect-specific encoding -- `_monetization_sqlite.py`'s own
        # module docstring warns about exactly this: the SQLite harness's
        # `gen_random_uuid()` shim stores `.hex` (32 chars, no dashes),
        # so a raw-SQL `str(uuid4())` (hyphenated) bind NEVER matches a
        # row there, even though the same code is silently correct against
        # Postgres. Caught by this fix pass's own item 9 regression test.
        db.execute(
            sa_update(SnapshotEntitlement)
            .where(
                SnapshotEntitlement.customer_id == customer.id,
                SnapshotEntitlement.kind == "subscription",
                (SnapshotEntitlement.expires_at.is_(None)) | (SnapshotEntitlement.expires_at > now),
            )
            .values(expires_at=now)
        )
    elif status in _PLAN_AUTHORITY_STATUSES:
        # Item 10 fix: the pre-fix probe selected on `kind == 'subscription'`
        # with no `expires_at` filter, so a canceled-then-resubscribed Scale
        # customer's EXPIRED row (stamped by the branch above on their
        # earlier cancel) always matched, and the resubscribe silently
        # skipped creating a fresh entitlement.
        has_active = db.execute(
            select(SnapshotEntitlement.id).where(
                SnapshotEntitlement.customer_id == customer.id,
                SnapshotEntitlement.kind == "subscription",
                (SnapshotEntitlement.expires_at.is_(None)) | (SnapshotEntitlement.expires_at > now),
            )
        ).first()
        if has_active is None:
            db.add(
                SnapshotEntitlement(
                    customer_id=customer.id,
                    kind="subscription",
                    scope="full",
                )
            )

    _recompute_plan_onto_keys(db, customer.id, customer_plan(db, customer.id))
    # E6/Gate A: this is one of the three "activation path" call sites
    # (alongside `_handle_checkout_session_completed` and `invoice.paid`)
    # that can be the FIRST to observe this subscription reach
    # active/trialing -- idempotent, see `_ensure_provisioned_for_
    # subscription`'s own docstring.
    _ensure_provisioned_for_subscription(
        db, customer, row, background_tasks, send_magic_link=send_magic_link
    )
    return "processed"


def _invoice_subscription_id(invoice: dict) -> str | None:
    """Item 7/E3 fix: `2025-03-31.basil` removed `Invoice.subscription`
    (moved to `invoice.parent.subscription_details.subscription`).
    `stripe==13.2.0`'s installed models have no top-level field at all, so
    reading only the pre-basil key made `invoice.payment_failed` a no-op
    forever (the dunning anchor never got set from this path -- the
    `customer.subscription.updated(status='past_due')` path is the belt to
    this suspenders, per the fixlist's own honest-scope note). Reads both
    shapes so old test fixtures / replayed pre-basil events keep working
    too."""
    sub_id = invoice.get("subscription")
    if sub_id:
        return sub_id
    parent = invoice.get("parent") or {}
    sub_details = parent.get("subscription_details") or {}
    return sub_details.get("subscription")


def _resolve_invoice_ownership(db: OrmSession, invoice: dict) -> tuple[bool, ApiCustomer | None]:
    """A2/C4/C6: `invoice.*` events look at `invoice.subscription` first,
    else `invoice.customer`. Local DB membership (this app is the only
    writer of `stripe_subscription_id`/`stripe_customer_id`) is checked
    FIRST as a fast, mock-free path; only when that fails do we fall back
    to a real Stripe API lookup (whose failure is C4's 500, not a silent
    'foreign')."""
    sub_id = _invoice_subscription_id(invoice)
    if sub_id:
        local_sub = db.execute(
            select(ApiSubscription).where(ApiSubscription.stripe_subscription_id == sub_id)
        ).scalar_one_or_none()
        if local_sub is not None:
            customer = db.execute(
                select(ApiCustomer).where(ApiCustomer.id == local_sub.customer_id)
            ).scalar_one_or_none()
            return True, customer

    customer_id = invoice.get("customer")
    if customer_id:
        local_customer = _customer_by_stripe_id(db, customer_id)
        if local_customer is not None:
            return True, local_customer

    # Nothing local -- fall back to a real Stripe lookup (C4: an API error
    # here raises, it is not treated as "confirmed foreign").
    _configure_stripe()
    if sub_id:
        try:
            remote_sub = stripe.Subscription.retrieve(sub_id)
        except stripe.InvalidRequestError as exc:
            if getattr(exc, "code", None) == "resource_missing":
                raise _PermanentWebhookError(f"resource_missing: subscription {sub_id}") from exc
            raise
        if _has_app_metadata(remote_sub):
            customer = _stripe_customer_or_create(db, remote_sub["customer"])
            return True, customer
    if customer_id:
        try:
            remote_customer = stripe.Customer.retrieve(customer_id)
        except stripe.InvalidRequestError as exc:
            if getattr(exc, "code", None) == "resource_missing":
                raise _PermanentWebhookError(f"resource_missing: customer {customer_id}") from exc
            raise
        if _has_app_metadata(remote_customer):
            customer = _stripe_customer_or_create(db, customer_id)
            return True, customer
    return False, None


def _retrieve_subscription_or_permanent(sub_id: str) -> dict:
    """Item 15 fix: `stripe.Subscription.retrieve` in
    `_handle_checkout_session_completed` was the only `retrieve` call in
    this file NOT wrapped in the `resource_missing` -> permanent-error
    mapping every other Stripe lookup here uses -- a checkout session
    whose subscription was deleted between checkout and delivery 500'd
    forever instead of recording `outcome='permanent_error'` (D4)."""
    try:
        return stripe.Subscription.retrieve(sub_id)
    except stripe.InvalidRequestError as exc:
        if getattr(exc, "code", None) == "resource_missing":
            raise _PermanentWebhookError(f"resource_missing: subscription {sub_id}") from exc
        raise


def _handle_checkout_session_completed(
    db: OrmSession, session: dict, event_created: int, background_tasks: BackgroundTasks | None
) -> str:
    if not _has_app_metadata(session):
        return "skipped_foreign_app"

    email = (session.get("customer_details") or {}).get("email") or session.get("customer_email")
    if not email:
        raise _PermanentWebhookError("checkout session completed with no email")
    customer = _upsert_customer_by_email(db, email, session.get("customer"))

    mode = session.get("mode", "subscription")
    if mode == "payment":
        # Item 16 fix: a delayed payment method (e.g. certain bank debits)
        # can complete Checkout with `payment_status="unpaid"` -- money not
        # yet received. Provision only once it's actually paid; otherwise
        # record the event and wait for `checkout.session.
        # async_payment_succeeded` (not yet wired -- add it to the webhook
        # event list if delayed methods are ever enabled, per the runbook).
        if session.get("payment_status") != "paid":
            return "processed"
        return _handle_snapshot_payment(db, session, customer, background_tasks)

    # mode == "subscription": provision against the subscription's CURRENT
    # state. Amendment A4 gate: if `customer.subscription.updated` already
    # landed (out of order) and created the `api_subscriptions` row, use
    # ITS state rather than re-deriving from this (possibly older) event --
    # `checkout.session.completed` never re-applies subscription state,
    # only provisions (E6/Gate A).
    sub_id = session.get("subscription")
    outcome = "processed"
    row: ApiSubscription | None = None
    if sub_id:
        row = db.execute(
            select(ApiSubscription).where(ApiSubscription.stripe_subscription_id == sub_id)
        ).scalar_one_or_none()
        if row is None:
            _configure_stripe()
            remote_sub = _retrieve_subscription_or_permanent(sub_id)
            # Checkout itself sends exactly one link below.  Do not let the
            # subscription sync it invokes in this same request queue a
            # second one (R4-14).
            outcome = _apply_subscription_event(
                db, customer, remote_sub, event_created, background_tasks, send_magic_link=False
            )
            row = db.execute(
                select(ApiSubscription).where(ApiSubscription.stripe_subscription_id == sub_id)
            ).scalar_one_or_none()

    if outcome == "duplicate_subscription_canceled":
        return outcome

    # E6/Gate A: `_ensure_provisioned_for_subscription` is the single place
    # that decides whether THIS subscription is paid-authoritative enough
    # to mint (`active`/`trialing`) and whether a key has already been
    # minted for it (idempotent per subscription AND per customer, see its
    # own docstring). Item 16's original concern -- a brand-new
    # subscription's first invoice can end `incomplete` (e.g. 3DS still
    # pending) while `checkout.session.completed` still fires -- is now
    # handled by that guard rather than a separate terminal-status check
    # here: an `incomplete` row simply does not provision, from EITHER
    # trigger, until whichever event (a subscription webhook or
    # `invoice.paid`) first observes it reach `active`/`trialing`.
    if not sub_id or row is None:
        raise _PermanentWebhookError(
            f"checkout completed but subscription not provisionable: subscription={sub_id!r}"
        )
    provisioning = _ensure_provisioned_for_subscription(db, customer, row, background_tasks)
    # A completed Checkout is an explicit purchase. Even if it upgraded an
    # existing Developer key in place, send one login link; subscription and
    # invoice events deliberately retain mint-only email behavior.
    if not provisioning.link_queued:
        _send_magic_link(db, customer, background_tasks)
    return outcome


def _handle_snapshot_payment(
    db: OrmSession, session: dict, customer: ApiCustomer, background_tasks: BackgroundTasks | None
) -> str:
    """R2/C10: a one-time snapshot purchase mints a Developer key only if
    the customer has zero usable keys, records a `snapshot_entitlements`
    row, emails the FOUNDER the order (R2 -- manual fulfillment until
    Phase 3) and the buyer a "we're preparing your file" message."""
    db.execute(select(ApiCustomer.id).where(ApiCustomer.id == customer.id).with_for_update())
    metadata = session.get("metadata") or {}
    scope = metadata.get("scope", "full")
    jurisdiction = metadata.get("jurisdiction")
    payment_intent_id = session.get("payment_intent")

    # Item 22 fix: `uq_snapshot_entitlements_payment_intent` (migration
    # 0019) is a partial unique index on `stripe_payment_intent_id WHERE
    # NOT NULL` -- a plain `db.add` + flush would raise `IntegrityError` on
    # a second insert for the same payment intent (a resend under a
    # different Stripe event id, a manual replay, or a future
    # `async_payment_succeeded` handler for the same purchase), which
    # `_handle_charge_refunded`'s `.scalar_one_or_none()` read would then
    # see as `MultipleResultsFound` had it slipped through. `ON CONFLICT DO
    # NOTHING` makes the resend a harmless no-op instead.
    dialect_name = db.get_bind().dialect.name
    insert_fn = pg_insert if dialect_name == "postgresql" else sqlite_insert
    db.execute(
        insert_fn(SnapshotEntitlement)
        .values(
            customer_id=customer.id,
            kind="one_time",
            scope=scope,
            jurisdiction=jurisdiction,
            stripe_checkout_session_id=session.get("id"),
            stripe_payment_intent_id=payment_intent_id,
        )
        .on_conflict_do_nothing(
            index_elements=["stripe_payment_intent_id"],
            index_where=text("stripe_payment_intent_id IS NOT NULL"),
        )
    )

    if not _customer_has_usable_key(db, customer.id):
        _mint_paid_key_with_reveal(db, customer, "developer", background_tasks)
    _send_magic_link(db, customer, background_tasks)

    _notify_operator(
        background_tasks,
        "Bill Commons: new snapshot order",
        f"{customer.email} bought a {scope} snapshot"
        + (f" ({jurisdiction})" if jurisdiction else "")
        + f". Fulfill within one business day per docs/operations/monetization-runbook.md "
        f"(checkout session {session.get('id')}).",
    )
    subject = "Your Bill Commons snapshot order"
    body = (
        "Thanks for your order -- we're preparing your file and will email a "
        "download link within one business day."
    )
    if background_tasks is None:
        _send_email(customer.email, subject, body)
    else:
        background_tasks.add_task(_send_email, customer.email, subject, body)
    return "processed"


def _handle_invoice_event(
    db: OrmSession, invoice: dict, event_type: str, event_created: int, background_tasks: BackgroundTasks | None
) -> str:
    is_ours, customer = _resolve_invoice_ownership(db, invoice)
    if not is_ours or customer is None:
        return "skipped_foreign_app"

    sub_id = _invoice_subscription_id(invoice)
    if not sub_id:
        return "processed"  # one-off invoice, nothing to sync
    row = db.execute(
        select(ApiSubscription).where(ApiSubscription.stripe_subscription_id == sub_id)
    ).scalar_one_or_none()
    if row is None:
        # Item 18 fix: Stripe makes no cross-event-type ordering guarantee
        # -- `invoice.payment_failed` can arrive before `customer.
        # subscription.created` for the same subscription, and the pre-fix
        # `return "processed"` here silently dropped the dunning anchor
        # (the later `.created` then wrote `status="active"` while the
        # customer was actually past_due at Stripe). Synthesize the row the
        # same way C6 synthesizes a customer: retrieve the subscription and
        # run it through the normal upsert path first.
        _configure_stripe()
        try:
            remote_sub = stripe.Subscription.retrieve(sub_id)
        except stripe.InvalidRequestError as exc:
            if getattr(exc, "code", None) == "resource_missing":
                logger.warning(
                    "invoice %s references subscription %s, which no longer exists -- "
                    "dropping the dunning signal",
                    invoice.get("id"),
                    sub_id,
                )
                return "processed"
            raise
        _apply_subscription_event(db, customer, remote_sub, event_created, background_tasks)
        row = db.execute(
            select(ApiSubscription).where(ApiSubscription.stripe_subscription_id == sub_id)
        ).scalar_one_or_none()
        if row is None:
            return "processed"

    # D1: the ordering guard covers invoice events too.
    if row.last_event_created_at is not None and event_created < int(_aware(row.last_event_created_at).timestamp()):
        return "processed"

    now = datetime.now(timezone.utc)
    # R4-5: invoice.paid can be the first delivery that tries to make a
    # locally incomplete subscription authoritative.  Do the same
    # duplicate-subscription check as `_apply_subscription_event` before
    # that write can trip the partial unique index.
    incoming_blocking = event_type == "invoice.paid" or (
        event_type == "invoice.payment_failed" and row.status != "incomplete"
    )
    incumbent_to_cancel: str | None = None
    existing_other = None
    swap_needed = False
    if incoming_blocking:
        existing_other = db.execute(
            select(ApiSubscription)
            .where(ApiSubscription.customer_id == customer.id)
            .where(ApiSubscription.stripe_subscription_id != sub_id)
            .where(ApiSubscription.status.in_(_BLOCKING_SUB_STATUSES))
        ).scalars().first()
        swap_needed = (
            existing_other is not None
            and existing_other.status in ("past_due", "unpaid")
            and event_type == "invoice.paid"
            and row.status not in _TERMINAL_SUB_STATUSES
        )

    # R7 fix (finding #1): keep the incumbent-swap savepoint open across
    # the newcomer's own write AND the deferred incumbent Stripe cancel
    # below -- a `_PermanentWebhookError` from that cancel (or from the
    # duplicate-handling cancel in the `except IntegrityError` branch) must
    # unwind the WHOLE swap, not just whatever happened to already be
    # flushed in its own now-closed savepoint. When `swap_needed` is False,
    # `incumbent_to_cancel` never gets set and this is a no-op context.
    with (db.begin_nested() if swap_needed else contextlib.nullcontext()):
        if swap_needed:
            existing_other.status = "canceled"
            existing_other.past_due_since = None
            watermark = datetime.fromtimestamp(event_created, tz=timezone.utc)
            if existing_other.last_event_created_at is None or _aware(existing_other.last_event_created_at) < watermark:
                existing_other.last_event_created_at = watermark
            # Make the incumbent terminal before this invoice activates
            # the newcomer.  The incoming row is also flushed below
            # before Stripe is canceled, so any failure rolls back
            # the complete local swap for a clean retry.
            db.flush()
            incumbent_to_cancel = existing_other.stripe_subscription_id
            existing_other = None
        if incoming_blocking and existing_other is not None:
            _configure_stripe()
            _cancel_subscription_or_classify(sub_id, background_tasks)
            row.status = "canceled"
            row.past_due_since = None
            watermark = datetime.fromtimestamp(event_created, tz=timezone.utc)
            if row.last_event_created_at is None or _aware(row.last_event_created_at) < watermark:
                row.last_event_created_at = watermark
            db.flush()
            _notify_operator(
                background_tasks,
                "Bill Commons: duplicate subscription canceled",
                f"Customer {customer.email} already had an active subscription; canceled {sub_id}.",
            )
            return "duplicate_subscription_canceled"
        # Item 11 fix: `_handle_invoice_event` and `_apply_subscription_event`
        # share one `last_event_created_at` watermark -- an `invoice.paid`
        # whose `created` lands after the `customer.subscription.deleted` that
        # preceded it (both legitimate, just delivered close together) used to
        # flip a CANCELED subscription back to "active" unconditionally, which
        # then re-blocked the customer's next Checkout with a 409 and
        # re-granted paid quota through item 1's recompute.
        #
        # Item 5 fix: was `row.status != "canceled"` -- the file's own
        # `_TERMINAL_SUB_STATUSES` also covers `incomplete_expired`; only a
        # subscription event may revive either terminal state.
        try:
            with db.begin_nested():
                if row.status not in _TERMINAL_SUB_STATUSES:
                    if event_type == "invoice.payment_failed":
                        # Item 1(d)/E6 fix: `incomplete` is NEVER a paid state -- the
                        # first invoice on a brand-new subscription failing is the
                        # NORMAL case for a customer who never successfully paid, not
                        # dunning against someone who once did. Promoting it to
                        # `past_due` (which IS plan-authoritative) would make
                        # `customer_plan()` grant paid quota, and `payment_required()`
                        # would wait a further 7 days before 402ing a customer who
                        # never paid a cent -- self-healing only ~23h later when Stripe
                        # expires the subscription to `incomplete_expired`. Only a
                        # subscription that was ALREADY billing-authoritative (or
                        # already dunning) can be pushed into dunning by an invoice
                        # event.
                        if row.status != "incomplete":
                            if row.status != "past_due" or row.past_due_since is None:
                                row.past_due_since = now
                            row.status = "past_due"
                    elif event_type == "invoice.paid":
                        row.status = "active"
                        row.past_due_since = None
                watermark = datetime.fromtimestamp(event_created, tz=timezone.utc)
                if row.last_event_created_at is None or _aware(row.last_event_created_at) < watermark:
                    row.last_event_created_at = watermark
                db.flush()
        except IntegrityError:
            # Belt-and-braces for a concurrent activation that raced the probe.
            existing_other = db.execute(
                select(ApiSubscription)
                .where(ApiSubscription.customer_id == customer.id)
                .where(ApiSubscription.stripe_subscription_id != sub_id)
                .where(ApiSubscription.status.in_(_BLOCKING_SUB_STATUSES))
            ).scalars().first()
            if existing_other is None:
                raise
            _configure_stripe()
            _cancel_subscription_or_classify(sub_id, background_tasks)
            row.status = "canceled"
            row.past_due_since = None
            watermark = datetime.fromtimestamp(event_created, tz=timezone.utc)
            if row.last_event_created_at is None or _aware(row.last_event_created_at) < watermark:
                row.last_event_created_at = watermark
            db.flush()
            _notify_operator(
                background_tasks,
                "Bill Commons: duplicate subscription canceled",
                f"Customer {customer.email} already had an active subscription; canceled {sub_id}.",
            )
            # R7 fix (finding #2): the newcomer's own upsert hitting a
            # duplicate must not silently swallow an already-decided
            # incumbent swap -- the incumbent still needs its deferred
            # Stripe cancel, or it bills forever with no record revisiting
            # it.
            if incumbent_to_cancel is not None:
                _configure_stripe()
                _cancel_subscription_or_classify(incumbent_to_cancel, background_tasks)
                _notify_operator(
                    background_tasks,
                    "Bill Commons: dunning subscription canceled for new paid subscription",
                    f"Customer {customer.email} had dunning subscription {incumbent_to_cancel}; "
                    f"canceled it so {sub_id} can become authoritative.",
                )
            return "duplicate_subscription_canceled"
        if incumbent_to_cancel is not None:
            # Both sides of this invoice-led swap have flushed inside the
            # SAME savepoint opened above -- a `_PermanentWebhookError` here
            # unwinds the entire swap rather than leaving the incumbent
            # `canceled` locally while still live at Stripe. A transient
            # failure rolls back the outer webhook transaction and Stripe
            # retries the full swap either way.
            _configure_stripe()
            _cancel_subscription_or_classify(incumbent_to_cancel, background_tasks)
            _notify_operator(
                background_tasks,
                "Bill Commons: dunning subscription canceled for new paid subscription",
                f"Customer {customer.email} had dunning subscription {incumbent_to_cancel}; "
                f"canceled it so {sub_id} can become authoritative.",
            )
    # E6/Gate A: `invoice.paid` is an explicit activation-path trigger --
    # if it lands before any `customer.subscription.*` event observes this
    # subscription reaching `active` (a plausible Stripe delivery order),
    # this is the first place that transition is visible. Idempotent, see
    # `_ensure_provisioned_for_subscription`'s own docstring.
    if event_type == "invoice.paid":
        _recompute_plan_onto_keys(db, customer.id, customer_plan(db, customer.id))
        _ensure_provisioned_for_subscription(db, customer, row, background_tasks)
    return "processed"


def _refund_cancel_access_flag(charge: dict) -> bool:
    """Item 12 fix: the pre-fix version read `refunds.data[-1]` -- Stripe
    list objects are ordered NEWEST-first, so that was the OLDEST refund on
    the charge, the opposite of what the runbook's "set
    `cancel_access='true'` on the refund you're issuing now" procedure
    assumes. A charge with a prior goodwill refund therefore ignored an
    explicit "cut access" instruction on a later refund (and the mirror
    case cut access on an unrelated earlier partial refund).

    Two more bugs fixed in the same line: `charge.refunds` is an
    `Optional[ListObject]` that isn't guaranteed to be embedded in every
    webhook payload (may need expansion) -- fetch it explicitly when
    absent; and `.get("metadata", {})` returns `None`, not `{}`, when
    Stripe sends `"metadata": null` (`dict.get` only supplies the default
    when the key is MISSING) -- every other refund-metadata read in this
    file already uses the `(... or {})` idiom, this one didn't.

    Item 12 round-2 fix (this function's own `any()` over the whole list
    was itself a leftover bug): only the NEWEST refund's flag is
    consulted, not `any()` across every refund ever issued on the charge.
    `any()` meant a LATER goodwill refund on a charge whose EARLIER refund
    carried `cancel_access="true"` re-triggered cancellation off the stale
    flag -- harmless for the SAME subscription (`_active_subscription`
    already returns `None` once it's locally canceled) but capable of
    canceling a re-subscribed customer's brand-new subscription. Reading
    only `refunds[0]` (Stripe lists are newest-first, per the docstring
    above) fixes both directions at once. Second gap closed: Stripe embeds
    at most 10 refunds with `has_more` -- a truncated embedded list is now
    detected and re-fetched via the explicit `stripe.Refund.list` call
    (still newest-first) instead of silently trusting whatever happened to
    be attached to the event payload."""
    refunds_obj = charge.get("refunds")
    refunds = (refunds_obj or {}).get("data")
    has_more = bool((refunds_obj or {}).get("has_more"))
    if refunds is None or has_more:
        payment_intent_id = charge.get("payment_intent")
        if not payment_intent_id:
            # Keep the embedded newest-first refund when Stripe omitted the
            # payment intent. A charge with more than 100 refunds remains
            # outside this handler's deliberately bounded scope.
            if refunds:
                return (refunds[0].get("metadata") or {}).get("cancel_access") == "true"
            return False
        _configure_stripe()
        refunds = stripe.Refund.list(payment_intent=payment_intent_id, limit=100).get("data") or []
    if not refunds:
        return False
    newest = refunds[0]
    return (newest.get("metadata") or {}).get("cancel_access") == "true"


def _handle_charge_refunded(
    db: OrmSession, charge: dict, event_created: int, background_tasks: BackgroundTasks | None
) -> str:
    _configure_stripe()
    payment_intent_id = charge.get("payment_intent")
    cancel_access = _refund_cancel_access_flag(charge)

    # A7/B5/C5: one-time snapshot entitlement, matched by payment_intent.
    # Item 22 fix: `.scalars().first()`, not `.scalar_one_or_none()` --
    # the latter raises `MultipleResultsFound` (a 500 retry loop) the
    # instant two rows ever share a payment_intent; the partial unique
    # index (0019/migration 0020's docstring) should make that
    # unreachable now, but this read no longer ASSUMES it.
    if payment_intent_id:
        entitlement = db.execute(
            select(SnapshotEntitlement).where(
                SnapshotEntitlement.stripe_payment_intent_id == payment_intent_id
            )
        ).scalars().first()
        if entitlement is not None:
            if entitlement.delivered_at is None:
                entitlement.expires_at = datetime.now(timezone.utc)
                _notify_operator(
                    background_tasks,
                    "Bill Commons: snapshot refunded (undelivered) -- entitlement revoked",
                    f"payment_intent {payment_intent_id}",
                )
            else:
                # C5: refund policy is the delivery rule -- after delivery,
                # a refund does nothing except notify the operator.
                _notify_operator(
                    background_tasks,
                    "Bill Commons: snapshot refunded (already delivered)",
                    f"payment_intent {payment_intent_id} -- no entitlement change; "
                    "file was already sent.",
                )
            return "processed"

    # C8/D3: subscription refund. Access continues unless the operator
    # explicitly set metadata.cancel_access="true" on the Refund in the
    # Dashboard.
    is_ours, customer = _resolve_invoice_ownership(db, {"customer": charge.get("customer")})
    is_tagged = _has_app_metadata(charge)
    remote_intent = None
    if not is_ours and payment_intent_id:
        remote_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
        is_tagged = is_tagged or _has_app_metadata(remote_intent)
        sessions = stripe.checkout.Session.list(payment_intent=payment_intent_id, limit=1).get("data") or []
        if sessions:
            checkout_session = sessions[0]
            is_tagged = is_tagged or _has_app_metadata(checkout_session)
            email = (checkout_session.get("customer_details") or {}).get("email")
            if email:
                customer = db.execute(
                    select(ApiCustomer).where(ApiCustomer.email == email.strip().lower())
                ).scalar_one_or_none()
                # A guest-email match alone is not ownership evidence on a
                # shared Stripe account: both the app tag and email match
                # are required before this refund may touch local access.
                is_ours = is_tagged and customer is not None
    if not is_ours or customer is None:
        # Stripe may deliver a guest-checkout refund before the matching
        # checkout completion has created our local customer.  Its own
        # metadata is sufficient to establish ownership, but not enough to
        # safely process it yet: raise so Stripe retries after checkout.
        if is_tagged:
            # The 72-hour cutoff intentionally uses the Stripe event's
            # `created` timestamp, not the local receipt time.
            if event_created <= int((datetime.now(timezone.utc) - timedelta(hours=72)).timestamp()):
                if cancel_access:
                    _notify_operator(
                        background_tasks,
                        "Bill Commons: manual access revocation required",
                        f"payment_intent {payment_intent_id!r} could not be mapped after 72 hours.",
                    )
                    raise _PermanentWebhookError("manual access revocation required for unresolved cancel_access refund")
                _notify_operator(
                    background_tasks,
                    "Bill Commons: refund skipped after unresolved guest checkout",
                    f"payment_intent {payment_intent_id!r} could not be mapped to a local customer after 72 hours.",
                )
                return "skipped_unresolvable_refund"
            raise RuntimeError("Bill Commons refund arrived before checkout provisioning")
        return "skipped_foreign_app"

    _notify_operator(
        background_tasks,
        "Bill Commons: subscription charge refunded",
        f"customer {customer.email}, cancel_access={cancel_access}",
    )
    if cancel_access:
        metadata = charge.get("metadata") or {}
        invoice = charge.get("invoice")
        if isinstance(invoice, dict):
            sub_id = _invoice_subscription_id(invoice)
        elif invoice:
            remote_invoice = stripe.Invoice.retrieve(invoice)
            sub_id = _invoice_subscription_id(remote_invoice)
        else:
            sub_id = None
        sub_id = sub_id or metadata.get("subscription_id") or metadata.get("stripe_subscription_id")
        row = (
            db.execute(
                select(ApiSubscription).where(
                    ApiSubscription.stripe_subscription_id == sub_id,
                    ApiSubscription.customer_id == customer.id,
                )
            ).scalar_one_or_none()
            if sub_id
            else None
        )
        if sub_id and row is None:
            _notify_operator(
                background_tasks,
                "Bill Commons: cancel_access refund subscription mismatch",
                f"customer {customer.email}, subscription={sub_id!r}, payment_intent={payment_intent_id!r}",
            )
            raise _PermanentWebhookError("refund_subscription_mismatch")
        if row is None and not sub_id:
            candidates = db.execute(
                select(ApiSubscription).where(
                    ApiSubscription.customer_id == customer.id,
                    ApiSubscription.status.not_in(_TERMINAL_SUB_STATUSES),
                )
            ).scalars().all()
            if len(candidates) == 1:
                row = candidates[0]
        if row is None:
            # No id was derivable and no non-terminal subscription remains.
            # Revoke any subscription entitlement instead of ending a
            # cancel_access refund with no access change.
            candidates = db.execute(
                select(ApiSubscription.id).where(
                    ApiSubscription.customer_id == customer.id,
                    ApiSubscription.status.not_in(_TERMINAL_SUB_STATUSES),
                )
            ).all()
            if not candidates:
                # R7 fix (finding #3): every other terminal branch in this
                # function stales-checks `event_created` against a
                # subscription row's `last_event_created_at` before
                # mutating state; this branch has no such row to check
                # (there are none left). Use the entitlement's own
                # `created_at` as the equivalent watermark instead -- a
                # delayed/retried refund event must not expire an
                # entitlement that was granted or extended (e.g. a manual
                # comp or re-provision) AFTER the refund it's replaying.
                now = datetime.now(timezone.utc)
                # Stripe's `created` has one-second resolution (see the D1
                # watermark comment above `_apply_subscription_event`) --
                # an entitlement granted in the SAME wall-clock second as
                # the refund event (sub-second `created_at` vs the
                # truncated-to-the-second `event_ts`) must not be treated
                # as "created after" it, or an entitlement seeded moments
                # before a same-second webhook replay never expires.
                event_ts = datetime.fromtimestamp(event_created, tz=timezone.utc)
                db.execute(
                    sa_update(SnapshotEntitlement)
                    .where(
                        SnapshotEntitlement.customer_id == customer.id,
                        SnapshotEntitlement.kind == "subscription",
                        (SnapshotEntitlement.expires_at.is_(None)) | (SnapshotEntitlement.expires_at > now),
                        SnapshotEntitlement.created_at < event_ts + timedelta(seconds=1),
                    )
                    .values(expires_at=now)
                )
                _recompute_plan_onto_keys(db, customer.id, customer_plan(db, customer.id))
                return "processed"
            _notify_operator(
                background_tasks,
                "Bill Commons: cancel_access refund could not be mapped to a subscription",
                f"customer {customer.email}, payment_intent={payment_intent_id!r}",
            )
            raise _PermanentWebhookError("cancel_access refund could not be mapped to a subscription")
        if row.last_event_created_at is not None and event_created < int(_aware(row.last_event_created_at).timestamp()):
            return "processed"
        _cancel_subscription_or_classify(row.stripe_subscription_id, background_tasks)
        row.status = "canceled"
        row.past_due_since = None
        watermark = datetime.fromtimestamp(event_created, tz=timezone.utc)
        if row.last_event_created_at is None or _aware(row.last_event_created_at) < watermark:
            row.last_event_created_at = watermark
        # `SessionLocal` intentionally uses autoflush=False.  Flush the
        # canceled state before `customer_plan()` queries it, otherwise
        # the old paid row remains visible and key plans are not dropped.
        db.flush()
        remaining_authority = db.execute(
            select(ApiSubscription.id).where(
                ApiSubscription.customer_id == customer.id,
                ApiSubscription.status.in_(_PLAN_AUTHORITY_STATUSES),
            )
        ).first()
        if remaining_authority is None:
            now = datetime.now(timezone.utc)
            db.execute(
                sa_update(SnapshotEntitlement)
                .where(
                    SnapshotEntitlement.customer_id == customer.id,
                    SnapshotEntitlement.kind == "subscription",
                    (SnapshotEntitlement.expires_at.is_(None)) | (SnapshotEntitlement.expires_at > now),
                )
                .values(expires_at=now)
            )
        _recompute_plan_onto_keys(db, customer.id, customer_plan(db, customer.id))
    return "processed"


async def _read_raw_body(request: Request) -> bytes:
    """Item 6 fix (half 1 of 2): resolved on the event loop (this is the
    only place in the request path that still needs `await`), so
    `stripe_webhook` itself can be a plain `def` -- FastAPI/Starlette then
    runs THAT in the threadpool, which is what actually gets every
    blocking Stripe SDK call and every SQLAlchemy call in this handler off
    the event loop."""
    return await request.body()


@router.post("/webhook")
def stripe_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    raw_body: bytes = Depends(_read_raw_body),
    db: OrmSession = Depends(get_db),
):
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    try:
        event = stripe.Webhook.construct_event(raw_body, sig_header, webhook_secret)
    except (ValueError, stripe.SignatureVerificationError):
        return JSONResponse(status_code=400, content={"error": {"code": "bad_signature", "message": "Invalid webhook signature."}})

    event_id = event["id"]
    event_type = event["type"]
    event_created = int(event.get("created", 0))
    obj = event["data"]["object"]

    payload_expression = "CAST(:payload AS jsonb)" if db.get_bind().dialect.name == "postgresql" else ":payload"
    inserted = db.execute(
        text(
            f"INSERT INTO stripe_events (id, type, payload) VALUES (:id, :type, {payload_expression}) "
            "ON CONFLICT (id) DO NOTHING RETURNING id"
        ),
        {"id": event_id, "type": event_type, "payload": json.dumps(dict(event))},
    ).first()
    if inserted is None:
        return JSONResponse(status_code=200, content={"received": True, "duplicate": True})

    outcome = "processed"
    last_error: str | None = None
    try:
        # Item 14: configure once up front -- every branch below eventually
        # touches the Stripe API (directly or via a helper), and this is
        # the one path that pre-fix never called `_configure_stripe()` at
        # all before its first Stripe call.
        _configure_stripe()
        if event_type == "checkout.session.completed":
            outcome = _handle_checkout_session_completed(db, obj, event_created, background_tasks)
        elif event_type in ("customer.subscription.created", "customer.subscription.updated"):
            # Item 17: fall back to the CUSTOMER's own metadata when the
            # subscription object itself carries none -- A2's documented
            # "subscription -> customer" parent fallback, previously dead
            # code on this branch.
            if not _has_app_metadata_with_customer_fallback(obj, obj.get("customer")):
                outcome = "skipped_foreign_app"
            else:
                customer = _stripe_customer_or_create(db, obj["customer"])
                outcome = _apply_subscription_event(db, customer, obj, event_created, background_tasks)
        elif event_type == "customer.subscription.deleted":
            if not _has_app_metadata_with_customer_fallback(obj, obj.get("customer")):
                outcome = "skipped_foreign_app"
            else:
                customer = _stripe_customer_or_create(db, obj["customer"])
                obj = {**obj, "status": "canceled"}
                outcome = _apply_subscription_event(db, customer, obj, event_created, background_tasks)
        elif event_type in ("invoice.paid", "invoice.payment_failed"):
            outcome = _handle_invoice_event(db, obj, event_type, event_created, background_tasks)
        elif event_type == "charge.refunded":
            outcome = _handle_charge_refunded(db, obj, event_created, background_tasks)
        else:
            outcome = "skipped_foreign_app"
    except _PermanentWebhookError as exc:
        outcome = "permanent_error"
        last_error = exc.reason
        _notify_operator(
            background_tasks,
            f"Bill Commons: permanent webhook error ({event_type})",
            f"event {event_id}: {exc.reason}",
        )
    # E7/Gate B (SPEC-LOCKED "Post-verify decisions round 2", rolls back
    # round-1 item 23): there is deliberately no `except Exception` here
    # anymore. Item 23 reclassified `_configure_stripe()`'s `HTTPException`
    # (raised when `STRIPE_SECRET_KEY` was unset) as a `permanent_error`,
    # recorded + 200'd -- but a misconfiguration is TRANSIENT (it self-heals
    # the moment an operator sets the env var), and a 200 makes Stripe stop
    # retrying it forever with no way back in (a Dashboard "Resend" reuses
    # the same event id and is swallowed by the idempotency `ON CONFLICT DO
    # NOTHING`). `_configure_stripe`/`_price_id` now raise
    # `_ConfigurationError` (not `HTTPException`) specifically so it is NOT
    # caught here: it propagates to FastAPI's generic 500 handler, Stripe
    # retries for up to 3 days, and the `UPDATE stripe_events` below never
    # runs -- the just-`INSERT`ed row rolls back with the rest of this
    # uncommitted transaction, exactly like any other unclassified
    # exception this handler can raise.

    db.execute(
        text(
            "UPDATE stripe_events SET outcome = :outcome, processed_at = :now, last_error = :last_error "
            "WHERE id = :id"
        ),
        {
            "outcome": outcome,
            "now": datetime.now(timezone.utc),
            "last_error": last_error,
            "id": event_id,
        },
    )
    # E4/item 24: commit BEFORE any email is sent -- `BackgroundTasks`
    # registered above (`_notify_operator`/`_send_magic_link`/the buyer
    # order email) all run AFTER this response is returned, which is
    # itself after this commit, so nothing is ever emailed off state that
    # could still roll back.
    db.commit()
    return JSONResponse(status_code=200, content={"received": True, "outcome": outcome})
