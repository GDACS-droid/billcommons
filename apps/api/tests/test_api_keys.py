"""Unit tests for `billcommons_api.api_keys` against a throwaway in-memory
SQLite DB (see `_monetization_sqlite.py` for why this suite doesn't use the
live-Postgres `client`/`app` fixtures from `conftest.py`: migration 0019's
tables don't exist there until the operator applies it).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, text

import billcommons_api.api_keys as api_keys
from billcommons_schema.models import ApiCustomer, ApiKey, ApiSubscription

from tests._monetization_sqlite import build_sqlite_app


@pytest.fixture()
def db_session(monkeypatch):
    monkeypatch.setenv("BILLCOMMONS_REVEAL_KEY", Fernet.generate_key().decode())
    _, SessionLocal = build_sqlite_app(monkeypatch)
    db = SessionLocal()
    yield db
    db.close()


def _make_customer(db, email="dev@example.com") -> ApiCustomer:
    customer = ApiCustomer(email=email)
    db.add(customer)
    db.flush()
    db.commit()
    return customer


def test_mint_key_format_and_hash(db_session):
    customer = _make_customer(db_session)
    row, full_key = api_keys.mint_key(db_session, customer.id, environment="live", plan="developer")
    db_session.commit()

    assert full_key.startswith("bc_live_")
    assert len(full_key) == len("bc_live_") + 32
    assert row.key_prefix == full_key[:16]
    assert row.key_hash != full_key  # never stored in plaintext
    assert row.status == "active"


def test_test_mode_prefix(db_session):
    customer = _make_customer(db_session)
    _, full_key = api_keys.mint_key(db_session, customer.id, environment="test", plan="developer")
    db_session.commit()
    assert full_key.startswith("bc_test_")

    resolved = api_keys.resolve_key(full_key)
    assert resolved is not None
    assert resolved.environment == "test"


def test_resolve_key_unknown_returns_none(db_session):
    assert api_keys.resolve_key("bc_live_" + "z" * 32) is None


def test_resolve_key_valid(db_session):
    customer = _make_customer(db_session)
    _, full_key = api_keys.mint_key(db_session, customer.id, environment="live", plan="builder")
    db_session.commit()

    resolved = api_keys.resolve_key(full_key)
    assert resolved is not None
    assert resolved.plan == "builder"
    assert resolved.is_usable()


def test_resolve_key_revoked_is_not_usable(db_session):
    customer = _make_customer(db_session)
    row, full_key = api_keys.mint_key(db_session, customer.id)
    db_session.commit()
    api_keys.revoke_key(db_session, row.id, customer.id)
    db_session.commit()

    resolved = api_keys.resolve_key(full_key)
    assert resolved is not None
    assert resolved.status == "revoked"
    assert not resolved.is_usable()


def test_key_ceiling_refuses_a_fourth_usable_key(db_session):
    customer = _make_customer(db_session)
    api_keys.mint_key(db_session, customer.id, name="a")
    db_session.commit()
    api_keys.mint_key(db_session, customer.id, name="b")
    db_session.commit()
    with pytest.raises(api_keys.KeyLimitExceeded):
        api_keys.mint_key(db_session, customer.id, name="c")


def test_rotation_overlap_old_key_valid_for_24h(db_session):
    customer = _make_customer(db_session)
    old_row, old_key = api_keys.mint_key(db_session, customer.id)
    db_session.commit()

    new_row, new_key = api_keys.rotate_key(db_session, old_row.id, customer.id)
    db_session.commit()

    old_resolved = api_keys.resolve_key(old_key)
    assert old_resolved is not None
    assert old_resolved.status == "rotating"
    assert old_resolved.is_usable()  # still usable within the 24h overlap

    new_resolved = api_keys.resolve_key(new_key)
    assert new_resolved is not None
    assert new_resolved.status == "active"


def test_rotation_refused_when_already_rotating(db_session):
    customer = _make_customer(db_session)
    old_row, _ = api_keys.mint_key(db_session, customer.id)
    db_session.commit()
    api_keys.rotate_key(db_session, old_row.id, customer.id)
    db_session.commit()

    # A second key, then try to rotate IT too -- ceiling/rotating-in-flight
    # guard should refuse a second concurrent rotation.
    second_row, _ = api_keys.mint_key(db_session, customer.id, name="second")
    db_session.commit()
    with pytest.raises(api_keys.KeyLimitExceeded):
        api_keys.rotate_key(db_session, second_row.id, customer.id)


def test_expired_rotating_key_is_treated_as_revoked_on_resolve(db_session):
    customer = _make_customer(db_session)
    old_row, old_key = api_keys.mint_key(db_session, customer.id)
    db_session.commit()
    _, _ = api_keys.rotate_key(db_session, old_row.id, customer.id)
    # Force the overlap window into the past directly.
    old_row.revoke_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()
    api_keys.invalidate(old_row.key_hash)

    resolved = api_keys.resolve_key(old_key)
    assert resolved is None or not resolved.is_usable()


def test_cache_invalidate_reflects_revoke_immediately(db_session):
    customer = _make_customer(db_session)
    row, full_key = api_keys.mint_key(db_session, customer.id)
    db_session.commit()

    first = api_keys.resolve_key(full_key)
    assert first.is_usable()

    api_keys.revoke_key(db_session, row.id, customer.id)
    db_session.commit()

    second = api_keys.resolve_key(full_key)
    assert not second.is_usable()  # invalidate() must have cleared the 60s cache


def test_revoke_invalidation_fires_on_the_callers_commit_not_before(db_session):
    """Item 2 regression (the `_invalidate_after_commit` half): `revoke_key`
    doesn't own the caller's transaction (`routers/account.py` commits
    right after it; `routers/billing.py`'s webhook handler calls it
    mid-handler and commits once at the very end, per E4). Confirm the
    cache is NOT actually cleared until `db.commit()` runs -- i.e. the
    invalidation really is wired to the commit event, not just to the
    `revoke_key` call itself (which would be the pre-fix, premature-
    invalidate behavior)."""
    customer = _make_customer(db_session)
    row, full_key = api_keys.mint_key(db_session, customer.id)
    db_session.commit()

    first = api_keys.resolve_key(full_key)
    assert first.is_usable()
    key_hash = row.key_hash

    api_keys.revoke_key(db_session, row.id, customer.id)
    # The immediate `invalidate()` inside `revoke_key` already dropped the
    # cache entry (defense in depth) -- re-seed it here to isolate the
    # after-commit half specifically: without the `_invalidate_after_commit`
    # hook, nothing would clear this re-seeded entry when `db.commit()`
    # below runs, and `resolve_key` would keep serving the stale value.
    with api_keys._cache_lock:
        api_keys._cache[key_hash] = (first, api_keys.time.monotonic() + 60.0)

    db_session.commit()  # the after_commit hook must fire exactly here

    third = api_keys.resolve_key(full_key)
    assert not third.is_usable()


def test_revoke_race_write_after_invalidate_is_never_cached(db_session, monkeypatch):
    """Item 2 regression (the tombstone half): the decisive defect codex/
    grok/the devil's-advocate all converged on -- a concurrent
    `resolve_key` cache MISS whose DB read raced a revoke's `invalidate()`
    (started before it, but finishes its cache WRITE after it) must not
    re-seed the cache with the stale pre-revoke resolution, since nothing
    would ever invalidate it a second time. This directly exercises
    `_tombstones`, the dialect-independent guard; the full two-transaction
    end-to-end race additionally needs the Postgres harness (per the
    fixlist's own verification-steps section), since SQLite's single
    shared connection can't express two genuinely concurrent
    transactions."""
    customer = _make_customer(db_session)
    row, full_key = api_keys.mint_key(db_session, customer.id)
    db_session.commit()
    key_hash = row.key_hash

    original_load = api_keys._load_resolved_key

    def _load_then_race_an_invalidate(db, key_prefix, presented_hash):
        resolved = original_load(db, key_prefix, presented_hash)
        # Simulate a concurrent revoke's `invalidate()` landing AFTER this
        # read started (it already has `resolved` in hand) but BEFORE this
        # read's result would be written to the cache below.
        api_keys.invalidate(key_hash)
        return resolved

    monkeypatch.setattr(api_keys, "_load_resolved_key", _load_then_race_an_invalidate)

    resolved = api_keys.resolve_key(full_key)
    assert resolved is not None
    assert resolved.status == "active"  # the read itself found a real, live key...
    # ...but it must NOT have been cached, since it raced an invalidate.
    assert key_hash not in api_keys._cache


def test_expire_stale_reveal_auto_revokes_unrevealed_key(db_session):
    """Round-2 amendment C7."""
    customer = _make_customer(db_session)
    row, full_key = api_keys.mint_key(db_session, customer.id)
    row.reveal_ciphertext = api_keys.encrypt_for_reveal(full_key)
    row.reveal_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()

    expired = api_keys.expire_stale_reveal(db_session, row)
    assert expired is True
    assert row.status == "revoked"
    assert row.reveal_ciphertext is None


def test_expire_stale_reveal_also_fires_for_a_rotating_key(db_session):
    """Fixlist item 9 regression (the `expire_stale_reveal` sub-defect):
    broadened from `status == 'active'` to `status in ('active',
    'rotating')`. Pre-fix, a `rotating` key with an expired, un-revealed
    reveal window could never lazily expire -- this call was always a
    no-op for it, so the reveal columns stayed set forever and
    `reveal_key_endpoint`'s expired-window branch kept returning
    `reveal_expired` with nothing ever cleaning it up."""
    customer = _make_customer(db_session)
    row, full_key = api_keys.mint_key(db_session, customer.id)
    db_session.commit()
    row.status = "rotating"
    row.revoke_at = datetime.now(timezone.utc) + timedelta(hours=1)  # still within overlap
    row.reveal_ciphertext = api_keys.encrypt_for_reveal(full_key)
    row.reveal_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)  # already expired
    db_session.commit()

    expired = api_keys.expire_stale_reveal(db_session, row)
    assert expired is True
    assert row.status == "revoked"
    assert row.reveal_ciphertext is None


def test_reveal_ciphertext_roundtrip(db_session):
    plaintext = "bc_live_" + "a" * 32
    ciphertext = api_keys.encrypt_for_reveal(plaintext)
    assert api_keys.decrypt_reveal(ciphertext) == plaintext


def test_decrypt_reveal_returns_none_when_reveal_key_unset(db_session, monkeypatch):
    """Fixlist item 8 regression: `decrypt_reveal` only caught
    `InvalidToken` -- `_get_fernet()` raises `RuntimeError` when
    `BILLCOMMONS_REVEAL_KEY` is unset, which used to propagate straight
    out as an unhandled 500 (defeating `reveal_key_endpoint`'s documented
    503-on-misconfiguration contract). Must return None instead."""
    plaintext = "bc_live_" + "a" * 32
    ciphertext = api_keys.encrypt_for_reveal(plaintext)

    monkeypatch.delenv("BILLCOMMONS_REVEAL_KEY", raising=False)
    api_keys._fernet = None  # module-global memoized Fernet -- force re-init
    try:
        assert api_keys.decrypt_reveal(ciphertext) is None
    finally:
        api_keys._fernet = None


def test_decrypt_reveal_returns_none_when_reveal_key_malformed(db_session, monkeypatch):
    """Fixlist item 8 regression, the `ValueError`/`binascii.Error` half:
    a `BILLCOMMONS_REVEAL_KEY` that is set but isn't a valid 32-byte
    urlsafe-base64 Fernet key must also return None, not propagate."""
    plaintext = "bc_live_" + "a" * 32
    ciphertext = api_keys.encrypt_for_reveal(plaintext)

    monkeypatch.setenv("BILLCOMMONS_REVEAL_KEY", "not-a-valid-fernet-key")
    api_keys._fernet = None
    try:
        assert api_keys.decrypt_reveal(ciphertext) is None
    finally:
        api_keys._fernet = None


# ---------------------------------------------------------------------------
# 2026-08-21 fix-pass regressions
# ---------------------------------------------------------------------------


def test_resolve_key_never_caches_an_unknown_key(db_session):
    """Item 1 regression: probing a garbage `bc_live_`-shaped key must never
    grow `_cache` -- pre-fix, every miss was stored as `(None, expiry)`,
    giving an unauthenticated remote party one permanent dict entry per
    probe on a `numReplicas=1` process."""
    api_keys.clear_cache()
    for i in range(50):
        assert api_keys.resolve_key(f"bc_live_{'z' * 24}{i:08d}") is None
    assert len(api_keys._cache) == 0


def test_resolve_key_cache_is_bounded_for_known_keys(db_session, monkeypatch):
    """Item 1 (defense in depth): even the positive (hit-only) cache is
    capped with an oldest-by-insertion eviction, same idiom as
    `_BoundedFixedWindowCounter`."""
    monkeypatch.setattr(api_keys, "_MAX_CACHE_ENTRIES", 5)
    api_keys.clear_cache()
    # A distinct customer per key -- the 2-active-key-per-customer ceiling
    # (R5) would otherwise raise `KeyLimitExceeded` well before 10 keys.
    for i in range(10):
        customer = _make_customer(db_session, f"bounded-{i}@example.com")
        _, full_key = api_keys.mint_key(db_session, customer.id, name=f"k{i}")
        db_session.commit()
        assert api_keys.resolve_key(full_key) is not None
    assert len(api_keys._cache) <= 5


def test_load_resolved_key_sees_canceled_subscription(db_session):
    """Item 9 (SPEC-DEV): `_load_resolved_key` must see a CANCELED
    subscription, not filter it out of the query -- pre-fix, a customer
    whose only subscription was canceled resolved with
    `subscription_status=None`, making `payment_required()`'s canceled
    branch permanently unreachable.

    2026-08-21 fix-pass decision E1 (SPEC-LOCKED "Post-verify decisions",
    resolving the A3-vs-B4 conflict this Phase-1 test's ORIGINAL assertion
    predated): a canceled subscription must NEVER 402 -- it drops the
    customer to the Developer tier instead (`billing.customer_plan`).
    `subscription_status` must still resolve to `'canceled'` (that's what
    THIS test guards), but `payment_required()` off of it is `False`."""
    customer = _make_customer(db_session, "canceled-sub@example.com")
    db_session.add(ApiSubscription(customer_id=customer.id, plan="builder", status="canceled"))
    db_session.commit()
    _, full_key = api_keys.mint_key(db_session, customer.id, plan="builder")
    db_session.commit()

    resolved = api_keys.resolve_key(full_key)
    assert resolved is not None
    assert resolved.subscription_status == "canceled"
    assert resolved.payment_required() is False


def test_load_resolved_key_prefers_non_canceled_over_newer_canceled(db_session):
    """Item 9's safe ordering: with one non-canceled and one (newer)
    canceled subscription row, the non-canceled one wins -- B4's invariant
    is <=1 non-canceled per customer, so it is always the authoritative
    one when it exists."""
    customer = _make_customer(db_session, "mixed-sub@example.com")
    older = ApiSubscription(customer_id=customer.id, plan="builder", status="active")
    db_session.add(older)
    db_session.commit()
    newer = ApiSubscription(customer_id=customer.id, plan="scale", status="canceled")
    db_session.add(newer)
    db_session.commit()
    _, full_key = api_keys.mint_key(db_session, customer.id, plan="builder")
    db_session.commit()

    resolved = api_keys.resolve_key(full_key)
    assert resolved.subscription_status == "active"
    assert resolved.payment_required() is False


def test_rotate_key_refreshes_stale_identity_map_entry(db_session):
    """Item 6 regression: `rotate_key` must see the CURRENT row state even
    if this session already has a stale copy of it in its identity map from
    an earlier load -- simulated here with a raw UPDATE that bypasses the
    ORM (standing in for a concurrent transaction's write becoming visible
    after this session already cached the object). Pre-fix, `old` was read
    BEFORE the customer-row lock and the identity map handed back the
    stale, already-loaded Python object with `status='active'` even though
    the row was actually already revoked."""
    customer = _make_customer(db_session)
    row, full_key = api_keys.mint_key(db_session, customer.id)
    db_session.commit()

    # Load it into the identity map once, as if an earlier step in this
    # request had already fetched it.
    db_session.execute(select(ApiKey).where(ApiKey.id == row.id)).scalar_one()

    # Simulate a concurrent revoke becoming visible: a raw UPDATE that does
    # not go through this session's ORM attribute tracking.
    db_session.execute(text("UPDATE api_keys SET status = 'revoked' WHERE id = :id"), {"id": row.id.hex})
    db_session.commit()

    with pytest.raises(api_keys.KeyLimitExceeded):
        api_keys.rotate_key(db_session, row.id, customer.id)


def test_revoke_key_refreshes_stale_identity_map_entry(db_session):
    """Item 6 regression, `revoke_key` half: takes the customer lock AND
    refreshes a stale identity-map entry before revoking."""
    customer = _make_customer(db_session)
    row, full_key = api_keys.mint_key(db_session, customer.id)
    db_session.commit()

    db_session.execute(select(ApiKey).where(ApiKey.id == row.id)).scalar_one()
    db_session.execute(text("UPDATE api_keys SET name = 'renamed' WHERE id = :id"), {"id": row.id.hex})
    db_session.commit()

    result = api_keys.revoke_key(db_session, row.id, customer.id)
    assert result is not None
    assert result.status == "revoked"
    assert result.name == "renamed"  # picked up the fresh row, not the stale cached one


def test_mint_developer_key_if_first_login_mints_exactly_once(db_session):
    """Item 3 (part 3 of 3): the auto-mint helper mints on the first call
    for a brand-new customer and declines on every call after (the customer
    now has a key)."""
    customer = _make_customer(db_session, "first-login@example.com")
    key1 = api_keys.mint_developer_key_if_first_login(db_session, customer.id)
    db_session.commit()
    assert key1 is not None and key1.startswith("bc_live_")

    key2 = api_keys.mint_developer_key_if_first_login(db_session, customer.id)
    db_session.commit()
    assert key2 is None


def test_mint_developer_key_if_first_login_skips_stripe_customers(db_session):
    """Amendment D5: a customer who has ever touched Stripe never gets a
    surprise auto-minted key from a login."""
    customer = _make_customer(db_session, "stripe-customer@example.com")
    customer.stripe_customer_id = "cus_test123"
    db_session.commit()

    assert api_keys.mint_developer_key_if_first_login(db_session, customer.id) is None


def test_mint_developer_key_if_first_login_skips_existing_subscription(db_session):
    customer = _make_customer(db_session, "has-sub@example.com")
    db_session.add(ApiSubscription(customer_id=customer.id, plan="builder", status="active"))
    db_session.commit()

    assert api_keys.mint_developer_key_if_first_login(db_session, customer.id) is None
