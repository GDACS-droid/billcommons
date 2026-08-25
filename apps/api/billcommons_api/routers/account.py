"""/api/v1/account -- email-only identity (no user system: identity is an
email, the account system is Stripe -- Phase 2). 2026-08-21 monetization
spec, `SPEC-LOCKED.md` §2/§8 as amended.

**Login is a magic link, not a password.** `POST /magic-link {email}` always
returns 202 (never reveals whether an account exists), rate-limited by IP
and by normalized email (own in-process buckets -- this whole router is
exempt from `QuotaMiddleware`, amendment A5, since it does its own auth).
The link's token is single-use, sha256-hashed at rest, 15-minute TTL
(`account_login_tokens`, shared with the key-reveal flow, B1).

**Round-3 amendment D7: the emailed link points at a static WEB page**,
`https://billcommons.org/account/login?token=...`
(`apps/web/app/account/login/page.tsx`), NOT an API route -- a `GET` must
never consume a single-use token (a link that gets pre-fetched by an email
scanner/proxy would burn it before the human ever clicks). That page's
"Continue" button does the actual consumption via
`POST /api/v1/account/session {token}` (JSON body, Origin-checked like
every other unsafe method here, B7): on success it upserts the
`api_customers` row by lower(email) (A1), sets a signed HttpOnly session
cookie (24h, SameSite=Lax, Secure -- hand-rolled HMAC, not itsdangerous, to
avoid a new dependency for one signed cookie), and returns either `204`
(nothing new to show -- the web page redirects straight to `/account`) or
`200 {"key": "bc_live_..."}` when this login just auto-minted this
customer's first Developer key (amendment D5: only when the customer has
ZERO keys, NO `stripe_customer_id`, and NO `api_subscriptions` row -- so a
customer who's ever touched Stripe, or who still has an existing key,
never gets a surprise second key here). That key is revealed INLINE in
this synchronous response (D5 supersedes the earlier draft's B1
reveal-ciphertext dance for this path -- there is no more "no response
body to hand a secret back in" problem once login itself is a JSON POST,
not a redirect). `reveal_ciphertext` stays reserved for Phase 2's
Stripe-checkout-minted key, where the mint truly does happen out of band
of any request the customer is a party to.

`POST /keys` (an already-logged-in customer minting an EXTRA key) is
likewise a synchronous request/response, so its plaintext IS returned
directly in that response body, once.

Every cookie-authenticated unsafe method (`POST`/`DELETE` here) requires an
`Origin` header from an allowed list (B7's CSRF gate, env
`BILLCOMMONS_ALLOWED_ORIGINS`) -- else 403 `bad_origin`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
import urllib.request
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session as OrmSession

from billcommons_api.api_keys import (
    KeyLimitExceeded,
    _PLAN_AUTHORITY_STATUSES,
    _aware,
    decrypt_reveal,
    expire_stale_reveal,
    mint_developer_key_if_first_login,
    mint_key,
    revoke_key,
    rotate_key,
)
from billcommons_api.deps import get_db
from billcommons_api.errors import conflict, forbidden, not_found, service_unavailable, unauthorized
from billcommons_api.rate_limit import _BoundedFixedWindowCounter, client_ip
from billcommons_schema.models import ApiCustomer, ApiKey, ApiSubscription

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/account", tags=["account"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SESSION_COOKIE = "bc_session"
_SESSION_TTL = timedelta(hours=24)
_LOGIN_TOKEN_TTL = timedelta(minutes=15)

_NOTIFY_FROM = "Bill Commons <alerts@billcommons.org>"

# Own in-process limiters -- this router is exempt from QuotaMiddleware
# (amendment A5), so magic-link issuance is throttled here instead. Item 10
# fix: `_BoundedFixedWindowCounter` (not the plain, unbounded
# `_FixedWindowCounter`) -- these are 1-hour windows, so the sweep that
# reclaims stale buckets only runs once per hour, and an unbounded dict
# could accumulate for that whole hour. Same 100k oldest-by-insertion cap
# the R10 anonymous daily buckets use.
_MAX_TRACKED_LOGIN_BUCKETS = 100_000
_ip_limiter = _BoundedFixedWindowCounter(20, 3600.0, time.monotonic, _MAX_TRACKED_LOGIN_BUCKETS)
_email_limiter = _BoundedFixedWindowCounter(5, 3600.0, time.monotonic, _MAX_TRACKED_LOGIN_BUCKETS)


def _normalize_email(email: str) -> str | None:
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        return None
    return email


def _session_secret() -> str:
    secret = os.environ.get("ACCOUNT_SESSION_SECRET")
    if not secret:
        raise RuntimeError("ACCOUNT_SESSION_SECRET is not set")
    return secret


def _sign_session(customer_id: uuid.UUID) -> str:
    exp = int((datetime.now(timezone.utc) + _SESSION_TTL).timestamp())
    payload = f"{customer_id}.{exp}"
    sig = hmac.new(_session_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()


def _verify_session(cookie_value: str) -> uuid.UUID | None:
    # Item 11 fix: the whole verification -- decode, HMAC compare, and UUID
    # parse -- lives inside ONE try/except now. Pre-fix, the
    # `hmac.compare_digest` call sat OUTSIDE the try block that only guarded
    # the base64 decode, so a crafted `bc_session` cookie whose decoded
    # signature contained a non-ASCII-compatible character raised
    # `TypeError` out of this function and 500'd `GET /account/me` instead
    # of the intended 401 `session_required`.
    try:
        decoded = base64.urlsafe_b64decode(cookie_value.encode()).decode()
        customer_id_str, exp_str, sig = decoded.rsplit(".", 2)
        payload = f"{customer_id_str}.{exp_str}"
        expected_sig = hmac.new(_session_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(
            sig.encode("utf-8", "surrogatepass"), expected_sig.encode("utf-8", "surrogatepass")
        ):
            return None
        if int(exp_str) < int(time.time()):
            return None
        return uuid.UUID(customer_id_str)
    except Exception:
        return None


def _allowed_origins() -> set[str]:
    raw = os.environ.get(
        "BILLCOMMONS_ALLOWED_ORIGINS", "https://billcommons.org,https://www.billcommons.org"
    )
    return {o.strip() for o in raw.split(",") if o.strip()}


def _check_origin(request: Request) -> None:
    """B7: every cookie-authed unsafe method requires an allowed Origin."""
    origin = request.headers.get("origin")
    if origin not in _allowed_origins():
        raise forbidden("bad_origin", "Origin header missing or not allowed.")


def _require_session(request: Request, db: OrmSession) -> ApiCustomer:
    cookie_value = request.cookies.get(_SESSION_COOKIE, "")
    customer_id = _verify_session(cookie_value) if cookie_value else None
    if customer_id is None:
        raise unauthorized("session_required", "Log in via a magic link first.")
    customer = db.execute(select(ApiCustomer).where(ApiCustomer.id == customer_id)).scalar_one_or_none()
    if customer is None:
        raise unauthorized("session_required", "Log in via a magic link first.")
    return customer


def _set_no_store_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


def _send_email(to: str, subject: str, body: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        # Documented behavior (see docs/operations/monetization-runbook.md):
        # with no RESEND_API_KEY, log the link at WARN instead of emailing
        # it, so local/dev environments can still complete the login flow.
        logger.warning("RESEND_API_KEY unset -- magic link for %s: %s", to, body)
        return
    payload = {
        "from": _NOTIFY_FROM,
        "to": [to],
        "subject": subject,
        "text": body,
    }
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "billcommons-api/1.0",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception:
        logger.exception("magic-link email failed")


def _issue_login_token(db: OrmSession, email: str, request_ip: str, *, commit: bool = True) -> str:
    """E8 (SPEC-LOCKED "Post-verify decisions round 2"): webhook transaction
    contract -- NO helper called from inside `stripe_webhook` may `commit()`
    or `rollback()` its caller's transaction. This function takes a
    `commit` flag instead of deciding for itself: `True` (default) for its
    OWN router's `request_magic_link`, which owns its whole transaction on
    this session and ends its job right here; `False` from `billing.
    _send_magic_link`, which runs INSIDE the Stripe webhook's single
    all-or-nothing transaction (see `billing.py`'s module docstring) and
    must never have that transaction ended early out from under it.

    Item 2 fix (round-2 fixlist, HIGH): this used to catch its own
    insert/flush failure, log it, `db.rollback()`, and return `None` --
    correct for `request_magic_link`'s "always 202" contract, but WRONG for
    `billing._send_magic_link`: a transient failure there was silently
    swallowed, the webhook handler carried on to a `200 {"outcome":
    "processed"}`, and `db.commit()` at the end of `stripe_webhook`
    committed nothing (the earlier `rollback()` here had already discarded
    the `stripe_events` idempotency row, the upserted customer, the freshly
    minted key and its `reveal_ciphertext`, and any entitlement) -- Stripe
    never retries a 200, and there's no `stripe_events` row left to
    diagnose from. Fixed by removing ALL error handling from this
    function -- it now only inserts + flushes (or commits, if asked) and
    lets any exception propagate to whichever caller is responsible for
    deciding what "failure" means on its own transaction. `request_magic_
    link` (item 10) wraps its own call in its own `try/except` to keep its
    documented always-202, never-reveal-DB-liveness contract; the webhook
    deliberately does NOT catch it, so the failure 500s the whole delivery
    and rolls back everything written earlier in it (item 2's actual
    fix)."""
    from billcommons_schema.models import AccountLoginToken

    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db.add(
        AccountLoginToken(
            token_hash=token_hash,
            email=email,
            purpose="login",
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + _LOGIN_TOKEN_TTL,
            request_ip=request_ip,
        )
    )
    db.flush()
    if commit:
        db.commit()
    return token


# ---------------------------------------------------------------------------
# Request/response bodies
# ---------------------------------------------------------------------------


class MagicLinkRequest(BaseModel):
    # Fixlist item 15 fix: plain `str`, no length bounds. `Field(min_length=3,
    # max_length=320)` made FastAPI reject `{"email": ""}` or a 321-char
    # email with 422 BEFORE `request_magic_link` ever ran -- a
    # distinguishable-by-length response instead of this route's own
    # documented uniform 202, which `_normalize_email` -> `always_ok`
    # already exists specifically to guarantee. Not an account-enumeration
    # oracle (length-only, never existence), but a contract-consistency
    # bug: `_normalize_email` owns rejection now, for every malformed
    # shape, not just the ones inside these bounds.
    email: str


class MagicLinkResponse(BaseModel):
    accepted: bool = True


class KeyMintRequest(BaseModel):
    name: str = Field(default="default", max_length=200)
    # Item 7 fix: was a bare `str`. `mint_key_material` (api_keys.py) raises
    # `ValueError` for anything but "live"/"test", and `create_key` below
    # only caught `KeyLimitExceeded` -- so `{"environment": "prod"}` from
    # any logged-in customer 500'd with a stack trace instead of a clean
    # 422. A `Literal` makes Pydantic itself reject the bad value before the
    # handler ever runs.
    environment: Literal["live", "test"] = "live"


class KeyRevealResponse(BaseModel):
    key: str
    warning: str = "This is the only time this key will be shown. Store it now."


class SessionRequest(BaseModel):
    # Fixlist item 15 fix: same reasoning as `MagicLinkRequest.email` above
    # -- a too-short/too-long `token` used to 422 instead of this route's
    # intended uniform 404 `invalid_token` (the token-hash lookup in
    # `consume_session_token` already rejects any string that doesn't
    # match a real, unused, unexpired token).
    token: str


@router.post("/magic-link", response_model=MagicLinkResponse, status_code=202)
def request_magic_link(
    body: MagicLinkRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: OrmSession = Depends(get_db),
) -> MagicLinkResponse:
    always_ok = MagicLinkResponse(accepted=True)
    email = _normalize_email(body.email)
    if email is None:
        return always_ok  # never reveal validation details either

    ip = client_ip(request)
    ip_ok, *_ = _ip_limiter.allow(ip)
    if not ip_ok:
        # Item 10 fix: short-circuit -- the pre-fix version always called
        # `_email_limiter.allow(email)` even when the IP was already over
        # its limit, so a single attacker IP could create attacker-chosen
        # bucket keys (arbitrary email strings) in the email limiter's dict
        # at full request rate on a route `QuotaMiddleware` never even sees
        # (amendment A5 exemption).
        return always_ok  # silently drop -- 202 either way, never reveal rate state
    email_ok, *_ = _email_limiter.allow(email)
    if not email_ok:
        return always_ok  # silently drop -- 202 either way, never reveal rate state

    # Item 2/10 fix: `_issue_login_token` no longer catches its own
    # failures (see its docstring -- E8 forbids a webhook-callable helper
    # from committing/rolling back on its caller's behalf). This route
    # OWNS its own try/except now, preserving its documented "always 202,
    # never reveal whether the DB is up" contract for BOTH the token
    # insert AND the `commit=True` this call performs (item 10: the
    # pre-fix version left that commit unguarded, outside any try/except,
    # so a commit-time failure propagated as a bare 500).
    try:
        token = _issue_login_token(db, email, ip, commit=True)
    except Exception:
        logger.exception("failed to issue login token")
        db.rollback()
        return always_ok
    # D7: the link points at a static WEB page, never this API directly --
    # a GET must never consume a single-use token (an email
    # scanner/proxy that pre-fetches links would burn it before the human
    # ever clicks). That page's "Continue" button is what calls
    # POST /api/v1/account/session.
    from billcommons_api.routers.billing import _site_url

    link = f"{_site_url()}/account/login?token={token}"
    # Fixlist item 16: was a bare `threading.Thread(daemon=True).start()`
    # per accepted request -- this route is exempt from `QuotaMiddleware`
    # (A5) and bounded only by the 20/hour/IP + 5/hour/email limiters
    # above, so a distributed source could hold an unbounded number of
    # 30-second OS threads open (each blocked in `urllib.request.urlopen`)
    # on the API process. `BackgroundTasks` is Phase 2's established
    # pattern for this exact "send email after the response" need
    # (`routers/billing.py`'s `_notify_operator`/webhook handlers) --
    # Starlette runs queued tasks in the SAME bounded AnyIO threadpool
    # every sync route handler already runs in, not a fresh unbounded
    # thread per call.
    background_tasks.add_task(
        _send_email,
        email,
        "Your Bill Commons sign-in link",
        f"Sign in: {link}\n\nExpires in 15 minutes.",
    )
    return always_ok


def _upsert_customer_by_email(db: OrmSession, email: str) -> ApiCustomer:
    """Item 3 fix (part 2 of 3): upsert-by-email via `ON CONFLICT DO
    NOTHING` + re-select, instead of the pre-fix select-then-insert. Two
    concurrent first logins for the same brand-new email used to both pass
    the `customer is None` check and both attempt an INSERT, racing the
    `uq_api_customers_email` unique index into a 500 for whichever
    transaction committed second."""
    customer = db.execute(select(ApiCustomer).where(ApiCustomer.email == email)).scalar_one_or_none()
    if customer is not None:
        return customer
    dialect_name = db.get_bind().dialect.name
    insert_fn = pg_insert if dialect_name == "postgresql" else sqlite_insert
    db.execute(insert_fn(ApiCustomer).values(email=email).on_conflict_do_nothing(index_elements=["email"]))
    return db.execute(select(ApiCustomer).where(ApiCustomer.email == email)).scalar_one()


@router.post("/session")
def consume_session_token(
    body: SessionRequest, request: Request, db: OrmSession = Depends(get_db)
):
    """D7: consumes a magic-link token (POST only -- a GET must never
    consume it). Origin-checked like every other unsafe method on this
    router (B7). Sets the session cookie and returns either `204` (nothing
    new to reveal) or `200 {"key": ...}` when this login just auto-minted
    the customer's first Developer key (D5, revealed INLINE -- no
    reveal-ciphertext needed for a synchronous JSON response).

    Item 3 fix (part 1 of 3): the token is claimed with ONE atomic
    `UPDATE ... WHERE used_at IS NULL ... RETURNING`, replacing the pre-fix
    check-then-set (a `SELECT`, then `row.used_at = now` applied only at
    `db.commit()` much later) that let two concurrent calls with the SAME
    token both pass the "is it unused" check before either had committed.

    Fixlist item 6 fix: `_session_secret()` is resolved FIRST, before any
    write. Pre-fix, a misconfigured instance (`ACCOUNT_SESSION_SECRET`
    unset -- startup only logs an ERROR for this, per item 20, and keeps
    serving) burned the magic-link token, upserted the customer, and
    auto-minted the customer's ONE Developer key (`mint_developer_key_if_
    first_login` never mints a second) all the way through `db.commit()`
    below -- and only THEN called `_sign_session` -> `_session_secret()`,
    which raises. The customer got an unhandled 500 with the token already
    consumed, the key already minted, and no way to ever read that key's
    plaintext (`reveal_ciphertext` is null on this inline-reveal path by
    design, D5) -- permanently unreadable and burning one of two active
    key slots, recoverable only via a second key from the account UI.
    Failing here, before any of that is written, turns a misconfiguration
    into a clean, retriable failure instead of an unrecoverable one.
    """
    _check_origin(request)
    _session_secret()  # raises RuntimeError -> 500 BEFORE any write below
    from billcommons_schema.models import AccountLoginToken

    token_hash = hashlib.sha256(body.token.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    claimed = db.execute(
        update(AccountLoginToken)
        .where(AccountLoginToken.token_hash == token_hash)
        .where(AccountLoginToken.used_at.is_(None))
        .where(AccountLoginToken.purpose == "login")
        .where(AccountLoginToken.expires_at > now)
        .values(used_at=now)
        .returning(AccountLoginToken.email)
    ).first()
    if claimed is None:
        raise not_found("invalid_token", "This sign-in link is invalid or has expired.")
    email = claimed[0]

    customer = _upsert_customer_by_email(db, email)

    # Item 3 fix (part 3 of 3): the "is this genuinely the first login"
    # check (zero keys, no Stripe customer, no subscription) now runs
    # INSIDE `mint_developer_key_if_first_login`'s own customer-row lock,
    # re-checked AFTER the lock is acquired -- not via a separate pre-lock
    # SELECT here (the pre-fix `_should_auto_mint_developer_key`), which let
    # two concurrent logins for a brand-new customer both observe zero keys
    # and both mint one.
    minted_key = mint_developer_key_if_first_login(db, customer.id)

    db.commit()

    from fastapi.responses import JSONResponse

    out = JSONResponse(content={"key": minted_key}, status_code=200) if minted_key is not None else Response(status_code=204)
    out.set_cookie(
        _SESSION_COOKIE,
        _sign_session(customer.id),
        max_age=int(_SESSION_TTL.total_seconds()),
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    _set_no_store_headers(out)
    return out


def _current_plan(db: OrmSession, customer_id: uuid.UUID) -> str:
    """The customer's current plan, derived the same way `GET /me` always
    has: the newest non-canceled subscription's plan, or `developer` with
    none. Item 8's fix factors this out so `create_key` below can share it
    -- see that function's docstring for why."""
    subscription = db.execute(
        select(ApiSubscription)
        .where(ApiSubscription.customer_id == customer_id)
        .where(ApiSubscription.status.in_(_PLAN_AUTHORITY_STATUSES))
        .order_by(ApiSubscription.created_at.desc())
    ).scalars().first()
    return subscription.plan if subscription else "developer"


@router.get("/me")
def get_account(request: Request, response: Response, db: OrmSession = Depends(get_db)):
    _set_no_store_headers(response)
    customer = _require_session(request, db)
    keys = db.execute(select(ApiKey).where(ApiKey.customer_id == customer.id)).scalars().all()
    # C7: lazily expire any key whose reveal window lapsed unrevealed, so
    # the UI sees `status: "revoked"` and can offer "Generate a new key"
    # instead of showing a key nobody can ever read.
    for k in keys:
        expire_stale_reveal(db, k)
    plan = _current_plan(db, customer.id)

    today = datetime.now(timezone.utc).date()
    usage_by_key: dict[str, dict] = {}
    if keys:
        from billcommons_schema.models import ApiKeyUsage

        rows = db.execute(
            select(ApiKeyUsage).where(
                ApiKeyUsage.key_id.in_([k.id for k in keys]), ApiKeyUsage.usage_date == today
            )
        ).scalars().all()
        usage_by_key = {str(r.key_id): {"requests": r.requests, "heavy_requests": r.heavy_requests} for r in rows}

    return {
        "email": customer.email,
        "plan": plan,
        "keys": [
            {
                "id": str(k.id),
                "name": k.name,
                "key_prefix": k.key_prefix,
                "environment": k.environment,
                "plan": k.plan,
                "status": k.status,
                "created_at": k.created_at.isoformat(),
                "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
                "pending_reveal": k.reveal_ciphertext is not None,
                "usage_today": usage_by_key.get(str(k.id), {"requests": 0, "heavy_requests": 0}),
            }
            for k in keys
        ],
    }


@router.post("/keys", response_model=KeyRevealResponse, status_code=201)
def create_key(
    body: KeyMintRequest, request: Request, db: OrmSession = Depends(get_db)
) -> KeyRevealResponse:
    """Item 8 fix: the plan is DERIVED from the customer's current
    subscription (same expression `GET /me` uses), not hardcoded to
    `"developer"`. Pre-fix, a paying Builder/Scale customer who minted a
    SECOND key from `/account` got that key metered at free-tier limits --
    `rotate_key` already correctly inherited `old.plan`; this was the one
    path that didn't, and it is a straight revenue leak, not a nit."""
    _check_origin(request)
    customer = _require_session(request, db)
    plan = _current_plan(db, customer.id)
    try:
        _, full_key = mint_key(db, customer.id, environment=body.environment, name=body.name, plan=plan)
    except KeyLimitExceeded as exc:
        # Fixlist item 11: 409, not 400 -- `SPEC-LOCKED.md` B3/A9 and this
        # service layer's own docstring (`api_keys.py`: "refused (409,
        # raised as KeyLimitExceeded here)") both call this a conflict
        # (an existing-state precondition failure), not a malformed
        # request. Only the router disagreed with both.
        raise conflict("key_limit_exceeded", str(exc))
    db.commit()
    return KeyRevealResponse(key=full_key)


@router.post("/keys/{key_id}/rotate", response_model=KeyRevealResponse)
def rotate_key_endpoint(
    key_id: uuid.UUID, request: Request, db: OrmSession = Depends(get_db)
) -> KeyRevealResponse:
    _check_origin(request)
    customer = _require_session(request, db)
    try:
        _, full_key = rotate_key(db, key_id, customer.id)
    except KeyLimitExceeded as exc:
        # Fixlist item 11: 409, matching `SPEC-LOCKED.md` B3 ("Rotation
        # refused (409) if it would exceed the ceiling") and A9 ("At most
        # one rotating key per customer at a time (409 otherwise)").
        raise conflict("rotation_refused", str(exc))
    db.commit()
    return KeyRevealResponse(key=full_key)


@router.post("/keys/{key_id}/revoke", status_code=204)
def revoke_key_endpoint(key_id: uuid.UUID, request: Request, db: OrmSession = Depends(get_db)) -> Response:
    _check_origin(request)
    customer = _require_session(request, db)
    row = revoke_key(db, key_id, customer.id)
    if row is None:
        raise not_found("key_not_found", "No such key on this account.")
    db.commit()
    return Response(status_code=204)


@router.post("/keys/{key_id}/reveal", response_model=KeyRevealResponse)
def reveal_key_endpoint(
    key_id: uuid.UUID, request: Request, db: OrmSession = Depends(get_db)
) -> KeyRevealResponse:
    """B1: return the plaintext exactly once, then null the reveal columns.
    Cookie-authed + Origin-checked (B7).

    Item 5 fix: the expired-window branch now calls `expire_stale_reveal`
    itself (status='revoked' + revoked_at + cache invalidation) instead of
    only nulling the reveal columns and leaving `status='active'` --
    otherwise `expire_stale_reveal`'s own lazy auto-revoke (C7) can never
    fire again once `reveal_ciphertext` is already None, and the key stays
    `active` (occupying one of the 2 active slots) forever with no one who
    can ever read it.

    Item 4 fix: decrypt BEFORE nulling anything. Pre-fix, the reveal
    columns were nulled and committed FIRST, and `plaintext is None`
    (decrypt failure) was only checked after -- so a rotated or
    misconfigured `BILLCOMMONS_REVEAL_KEY` destroyed the only copy of a
    live credential and then 404'd, leaving the key `active` and unreadable
    forever. Now a decrypt failure leaves the ciphertext INTACT and returns
    503 (retryable once the reveal key is fixed) instead of destroying
    it.

    Fixlist item 9 fix: the initial `SELECT` now takes `FOR UPDATE` on the
    row, and the eventual claim is a conditional
    `UPDATE ... WHERE reveal_ciphertext IS NOT NULL RETURNING id` rather
    than an unconditional assignment + commit. Pre-fix, two concurrent
    authenticated reveals for the same key could both observe
    `reveal_ciphertext IS NOT NULL` (no lock, no conditional consume), both
    decrypt, both null, both commit, and both return 200 with the
    plaintext -- violating B1's "shown to the caller exactly ONCE". `FOR
    UPDATE` serializes concurrent callers on this row; the conditional
    UPDATE is the second, defense-in-depth guarantee (it also protects
    correctness if `FOR UPDATE` is ever a no-op, e.g. a dialect that
    doesn't implement row locks) -- a second caller unblocked after the
    first commits re-reads `reveal_ciphertext IS NULL` and 404s
    `nothing_to_reveal` instead of double-revealing. Kept as a SEPARATE
    conditional UPDATE (not folded into one `UPDATE ... RETURNING`) so the
    decrypt-before-destroy property from item 4 above still holds: a
    decrypt failure leaves the ciphertext intact rather than being
    destroyed by an all-in-one claim."""
    _check_origin(request)
    customer = _require_session(request, db)
    row = db.execute(
        select(ApiKey)
        .where(ApiKey.id == key_id, ApiKey.customer_id == customer.id)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None or row.reveal_ciphertext is None:
        raise not_found("nothing_to_reveal", "No pending key reveal for this account.")
    now = datetime.now(timezone.utc)
    if row.reveal_expires_at is not None and _aware(row.reveal_expires_at) < now:
        expire_stale_reveal(db, row, now)
        raise not_found("reveal_expired", "This key's reveal window has expired.")

    plaintext = decrypt_reveal(row.reveal_ciphertext)
    if plaintext is None:
        raise service_unavailable(
            "reveal_decrypt_failed",
            "Could not decrypt the stored key. This is a temporary server "
            "configuration issue -- try again shortly, or contact support "
            "if it persists.",
        )
    claimed = db.execute(
        update(ApiKey)
        .where(ApiKey.id == row.id)
        .where(ApiKey.reveal_ciphertext.is_not(None))
        .values(reveal_ciphertext=None, reveal_token_hash=None, reveal_expires_at=None)
        .returning(ApiKey.id)
    ).first()
    if claimed is None:
        raise not_found("nothing_to_reveal", "No pending key reveal for this account.")
    db.commit()
    return KeyRevealResponse(key=plaintext)
