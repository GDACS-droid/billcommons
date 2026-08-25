"""API key minting, resolution, rotation, and reveal (2026-08-21 monetization
spec, `SPEC-LOCKED.md` R5/B1/B2/B3).

**Key format.** `bc_live_<32 base62>` (`bc_test_...` in Stripe test mode).
`key_prefix` = the first 16 characters (includes the `bc_live_`/`bc_test_`
prefix), unique + indexed, used for O(1) lookup and safe display.
`key_hash` = sha256 hex of the FULL key, compared with `hmac.compare_digest`
-- sha256, not argon2: these are ~190 bits of `secrets.choice` over a 62-char
alphabet, so a stolen hash is not brute-forceable and argon2 would only add
CPU to every single request (matches the existing `manage_token_hash`
precedent in `routers/webhooks.py`).

**Resolution** (`resolve_key`) is the auth hot path for
`billcommons_api.quota.QuotaMiddleware`: it runs on every keyed request, so
it carries its own 60-second in-process TTL cache keyed on `key_hash`
(B2/A8) -- revoke/rotate/suspend call `invalidate(key_hash)` so the
in-process cache never serves a killed key past the operation that killed
it (Railway `numReplicas=1` per B2's own assertion; see the runbook). A
cache HIT still re-checks `revoke_at < now()` (B2) so a `rotating` key's
24h overlap window still expires on time regardless of cache freshness.

**Reveal** (B1): the plaintext key is shown to the caller exactly ONCE, at
mint time, in the mint response itself (the free-tier `/account/keys` path)
or via `reveal_key` (the Stripe-checkout path, Phase 2 -- the key is minted
server-side and the plaintext is Fernet-encrypted into `reveal_ciphertext`
until the customer clicks through a magic link and calls
`POST /account/keys/{id}/reveal`). Never stored anywhere in cleartext
outside that single response.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from billcommons_schema.models import ApiCustomer, ApiKey, ApiSubscription
from billcommons_shared.db import get_session

KEY_PREFIX_LIVE = "bc_live_"
KEY_PREFIX_TEST = "bc_test_"
_VALID_KEY_PREFIXES = (KEY_PREFIX_LIVE, KEY_PREFIX_TEST)

# 32 base62 characters after the `bc_live_`/`bc_test_` marker -- ~190 bits
# of entropy from `secrets.choice`, the same order of magnitude as
# `secrets.token_urlsafe(32)` used elsewhere in this codebase.
_BASE62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_SECRET_LEN = 32

# key_prefix = the first 16 characters of the full key (marker + a few
# secret chars) -- unique, indexed, and safe to display (e.g. in an admin
# usage table) without exposing the whole secret.
KEY_PREFIX_DISPLAY_LEN = 16

# B2: 60-second in-process TTL cache on key_hash. Best-effort: with
# numReplicas=1 (asserted in the runbook) invalidation on revoke/rotate is
# immediate everywhere that matters; a second replica would see up to 60s
# of staleness, documented and accepted (spec risk #10).
_CACHE_TTL_SECONDS = 60.0

# B3: usable = active OR (rotating AND revoke_at > now()). Ceiling = 3
# usable keys per customer (2 active + 1 rotating).
MAX_USABLE_KEYS_PER_CUSTOMER = 3
MAX_ACTIVE_KEYS_PER_CUSTOMER = 2
ROTATION_OVERLAP = timedelta(hours=24)
REVEAL_TOKEN_TTL = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """Assume UTC for a naive datetime read back from the DB.

    Postgres `timestamptz` columns always round-trip timezone-aware; SQLite
    (used only by this branch's throwaway test harness, `tests/
    _monetization_sqlite.py` -- there is no SQLite deployment target) has no
    native timezone-aware datetime storage and hands back naive ones. Every
    stored datetime here is written as UTC (`_now()`), so treating a naive
    value as UTC is correct in both places and makes datetime comparisons
    (`is_usable`, `payment_required`, etc.) dialect-independent instead of
    raising `TypeError: can't compare offset-naive and offset-aware
    datetimes` under SQLite.
    """
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _mint_secret() -> str:
    return "".join(secrets.choice(_BASE62_ALPHABET) for _ in range(_SECRET_LEN))


def mint_key_material(environment: str) -> tuple[str, str, str]:
    """Returns (full_key, key_prefix, key_hash). Does not touch the DB."""
    if environment not in ("live", "test"):
        raise ValueError(f"invalid environment: {environment!r}")
    prefix_marker = KEY_PREFIX_LIVE if environment == "live" else KEY_PREFIX_TEST
    full_key = f"{prefix_marker}{_mint_secret()}"
    key_prefix = full_key[:KEY_PREFIX_DISPLAY_LEN]
    key_hash = hashlib.sha256(full_key.encode()).hexdigest()
    return full_key, key_prefix, key_hash


def parse_presented_key(value: str) -> bool:
    """True if `value` at least LOOKS like one of our keys (has the right
    marker prefix) -- used by `billcommons_api.quota` to decide whether a
    bearer token is an attempted API key (-> 401 on failure) versus some
    other bearer scheme this API doesn't own (-> treated as anonymous, e.g.
    the webhooks router's own `manage_token` bearer auth, amendment A5)."""
    return value.startswith(_VALID_KEY_PREFIXES)


@dataclass(frozen=True)
class ResolvedKey:
    key_id: uuid.UUID
    customer_id: uuid.UUID
    environment: str
    plan: str
    status: str
    revoke_at: datetime | None
    customer_suspended_at: datetime | None
    customer_suspension_reason: str | None
    extra_requests_per_day: int
    extra_heavy_per_day: int
    override_expires_at: datetime | None
    subscription_status: str | None
    subscription_past_due_since: datetime | None

    def is_usable(self, now: datetime | None = None) -> bool:
        now = now or _now()
        if self.status == "active":
            return True
        if self.status == "rotating" and self.revoke_at is not None:
            return _aware(self.revoke_at) > now
        return False

    def override_active(self, now: datetime | None = None) -> bool:
        now = now or _now()
        return self.override_expires_at is not None and _aware(self.override_expires_at) > now

    @property
    def active_extra_requests_per_day(self) -> int:
        return self.extra_requests_per_day if self.override_active() else 0

    @property
    def active_extra_heavy_per_day(self) -> int:
        return self.extra_heavy_per_day if self.override_active() else 0

    def payment_required(self, now: datetime | None = None) -> bool:
        """2026-08-21 fix-pass decision E1 (SPEC-LOCKED "Post-verify
        decisions", resolving the item-0 A3-vs-B4 conflict in favor of
        B4): a `canceled` (or, per fixlist item 3, `incomplete_expired`)
        subscription NEVER 402s -- it just drops the customer to the
        Developer tier (`billing.customer_plan`/`_recompute_plan_onto_keys`
        handle that). 402 fires ONLY for `status='unpaid'`, or `status=
        'past_due'` more than 7 days past `past_due_since` (A3's dunning
        window). The pre-fix version 402'd `canceled` too, which combined
        with item 1's old bug (rewriting a churned customer's keys back to
        a paid plan) meant a customer who canceled was EITHER locked out
        forever OR silently kept paid quota for free, never dropped
        cleanly to the free tier -- this is what "cleanly" looks like."""
        now = now or _now()
        if self.subscription_status == "unpaid":
            return True
        if self.subscription_status == "past_due" and self.subscription_past_due_since is not None:
            return now - _aware(self.subscription_past_due_since) > timedelta(days=7)
        return False


# Item 1 fix (2026-08-21 fix pass): bound the cache AND never cache a miss.
# The pre-fix version stored `None` for every unrecognized `bc_live_`/
# `bc_test_`-shaped bearer an attacker sent -- an unauthenticated remote
# party, unmetered by construction (see item 1's other half in
# `quota.py._dispatch_keyed`), could grow this dict by one permanent entry
# per garbage key on a `numReplicas=1` process. Two independent mitigations:
# (a) never cache a miss (`resolved is None`) at all -- a garbage key always
# re-hits the DB, but never occupies memory; (b) even the legitimate-key
# cache is capped with the same oldest-by-insertion eviction idiom
# `_BoundedFixedWindowCounter` already uses, as defense in depth.
_MAX_CACHE_ENTRIES = 50_000

_cache_lock = threading.Lock()
# key_hash -> (ResolvedKey, expires_at_monotonic) -- only ever holds HITS.
_cache: dict[str, tuple[ResolvedKey, float]] = {}

# Fixlist item 2: `invalidate()` used to only ever pop `_cache`. That is
# necessary but not sufficient -- a concurrent `resolve_key` cache MISS
# can open its own session, read the still-committed pre-change row
# (READ COMMITTED sees the OLD state until the mutating transaction
# actually commits), and re-seed `_cache` with the stale value AFTER this
# `invalidate()` call already ran, with nothing left to invalidate it a
# second time. `_tombstones` records "as of this monotonic timestamp,
# key_hash is known stale"; `resolve_key`'s cache-WRITE path (below)
# refuses to store a resolution whose DB read STARTED before the most
# recent tombstone for that key -- closing the write-after-invalidate
# race rather than just narrowing it. Bounded the same way `_cache` is
# (oldest-by-insertion eviction) -- it only needs to outlive whatever the
# slowest concurrent DB read in flight is, not forever.
_MAX_TOMBSTONES = 50_000
_tombstones: dict[str, float] = {}


def invalidate(key_hash: str) -> None:
    """Drop `key_hash` from the resolution cache immediately, and record a
    tombstone so a read that was already in flight when this ran can't
    re-seed the cache with stale data afterward (see `_tombstones` above).
    Called on revoke/rotate/suspend and any other transition that must
    take effect right away (B2)."""
    now_monotonic = time.monotonic()
    with _cache_lock:
        _cache.pop(key_hash, None)
        if key_hash not in _tombstones and len(_tombstones) >= _MAX_TOMBSTONES:
            oldest = next(iter(_tombstones), None)
            if oldest is not None:
                del _tombstones[oldest]
        _tombstones[key_hash] = now_monotonic


def _invalidate_after_commit(db: OrmSession, key_hash: str) -> None:
    """Item 2 fix: `revoke_key`/`rotate_key` must invalidate AFTER a
    successful commit, not before it -- `expire_stale_reveal` already gets
    this right (`db.commit()` then `invalidate(...)`). But `revoke_key`/
    `rotate_key` don't own their caller's commit: `routers/account.py`'s
    endpoints commit right after calling them, while `routers/billing.py`'s
    webhook handler calls `revoke_key` mid-handler and commits once at the
    very end (E4: "never commit() mid-handler"). A SQLAlchemy `after_commit`
    hook fires exactly when THAT session's transaction actually commits,
    wherever the `db.commit()` call lives, without this function needing to
    know or care which caller owns it. `once=True` so a caller that ends up
    calling `db.commit()` more than once on the same session (defensive/
    idempotent commit patterns) doesn't invalidate more than the one time
    that matters.
    """
    sa_event.listen(db, "after_commit", lambda _session: invalidate(key_hash), once=True)


def clear_cache() -> None:
    """Test-only: drop the entire cache (and tombstones) between test cases
    so one test's key resolution never leaks into another's."""
    with _cache_lock:
        _cache.clear()
        _tombstones.clear()


def expire_stale_reveal(db: OrmSession, row: ApiKey, now: datetime | None = None) -> bool:
    """Round-2 amendment C7: a minted key whose plaintext was never
    revealed within the 24h reveal window is auto-revoked -- a live
    credential sitting encrypted-at-rest forever, with no one who can ever
    read it (the reveal token/ciphertext are the only way in, and they're
    about to be nulled), is dead weight and a future support headache
    ("why is this key active but I never got it?"). Checked LAZILY (no
    scheduled job) on the two paths that ever look at a key with a pending
    reveal: `resolve_key` (below) and `GET /account/me`
    (`routers/account.py`). Returns True if this call just expired it.

    Fixlist item 9 fix (the "sub-defect" half): broadened from
    `status == "active"` to `status in ("active", "rotating")`. Pre-fix,
    a `rotating` key with an expired, un-revealed reveal window could
    never lazily expire -- `reveal_key_endpoint`'s own expired-window
    branch calls this function, but it was always a no-op for a
    `rotating` row, so the reveal columns stayed set forever and every
    call kept returning `reveal_expired` with nothing ever cleaning it up.
    """
    now = now or _now()
    if (
        row.status in ("active", "rotating")
        and row.reveal_ciphertext is not None
        and row.reveal_expires_at is not None
        and _aware(row.reveal_expires_at) < now
    ):
        row.status = "revoked"
        row.revoked_at = now
        row.reveal_ciphertext = None
        row.reveal_token_hash = None
        row.reveal_expires_at = None
        db.commit()
        invalidate(row.key_hash)
        return True
    return False


def _load_resolved_key(db: OrmSession, key_prefix: str, presented_hash: str) -> ResolvedKey | None:
    row: ApiKey | None = db.execute(
        select(ApiKey).where(ApiKey.key_prefix == key_prefix)
    ).scalar_one_or_none()
    if row is None or not hmac.compare_digest(presented_hash, row.key_hash):
        return None
    expire_stale_reveal(db, row)  # C7: may flip row.status to 'revoked' in place
    customer: ApiCustomer | None = db.execute(
        select(ApiCustomer).where(ApiCustomer.id == row.customer_id)
    ).scalar_one_or_none()
    if customer is None:
        return None
    # Item 9 (SPEC-DEV, amendment A3/A4's dunning gate): load the newest
    # subscription REGARDLESS of status. Filtering out 'canceled' here made
    # `subscription_status` resolve to None for a customer whose only
    # subscription is canceled, so `payment_required()`'s canceled branch
    # was unreachable -- they kept serving on the stale paid `api_keys.plan`
    # limits forever. B4's "<=1 non-canceled per customer" invariant means
    # the safe ordering is: prefer the (at most one) non-canceled row, and
    # only fall back to the newest canceled one if that's all there is.
    subscriptions: list[ApiSubscription] = db.execute(
        select(ApiSubscription).where(ApiSubscription.customer_id == row.customer_id)
    ).scalars().all()
    subscription: ApiSubscription | None = None
    non_canceled = [s for s in subscriptions if s.status != "canceled"]
    if non_canceled:
        subscription = max(non_canceled, key=lambda s: s.created_at)
    elif subscriptions:
        subscription = max(subscriptions, key=lambda s: s.created_at)
    return ResolvedKey(
        key_id=row.id,
        customer_id=row.customer_id,
        environment=row.environment,
        plan=row.plan,
        status=row.status,
        revoke_at=row.revoke_at,
        customer_suspended_at=customer.suspended_at,
        customer_suspension_reason=customer.suspension_reason,
        extra_requests_per_day=customer.extra_requests_per_day,
        extra_heavy_per_day=customer.extra_heavy_per_day,
        override_expires_at=customer.override_expires_at,
        subscription_status=subscription.status if subscription else None,
        subscription_past_due_since=subscription.past_due_since if subscription else None,
    )


# Looked up via module attribute (never bound as a default argument) so
# tests can point this module at a throwaway SQLite session factory with a
# plain `monkeypatch.setattr(api_keys, "_session_factory", ...)` -- see
# apps/api/tests/test_api_keys.py and test_quota.py.
_session_factory = get_session


def resolve_key(presented: str) -> ResolvedKey | None:
    """Resolve a presented key string to its current state, or None if it
    does not match any key at all. Caller (quota.py) has already confirmed
    `presented` starts with a valid marker via `parse_presented_key`.
    """
    if not parse_presented_key(presented) or len(presented) < KEY_PREFIX_DISPLAY_LEN:
        return None
    key_hash = hashlib.sha256(presented.encode()).hexdigest()
    now_monotonic = time.monotonic()

    with _cache_lock:
        cached = _cache.get(key_hash)
    if cached is not None:
        resolved, expires_at = cached
        if now_monotonic < expires_at:
            # B2: a cache HIT still re-checks revoke_at on every hit so a
            # `rotating` key dies on schedule regardless of cache freshness.
            if resolved is not None and resolved.status == "rotating" and resolved.revoke_at is not None:
                if _aware(resolved.revoke_at) <= _now():
                    return None
            return resolved

    # Item 2: captured BEFORE the DB read below starts -- this is the
    # timestamp the cache-write path (below) compares against
    # `_tombstones` to detect "this read may have started before a
    # concurrent revoke/rotate's invalidation, so its result must not be
    # cached even though it looks like a legitimate hit."
    read_started_at = time.monotonic()
    key_prefix = presented[:KEY_PREFIX_DISPLAY_LEN]
    db = _session_factory()
    try:
        resolved = _load_resolved_key(db, key_prefix, key_hash)
    finally:
        db.close()

    # Item 1: only cache a HIT. An unknown/garbage key is never stored, so
    # probing distinct bogus keys costs one DB SELECT each and zero
    # permanent memory. A legitimate resolution is still bounded by
    # `_MAX_CACHE_ENTRIES` (oldest-by-insertion eviction), as defense in
    # depth even though the number of real keys is naturally small.
    if resolved is not None:
        with _cache_lock:
            # Item 2: refuse to (re-)seed the cache with a read that may
            # have raced a revoke/rotate's invalidation -- if this key_hash
            # was tombstoned at or after `read_started_at`, this read might
            # have observed the pre-change (stale) row under READ COMMITTED
            # and the invalidate() that already ran has nothing left to
            # invalidate. Simply drop the result instead of caching it; the
            # NEXT resolve_key call re-reads the DB fresh (by then
            # certainly past the mutating transaction's commit) and caches
            # correctly.
            tombstoned_at = _tombstones.get(key_hash)
            if tombstoned_at is not None and tombstoned_at >= read_started_at:
                return resolved
            if key_hash not in _cache and len(_cache) >= _MAX_CACHE_ENTRIES:
                oldest = next(iter(_cache), None)
                if oldest is not None:
                    del _cache[oldest]
            _cache[key_hash] = (resolved, now_monotonic + _CACHE_TTL_SECONDS)
    return resolved


# ---------------------------------------------------------------------------
# Reveal ciphertext (B1)
# ---------------------------------------------------------------------------

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        secret = os.environ.get("BILLCOMMONS_REVEAL_KEY")
        if not secret:
            raise RuntimeError(
                "BILLCOMMONS_REVEAL_KEY is not set -- required to mint or reveal an API key"
            )
        _fernet = Fernet(secret.encode())
    return _fernet


def encrypt_for_reveal(plaintext_key: str) -> str:
    return _get_fernet().encrypt(plaintext_key.encode()).decode()


def decrypt_reveal(ciphertext: str) -> str | None:
    """Returns the decrypted plaintext, or None if the ciphertext can't be
    decrypted for ANY reason -- an expired/corrupt token (`InvalidToken`)
    or a broken `BILLCOMMONS_REVEAL_KEY` (fixlist item 8).

    Item 8 fix: `_get_fernet()` (called by `.decrypt()` via the module
    global) raises `RuntimeError` when `BILLCOMMONS_REVEAL_KEY` is unset,
    and `ValueError`/`binascii.Error` (a `ValueError` subclass) when it is
    set but isn't a valid 32-byte urlsafe-base64 Fernet key. Both used to
    propagate straight out of this function as an unhandled 500 with a
    stack trace -- defeating `reveal_key_endpoint`'s own documented
    contract that a rotated or misconfigured reveal key returns a clean,
    retriable 503. Truncating or dropping that env var during an operator
    edit (exactly the failure mode item 20's startup check exists to
    catch) is the most likely path into this branch, and the ciphertext
    is preserved either way -- this is wrong-status/observability, not
    data loss."""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, RuntimeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Mint / rotate / revoke -- customer-row-locked (B3)
# ---------------------------------------------------------------------------


class KeyLimitExceeded(Exception):
    """Raised when minting/rotating would exceed the usable-key ceiling."""


def _usable_keys_for_update(db: OrmSession, customer_id: uuid.UUID) -> list[ApiKey]:
    """Locks the customer row (B3: `SELECT ... FOR UPDATE`) so two concurrent
    mint/rotate requests for the same customer serialize instead of both
    reading a stale key count and both succeeding past the ceiling.

    Item 6 fix: `populate_existing=True` on the `ApiKey` select forces a
    refresh of any instance already sitting in this session's identity map
    from before the lock was taken -- without it, SQLAlchemy would hand back
    the same (potentially stale) Python object it already had cached, and
    the caller's ceiling/guard checks would evaluate pre-lock state even
    though the query ran after acquiring the lock.
    """
    db.execute(
        select(ApiCustomer.id).where(ApiCustomer.id == customer_id).with_for_update()
    )
    rows = db.execute(
        select(ApiKey)
        .where(ApiKey.customer_id == customer_id)
        .execution_options(populate_existing=True)
    ).scalars().all()
    now = _now()
    return [
        r
        for r in rows
        if r.status == "active" or (r.status == "rotating" and r.revoke_at and _aware(r.revoke_at) > now)
    ]


def mint_key(
    db: OrmSession,
    customer_id: uuid.UUID,
    *,
    environment: str = "live",
    plan: str = "developer",
    name: str = "default",
) -> tuple[ApiKey, str]:
    """Mints a new key under `customer_id`'s row lock. Returns (row,
    full_key) -- the ONLY time the plaintext is available in this process.
    Raises KeyLimitExceeded if the customer is already at the 3-usable
    ceiling (B3), OR already has 2 ACTIVE keys (R5) -- a fresh mint is
    always `status='active'`, and the ceiling's 3rd slot is reserved for a
    `rotating` key in flight, not a third plain active one."""
    usable = _usable_keys_for_update(db, customer_id)
    active_count = sum(1 for k in usable if k.status == "active")
    if active_count >= MAX_ACTIVE_KEYS_PER_CUSTOMER:
        raise KeyLimitExceeded(f"customer {customer_id} already has {active_count} active keys")
    if len(usable) >= MAX_USABLE_KEYS_PER_CUSTOMER:
        raise KeyLimitExceeded(f"customer {customer_id} already has {len(usable)} usable keys")
    full_key, key_prefix, key_hash = mint_key_material(environment)
    row = ApiKey(
        customer_id=customer_id,
        name=name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        environment=environment,
        plan=plan,
        status="active",
    )
    db.add(row)
    db.flush()
    return row, full_key


def mint_developer_key_if_first_login(db: OrmSession, customer_id: uuid.UUID) -> str | None:
    """Item 3 fix (amendment D5/A12a): re-checks the "genuinely first
    login" condition (zero keys, no `stripe_customer_id`, no
    `api_subscriptions` row) INSIDE the same customer-row lock `mint_key`
    takes, not before it.

    Pre-fix, `routers.account._should_auto_mint_developer_key` ran its
    zero-keys SELECT *outside* `mint_key`'s lock -- the lock only enforced
    the 2-active ceiling, not "am I the first login", so two concurrent
    `POST /account/session` calls for a brand-new customer's magic link
    could both pass the zero-keys check and both call `mint_key`, minting
    two Developer keys where D5 promises exactly one auto-minted key ever.

    Locking the customer row FIRST and re-reading the customer, key-count,
    and subscription-count from inside that lock makes the second
    concurrent caller see the first caller's already-minted key (once the
    first caller's `mint_key` call -- itself under the same lock -- has
    flushed) and correctly decline to mint a second one.
    """
    db.execute(select(ApiCustomer.id).where(ApiCustomer.id == customer_id).with_for_update())
    customer = db.execute(
        select(ApiCustomer)
        .where(ApiCustomer.id == customer_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if customer is None or customer.stripe_customer_id is not None:
        return None
    has_key = db.execute(
        select(ApiKey.id)
        .where(ApiKey.customer_id == customer_id)
        .execution_options(populate_existing=True)
    ).first()
    if has_key is not None:
        return None
    has_subscription = db.execute(
        select(ApiSubscription.id).where(ApiSubscription.customer_id == customer_id)
    ).first()
    if has_subscription is not None:
        return None
    # `mint_key` re-acquires the same row lock (harmless -- Postgres allows
    # re-acquiring a row lock already held by the same transaction) and
    # re-runs its own ceiling checks, which trivially pass here since we
    # just confirmed zero keys under this same lock.
    _, full_key = mint_key(db, customer_id, environment="live", plan="developer")
    return full_key


def rotate_key(db: OrmSession, key_id: uuid.UUID, customer_id: uuid.UUID) -> tuple[ApiKey, str]:
    """Mints a successor for `key_id`, sets the original to `rotating` with
    a 24h `revoke_at`, and invalidates both keys' cache entries. A9/B3: at
    most one `rotating` key per customer at a time; refused (409, raised as
    KeyLimitExceeded here) if this rotation would exceed the 3-usable
    ceiling or if a rotation is already in flight.

    Item 6 fix: `old` is looked up AFTER `_usable_keys_for_update` takes the
    customer row lock, not before -- the pre-fix ordering let a stale,
    already-loaded `old` (read before the lock) sail through the
    `any(status == "rotating")` and ceiling checks below using pre-lock
    state even though the lock had since been acquired.
    """
    usable = _usable_keys_for_update(db, customer_id)

    old = db.execute(
        select(ApiKey)
        .where(ApiKey.id == key_id, ApiKey.customer_id == customer_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if old is None or old.status != "active":
        raise KeyLimitExceeded("key not found or not active")

    if any(k.status == "rotating" for k in usable):
        raise KeyLimitExceeded("a rotation is already in flight for this customer")
    if len(usable) >= MAX_USABLE_KEYS_PER_CUSTOMER:
        raise KeyLimitExceeded("rotation would exceed the usable-key ceiling")

    full_key, key_prefix, key_hash = mint_key_material(old.environment)
    new_row = ApiKey(
        customer_id=customer_id,
        name=old.name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        environment=old.environment,
        plan=old.plan,
        status="active",
        rotated_from=old.id,
    )
    db.add(new_row)
    old.status = "rotating"
    old.revoke_at = _now() + ROTATION_OVERLAP
    db.flush()
    # Item 2 fix: the pre-fix version called `invalidate(old.key_hash)`
    # here -- BEFORE the caller's own `db.commit()` -- leaving the exact
    # window this fixlist item is about (a concurrent `resolve_key` cache
    # miss can re-seed `_cache` with the pre-rotation `active` state after
    # this invalidate but before the commit lands, and then never gets
    # invalidated again). Double-invalidate: an immediate one here (cheap,
    # narrows the window) plus one registered to fire exactly when the
    # session's transaction actually commits, wherever that `db.commit()`
    # call lives (see `_invalidate_after_commit`'s docstring).
    invalidate(old.key_hash)
    _invalidate_after_commit(db, old.key_hash)
    return new_row, full_key


def revoke_key(db: OrmSession, key_id: uuid.UUID, customer_id: uuid.UUID) -> ApiKey | None:
    """Item 6 fix: takes the same customer-row lock `mint_key`/`rotate_key`
    do. Before this fix, `revoke_key` never locked the customer row at all,
    so a rotation racing a revoke could read `old` before the revoke
    committed and then overwrite `status='revoked'` with `'rotating'` plus a
    fresh 24h `revoke_at` -- resurrecting a credential the operator had just
    killed."""
    db.execute(select(ApiCustomer.id).where(ApiCustomer.id == customer_id).with_for_update())
    row = db.execute(
        select(ApiKey)
        .where(ApiKey.id == key_id, ApiKey.customer_id == customer_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        return None
    row.status = "revoked"
    row.revoked_at = _now()
    row.revoke_at = _now()
    db.flush()
    # Item 2 fix: same double-invalidate as `rotate_key` above -- see
    # `_invalidate_after_commit`'s docstring. The pre-fix single
    # pre-commit `invalidate(row.key_hash)` here is item 2's decisive
    # defect: a concurrent `resolve_key` cache miss racing this revoke's
    # flush->commit window could re-seed `_cache` with `(active, ...)`
    # after this call, and nothing ever invalidated it again -- a
    # customer-visible "revoke returns 204 but the key keeps working for
    # up to 60s" bug.
    invalidate(row.key_hash)
    _invalidate_after_commit(db, row.key_hash)
    return row
