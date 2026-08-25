"""Tests for `billcommons_api.routers.billing` (2026-08-21 monetization
Phase 2 gates, `SPEC-LOCKED.md`) against the throwaway SQLite harness (see
`_monetization_sqlite.py`). `stripe.Webhook.construct_event` and every
outbound Stripe API call are monkeypatched -- this suite never talks to
the real Stripe API.
"""
from __future__ import annotations

import hashlib
import json
import secrets as secrets_mod
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import stripe
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

import billcommons_api.routers.billing as billing
from billcommons_schema.models import (
    AccountLoginToken,
    ApiCustomer,
    ApiKey,
    ApiSubscription,
    SnapshotEntitlement,
    StripeEvent,
)

from tests._monetization_sqlite import build_sqlite_app

ORIGIN = "https://billcommons.org"


@pytest.fixture()
def app_and_db(monkeypatch):
    monkeypatch.setenv("BILLCOMMONS_REVEAL_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ACCOUNT_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("BILLCOMMONS_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_fake")
    monkeypatch.setenv("STRIPE_PRICE_BUILDER_MONTHLY", "price_builder_monthly")
    monkeypatch.setenv("STRIPE_PRICE_BUILDER_ANNUAL", "price_builder_annual")
    monkeypatch.setenv("STRIPE_PRICE_SCALE_MONTHLY", "price_scale_monthly")
    monkeypatch.setenv("STRIPE_PRICE_SCALE_ANNUAL", "price_scale_annual")
    monkeypatch.setenv("STRIPE_PRICE_SNAPSHOT_STATE", "price_snapshot_state")
    monkeypatch.setenv("STRIPE_PRICE_SNAPSHOT_FULL", "price_snapshot_full")
    monkeypatch.setenv("OPERATOR_ALERT_EMAIL", "founder@example.com")

    app, SessionLocal = build_sqlite_app(monkeypatch)

    # Never let this suite make a real Resend call (mirrors
    # _monetization_sqlite's own RESEND_API_KEY delenv -- _send_email logs
    # at WARN with no key set).
    return app, SessionLocal


@pytest.fixture()
def client(app_and_db):
    app, _ = app_and_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _make_customer(SessionLocal, email="buyer@example.com", stripe_customer_id=None) -> uuid.UUID:
    db = SessionLocal()
    customer = ApiCustomer(email=email, stripe_customer_id=stripe_customer_id)
    db.add(customer)
    db.flush()
    db.commit()
    cid = customer.id
    db.close()
    return cid


def _log_in(client, SessionLocal, email: str) -> dict:
    """Issues a login token directly (skipping the email step) and
    consumes it via POST /account/session. Returns a `{"Cookie": ...}`
    header dict to pass explicitly on subsequent requests -- httpx's
    TestClient cookie jar does not reliably auto-attach a cookie set for
    bare host `testserver` across requests (a `testserver`/`testserver.local`
    domain-matching quirk unrelated to this app), so tests thread the
    session cookie through explicitly rather than relying on the jar."""
    token = secrets_mod.token_urlsafe(32)
    db = SessionLocal()
    db.add(
        AccountLoginToken(
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            email=email,
            purpose="login",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
            request_ip="203.0.113.99",
        )
    )
    db.commit()
    db.close()
    res = client.post(
        "/api/v1/account/session", json={"token": token}, headers={"Origin": ORIGIN}
    )
    assert res.status_code in (200, 204)
    cookie_value = res.cookies.get("bc_session")
    assert cookie_value
    return {"Cookie": f"bc_session={cookie_value}"}


def _stripe_event(event_type: str, obj: dict, created: int | None = None, event_id: str | None = None) -> dict:
    return {
        "id": event_id or f"evt_{uuid.uuid4().hex}",
        "type": event_type,
        "created": created if created is not None else int(datetime.now(timezone.utc).timestamp()),
        "data": {"object": obj},
    }


def _post_webhook(client, event: dict):
    return client.post(
        "/api/v1/billing/webhook",
        content=json.dumps(event),
        headers={"stripe-signature": "test", "Content-Type": "application/json"},
    )


@pytest.fixture(autouse=True)
def _bypass_signature_verification(monkeypatch):
    monkeypatch.setattr(stripe.Webhook, "construct_event", staticmethod(lambda payload, sig, secret: json.loads(payload)))


def _keys_for_customer(SessionLocal, customer_id: uuid.UUID) -> list[ApiKey]:
    db = SessionLocal()
    rows = db.execute(select(ApiKey).where(ApiKey.customer_id == customer_id)).scalars().all()
    db.close()
    return rows


def _stripe_event_row(SessionLocal, event_id: str) -> StripeEvent | None:
    db = SessionLocal()
    row = db.execute(select(StripeEvent).where(StripeEvent.id == event_id)).scalar_one_or_none()
    db.close()
    return row


def _subscription_row(SessionLocal, stripe_subscription_id: str) -> ApiSubscription | None:
    db = SessionLocal()
    row = db.execute(
        select(ApiSubscription).where(ApiSubscription.stripe_subscription_id == stripe_subscription_id)
    ).scalar_one_or_none()
    db.close()
    return row


# ---------------------------------------------------------------------------
# Checkout: Origin gate (B7), guest-capable (C2), 409 on second checkout (A4)
# ---------------------------------------------------------------------------


def test_checkout_without_origin_is_403(client):
    res = client.post("/api/v1/billing/checkout", json={"plan": "builder", "interval": "monthly"})
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "bad_origin"


def test_checkout_guest_capable(client, monkeypatch):
    """C2: no session cookie required, only the Origin check."""
    created = {}

    def _fake_create(**kwargs):
        created.update(kwargs)
        return {"url": "https://checkout.stripe.com/pay/cs_test_123", "id": "cs_test_123"}

    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(_fake_create))
    res = client.post(
        "/api/v1/billing/checkout",
        json={"plan": "builder", "interval": "monthly"},
        headers={"Origin": ORIGIN},
    )
    assert res.status_code == 200
    assert res.json()["url"].startswith("https://checkout.stripe.com")
    assert created["metadata"]["app"] == "billcommons"
    assert created["subscription_data"]["metadata"]["app"] == "billcommons"
    assert "customer" not in created and "customer_email" not in created


def test_checkout_409_when_active_subscription_exists(client, app_and_db, monkeypatch):
    _, SessionLocal = app_and_db
    email = "already-paying@example.com"
    customer_id = _make_customer(SessionLocal, email=email, stripe_customer_id="cus_existing")
    db = SessionLocal()
    db.add(
        ApiSubscription(
            customer_id=customer_id,
            stripe_subscription_id="sub_existing",
            plan="builder",
            status="active",
        )
    )
    db.commit()
    db.close()

    cookie_headers = _log_in(client, SessionLocal, email)
    monkeypatch.setattr(
        stripe.checkout.Session, "create", staticmethod(lambda **kw: {"url": "x", "id": "y"})
    )
    res = client.post(
        "/api/v1/billing/checkout",
        json={"plan": "scale", "interval": "monthly"},
        headers={"Origin": ORIGIN, **cookie_headers},
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "active_subscription_exists"


# ---------------------------------------------------------------------------
# Webhook: checkout.session.completed provisions exactly one key
# ---------------------------------------------------------------------------


def test_checkout_completed_mints_exactly_one_key(client, app_and_db, monkeypatch):
    _, SessionLocal = app_and_db
    monkeypatch.setattr(stripe.Subscription, "retrieve", staticmethod(lambda sub_id: {
        "id": sub_id, "customer": "cus_new", "status": "active",
        "metadata": {"app": "billcommons", "plan": "builder"},
        "current_period_end": int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp()),
        "cancel_at_period_end": False,
    }))

    event = _stripe_event(
        "checkout.session.completed",
        {
            "id": "cs_test_1",
            "mode": "subscription",
            "customer": "cus_new",
            "subscription": "sub_new_1",
            "customer_details": {"email": "new-buyer@example.com"},
            "metadata": {"app": "billcommons", "plan": "builder", "interval": "monthly"},
        },
    )
    res = _post_webhook(client, event)
    assert res.status_code == 200
    assert res.json()["outcome"] == "processed"

    customer = SessionLocal()
    row = customer.execute(select(ApiCustomer).where(ApiCustomer.email == "new-buyer@example.com")).scalar_one()
    keys = customer.execute(select(ApiKey).where(ApiKey.customer_id == row.id)).scalars().all()
    customer.close()
    assert len(keys) == 1
    assert keys[0].plan == "builder"
    assert keys[0].reveal_ciphertext is not None  # B1: never revealed inline for the checkout path


def test_webhook_replay_is_a_no_op(client, app_and_db, monkeypatch):
    _, SessionLocal = app_and_db
    monkeypatch.setattr(stripe.Subscription, "retrieve", staticmethod(lambda sub_id: {
        "id": sub_id, "customer": "cus_replay", "status": "active",
        "metadata": {"app": "billcommons", "plan": "builder"},
        "current_period_end": None, "cancel_at_period_end": False,
    }))
    event = _stripe_event(
        "checkout.session.completed",
        {
            "id": "cs_replay",
            "mode": "subscription",
            "customer": "cus_replay",
            "subscription": "sub_replay",
            "customer_details": {"email": "replay@example.com"},
            "metadata": {"app": "billcommons", "plan": "builder"},
        },
        event_id="evt_replay_1",
    )
    first = _post_webhook(client, event)
    assert first.status_code == 200
    assert first.json().get("duplicate") is not True

    second = _post_webhook(client, event)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True

    db = SessionLocal()
    cust = db.execute(select(ApiCustomer).where(ApiCustomer.email == "replay@example.com")).scalar_one()
    keys = db.execute(select(ApiKey).where(ApiKey.customer_id == cust.id)).scalars().all()
    db.close()
    assert len(keys) == 1, "replaying the same event id must not mint a second key"


def test_webhook_foreign_event_skipped(client, app_and_db):
    _, SessionLocal = app_and_db
    event = _stripe_event(
        "checkout.session.completed",
        {
            "id": "cs_foreign",
            "mode": "subscription",
            "customer": "cus_flhq",
            "customer_details": {"email": "flhq-client@example.com"},
            "metadata": {},  # no app tag -- e.g. an FLHQ Checkout Session on the same Stripe account
        },
    )
    res = _post_webhook(client, event)
    assert res.status_code == 200
    assert res.json()["outcome"] == "skipped_foreign_app"

    db = SessionLocal()
    cust = db.execute(select(ApiCustomer).where(ApiCustomer.email == "flhq-client@example.com")).scalar_one_or_none()
    db.close()
    assert cust is None, "a foreign event must never provision anything"


def test_invoice_with_no_own_metadata_but_billcommons_customer_is_processed(client, app_and_db):
    """A2 Phase-2 gate: the invoice object itself carries no metadata we
    check, but its `customer` matches a customer row we already own
    locally -- PROCESSED, not skipped."""
    _, SessionLocal = app_and_db
    _make_customer(SessionLocal, email="known@example.com", stripe_customer_id="cus_known")

    event = _stripe_event(
        "invoice.paid",
        {"id": "in_1", "customer": "cus_known", "subscription": None},
    )
    res = _post_webhook(client, event)
    assert res.status_code == 200
    assert res.json()["outcome"] == "processed"


def test_out_of_order_subscription_updated_before_checkout_completed(client, app_and_db, monkeypatch):
    """A4 gate: `customer.subscription.updated` lands BEFORE
    `checkout.session.completed` for the same subscription -- end state
    (exactly one key, minted with the subscription's plan) must be
    identical to the in-order case."""
    _, SessionLocal = app_and_db
    monkeypatch.setattr(
        stripe.Customer, "retrieve", staticmethod(lambda cid: {"id": cid, "email": "outoforder@example.com"})
    )

    sub_obj = {
        "id": "sub_ooo",
        "customer": "cus_ooo",
        "status": "active",
        "metadata": {"app": "billcommons", "plan": "builder"},
        "current_period_end": None,
        "cancel_at_period_end": False,
    }
    update_event = _stripe_event("customer.subscription.updated", sub_obj, created=1000)
    res1 = _post_webhook(client, update_event)
    assert res1.status_code == 200
    assert res1.json()["outcome"] == "processed"

    checkout_event = _stripe_event(
        "checkout.session.completed",
        {
            "id": "cs_ooo",
            "mode": "subscription",
            "customer": "cus_ooo",
            "subscription": "sub_ooo",
            "customer_details": {"email": "outoforder@example.com"},
            # deliberately a DIFFERENT plan in the checkout metadata, to
            # prove the key is minted with the subscription row's plan
            # (already synced), not re-derived from this event.
            "metadata": {"app": "billcommons", "plan": "scale"},
        },
        created=1001,
    )
    res2 = _post_webhook(client, checkout_event)
    assert res2.status_code == 200

    db = SessionLocal()
    cust = db.execute(select(ApiCustomer).where(ApiCustomer.email == "outoforder@example.com")).scalar_one()
    keys = db.execute(select(ApiKey).where(ApiKey.customer_id == cust.id)).scalars().all()
    db.close()
    assert len(keys) == 1
    assert keys[0].plan == "builder"


# ---------------------------------------------------------------------------
# Dunning: 402 after 7 days, invoice.paid clears it, stale invoice ignored
# ---------------------------------------------------------------------------


def _seed_active_subscription_and_key(SessionLocal, email, stripe_sub_id, plan="builder"):
    import billcommons_api.api_keys as api_keys_module

    db = SessionLocal()
    customer = ApiCustomer(email=email, stripe_customer_id=f"cus_{stripe_sub_id}")
    db.add(customer)
    db.flush()
    db.add(
        ApiSubscription(
            customer_id=customer.id,
            stripe_subscription_id=stripe_sub_id,
            plan=plan,
            status="active",
        )
    )
    db.commit()
    _, full_key = api_keys_module.mint_key(db, customer.id, plan=plan)
    db.commit()
    db.close()
    return full_key


def test_payment_failed_then_402_after_seven_days(client, app_and_db):
    import billcommons_api.api_keys as api_keys_module

    _, SessionLocal = app_and_db
    full_key = _seed_active_subscription_and_key(SessionLocal, "dunning@example.com", "sub_dun")

    event = _stripe_event(
        "invoice.payment_failed",
        {"id": "in_fail", "customer": "cus_sub_dun", "subscription": "sub_dun"},
        created=1_000_000,
    )
    res = _post_webhook(client, event)
    assert res.status_code == 200
    assert res.json()["outcome"] == "processed"

    row = _subscription_row(SessionLocal, "sub_dun")
    assert row.status == "past_due"
    assert row.past_due_since is not None

    # Not yet 402 -- past_due_since was just set to "now".
    api_keys_module.clear_cache()
    resolved = api_keys_module.resolve_key(full_key)
    assert resolved.payment_required() is False

    # Simulate 8 days passing.
    db = SessionLocal()
    live_row = db.execute(
        select(ApiSubscription).where(ApiSubscription.stripe_subscription_id == "sub_dun")
    ).scalar_one()
    live_row.past_due_since = datetime.now(timezone.utc) - timedelta(days=8)
    db.commit()
    db.close()

    api_keys_module.clear_cache()
    resolved = api_keys_module.resolve_key(full_key)
    assert resolved.payment_required() is True


def test_invoice_paid_newer_clears_past_due(client, app_and_db):
    _, SessionLocal = app_and_db
    _seed_active_subscription_and_key(SessionLocal, "cleared@example.com", "sub_clear")

    failed = _stripe_event(
        "invoice.payment_failed",
        {"id": "in_f", "customer": "cus_sub_clear", "subscription": "sub_clear"},
        created=2_000_000,
    )
    assert _post_webhook(client, failed).json()["outcome"] == "processed"
    row = _subscription_row(SessionLocal, "sub_clear")
    assert row.status == "past_due"

    paid = _stripe_event(
        "invoice.paid",
        {"id": "in_p", "customer": "cus_sub_clear", "subscription": "sub_clear"},
        created=2_000_100,  # newer than the failed event
    )
    assert _post_webhook(client, paid).json()["outcome"] == "processed"
    row = _subscription_row(SessionLocal, "sub_clear")
    assert row.status == "active"
    assert row.past_due_since is None


def test_stale_invoice_event_ignored(client, app_and_db):
    _, SessionLocal = app_and_db
    _seed_active_subscription_and_key(SessionLocal, "stale@example.com", "sub_stale")

    newer = _stripe_event(
        "invoice.payment_failed",
        {"id": "in_newer", "customer": "cus_sub_stale", "subscription": "sub_stale"},
        created=5_000_000,
    )
    assert _post_webhook(client, newer).json()["outcome"] == "processed"
    row = _subscription_row(SessionLocal, "sub_stale")
    assert row.status == "past_due"

    stale = _stripe_event(
        "invoice.paid",
        {"id": "in_stale", "customer": "cus_sub_stale", "subscription": "sub_stale"},
        created=4_999_000,  # OLDER than the failed event already applied
    )
    assert _post_webhook(client, stale).json()["outcome"] == "processed"
    row = _subscription_row(SessionLocal, "sub_stale")
    assert row.status == "past_due", "a stale (out-of-order, older) event must not undo a newer one"


# ---------------------------------------------------------------------------
# Duplicate subscription (C9/D2)
# ---------------------------------------------------------------------------


def test_duplicate_subscription_canceled_at_stripe(client, app_and_db, monkeypatch):
    _, SessionLocal = app_and_db
    customer_id = _make_customer(SessionLocal, email="dup@example.com", stripe_customer_id="cus_dup")
    db = SessionLocal()
    db.add(
        ApiSubscription(
            customer_id=customer_id,
            stripe_subscription_id="sub_first",
            plan="builder",
            status="active",
        )
    )
    db.commit()
    db.close()

    canceled_ids = []
    monkeypatch.setattr(stripe.Subscription, "cancel", staticmethod(lambda sub_id: canceled_ids.append(sub_id)))

    event = _stripe_event(
        "customer.subscription.created",
        {
            "id": "sub_second",
            "customer": "cus_dup",
            "status": "active",
            "metadata": {"app": "billcommons", "plan": "scale"},
            "current_period_end": None,
            "cancel_at_period_end": False,
        },
    )
    res = _post_webhook(client, event)
    assert res.status_code == 200
    assert res.json()["outcome"] == "duplicate_subscription_canceled"
    assert canceled_ids == ["sub_second"]
    # Item 8 fix: the duplicate is now RECORDED locally (status='canceled')
    # so a follow-up event for this same `sub_second` (Stripe will still
    # deliver `customer.subscription.updated`/`.deleted` for the sub it
    # just canceled) takes the normal update path instead of re-entering
    # this branch and re-calling `stripe.Subscription.cancel` on an
    # already-canceled subscription forever.
    dup_row = _subscription_row(SessionLocal, "sub_second")
    assert dup_row is not None
    assert dup_row.status == "canceled"

    # A follow-up event for the now-locally-known duplicate must not
    # re-enter the duplicate branch or call `.cancel` a second time.
    canceled_ids.clear()
    followup = _stripe_event(
        "customer.subscription.updated",
        {
            "id": "sub_second",
            "customer": "cus_dup",
            "status": "canceled",
            "metadata": {"app": "billcommons", "plan": "scale"},
            "current_period_end": None,
            "cancel_at_period_end": False,
        },
    )
    res2 = _post_webhook(client, followup)
    assert res2.status_code == 200
    assert res2.json()["outcome"] == "processed"
    assert canceled_ids == []


# ---------------------------------------------------------------------------
# Refunds (A7/B5/C5/C8/D3)
# ---------------------------------------------------------------------------


def _seed_snapshot_entitlement(SessionLocal, email, payment_intent_id, delivered=False):
    customer_id = _make_customer(SessionLocal, email=email, stripe_customer_id=f"cus_{payment_intent_id}")
    db = SessionLocal()
    db.add(
        SnapshotEntitlement(
            customer_id=customer_id,
            kind="one_time",
            scope="full",
            stripe_payment_intent_id=payment_intent_id,
            delivered_at=datetime.now(timezone.utc) if delivered else None,
        )
    )
    db.commit()
    db.close()
    return customer_id


def test_refund_before_delivery_revokes_entitlement(client, app_and_db):
    _, SessionLocal = app_and_db
    _seed_snapshot_entitlement(SessionLocal, "undelivered@example.com", "pi_undelivered", delivered=False)

    event = _stripe_event(
        "charge.refunded",
        {
            "id": "ch_1",
            "payment_intent": "pi_undelivered",
            "customer": "cus_pi_undelivered",
            "refunds": {"data": [{"metadata": {}}]},
        },
    )
    res = _post_webhook(client, event)
    assert res.status_code == 200

    db = SessionLocal()
    row = db.execute(
        select(SnapshotEntitlement).where(SnapshotEntitlement.stripe_payment_intent_id == "pi_undelivered")
    ).scalar_one()
    db.close()
    assert row.expires_at is not None


def test_refund_after_delivery_only_notifies_operator(client, app_and_db, monkeypatch):
    _, SessionLocal = app_and_db
    _seed_snapshot_entitlement(SessionLocal, "delivered@example.com", "pi_delivered", delivered=True)

    notified = []
    monkeypatch.setattr(
        billing, "_notify_operator", lambda background_tasks, subject, body: notified.append((subject, body))
    )

    event = _stripe_event(
        "charge.refunded",
        {
            "id": "ch_2",
            "payment_intent": "pi_delivered",
            "customer": "cus_pi_delivered",
            "refunds": {"data": [{"metadata": {}}]},
        },
    )
    res = _post_webhook(client, event)
    assert res.status_code == 200

    db = SessionLocal()
    row = db.execute(
        select(SnapshotEntitlement).where(SnapshotEntitlement.stripe_payment_intent_id == "pi_delivered")
    ).scalar_one()
    db.close()
    assert row.expires_at is None, "C5: a refund after delivery must not revoke the entitlement"
    assert any("already delivered" in subject for subject, _body in notified)


# ---------------------------------------------------------------------------
# Permanent errors (D4)
# ---------------------------------------------------------------------------


def test_permanent_error_recorded_not_500(client, app_and_db, monkeypatch):
    _, SessionLocal = app_and_db

    def _boom(customer_id):
        raise stripe.InvalidRequestError("No such customer", None, code="resource_missing")

    monkeypatch.setattr(stripe.Customer, "retrieve", staticmethod(_boom))

    event = _stripe_event(
        "invoice.paid",
        {"id": "in_missing", "customer": "cus_does_not_exist_locally_or_remotely", "subscription": None},
        event_id="evt_permanent_1",
    )
    res = _post_webhook(client, event)
    assert res.status_code == 200
    assert res.json()["outcome"] == "permanent_error"

    row = _stripe_event_row(SessionLocal, "evt_permanent_1")
    assert row is not None
    assert row.outcome == "permanent_error"
    assert row.last_error is not None


def test_transient_stripe_error_is_a_500(client, app_and_db, monkeypatch):
    """A genuine (non-resource_missing) Stripe API error during a parent
    lookup is NOT downgraded to skipped_foreign_app or permanent_error --
    it propagates, producing a 500 so Stripe retries (C4)."""

    def _boom(customer_id):
        raise stripe.APIConnectionError("network blip")

    monkeypatch.setattr(stripe.Customer, "retrieve", staticmethod(_boom))

    event = _stripe_event(
        "invoice.paid",
        {"id": "in_flaky", "customer": "cus_unreachable", "subscription": None},
        event_id="evt_transient_1",
    )
    res = _post_webhook(client, event)
    assert res.status_code == 500

    # The stripe_events row must have rolled back with the failed transaction.
    row = _stripe_event_row(SessionLocal := app_and_db[1], "evt_transient_1")
    assert row is None


# ---------------------------------------------------------------------------
# 2026-08-21 fix-pass regression tests (fixlist items, SPEC-LOCKED E1-E4)
# ---------------------------------------------------------------------------


def test_item1_cancel_drops_key_to_developer_not_402_and_row_plan_untouched(client, app_and_db):
    """Item 1/E1: a churned customer's KEY plan drops to Developer -- never
    stays on the paid plan, and never 402s just because they canceled
    (payment_required() is 402 only for unpaid/past_due>7d, per E1).
    `api_subscriptions.plan` itself is untouched (still 'builder') -- the
    CHECK constraint (`ck_api_subscriptions_plan`) would reject 'developer'
    there; only the KEY's plan becomes 'developer'."""
    import billcommons_api.api_keys as api_keys_module

    _, SessionLocal = app_and_db
    full_key = _seed_active_subscription_and_key(SessionLocal, "churn@example.com", "sub_churn", plan="builder")

    event = _stripe_event(
        "customer.subscription.deleted",
        {
            "id": "sub_churn",
            "customer": "cus_sub_churn",
            "status": "canceled",
            "metadata": {"app": "billcommons", "plan": "builder"},
        },
    )
    res = _post_webhook(client, event)
    assert res.status_code == 200
    assert res.json()["outcome"] == "processed"

    sub_row = _subscription_row(SessionLocal, "sub_churn")
    assert sub_row.status == "canceled"
    assert sub_row.plan == "builder", "the subscription row keeps the plan it was sold at"

    db = SessionLocal()
    key_row = db.execute(select(ApiKey).where(ApiKey.customer_id == sub_row.customer_id)).scalar_one()
    db.close()
    assert key_row.plan == "developer"

    api_keys_module.clear_cache()
    resolved = api_keys_module.resolve_key(full_key)
    assert resolved.plan == "developer"
    assert resolved.payment_required() is False, "E1: canceled must never 402"


def test_item2_portal_price_switch_flips_plan_from_price_id(client, app_and_db):
    """Item 2: a Portal plan switch changes `items[0].price` but Stripe
    never rewrites `subscription.metadata` -- the sync must follow the
    CURRENT price id, not the stale `metadata.plan` stamped at Checkout."""
    _seed_active_subscription_and_key(
        SessionLocal := app_and_db[1], "switcher@example.com", "sub_switch", plan="builder"
    )

    event = _stripe_event(
        "customer.subscription.updated",
        {
            "id": "sub_switch",
            "customer": "cus_sub_switch",
            "status": "active",
            # Deliberately STALE -- a real Portal switch never touches this.
            "metadata": {"app": "billcommons", "plan": "builder"},
            "items": {"data": [{"price": {"id": "price_scale_monthly"}, "current_period_end": None}]},
        },
    )
    res = _post_webhook(client, event)
    assert res.status_code == 200
    assert res.json()["outcome"] == "processed"

    row = _subscription_row(SessionLocal, "sub_switch")
    assert row.plan == "scale", "plan must follow the CURRENT price id, not stale metadata"

    db = SessionLocal()
    key_row = db.execute(select(ApiKey).where(ApiKey.customer_id == row.customer_id)).scalar_one()
    db.close()
    assert key_row.plan == "scale"


def test_item3_incomplete_expired_is_terminal_not_blocking(client, app_and_db):
    """Item 3: `incomplete_expired` must NOT count as an active subscription
    -- a customer whose only subscription row is `incomplete_expired`
    (a failed-first-payment Checkout that expired ~23h later) can start a
    NEW Checkout without hitting the 409 a `canceled`-only terminal check
    used to produce forever."""
    _, SessionLocal = app_and_db
    email = "retrying@example.com"
    customer_id = _make_customer(SessionLocal, email=email, stripe_customer_id="cus_retry")
    db = SessionLocal()
    db.add(
        ApiSubscription(
            customer_id=customer_id,
            stripe_subscription_id="sub_expired_incomplete",
            plan="builder",
            status="incomplete_expired",
        )
    )
    db.commit()
    db.close()

    cookie_headers = _log_in(client, SessionLocal, email)

    def _fake_create(**kwargs):
        return {"url": "https://checkout.stripe.com/pay/cs_retry", "id": "cs_retry"}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(stripe.checkout.Session, "create", staticmethod(_fake_create))
        # item 17's `_stripe_customer_id_for_checkout` tags the existing
        # Stripe Customer -- mock the real API call.
        mp.setattr(stripe.Customer, "modify", staticmethod(lambda cid, **kw: {"id": cid}))
        res = client.post(
            "/api/v1/billing/checkout",
            json={"plan": "builder", "interval": "monthly"},
            headers={"Origin": ORIGIN, **cookie_headers},
        )
    assert res.status_code == 200, res.text


def test_item4_checkout_endpoint_has_its_own_strict_rate_limit(client, monkeypatch):
    """Item 4/E2: `/billing/checkout` is an unauthenticated write against
    the shared Stripe account -- `QuotaMiddleware` exempts this whole
    router (A5), so this endpoint enforces its OWN limiter."""
    import time as time_mod

    from billcommons_api.rate_limit import _BoundedFixedWindowCounter

    monkeypatch.setattr(
        billing, "_checkout_ip_limiter", _BoundedFixedWindowCounter(1, 60.0, time_mod.monotonic, 100)
    )
    monkeypatch.setattr(
        billing, "_checkout_subnet_limiter", _BoundedFixedWindowCounter(1000, 60.0, time_mod.monotonic, 100)
    )
    monkeypatch.setattr(
        stripe.checkout.Session, "create", staticmethod(lambda **kw: {"url": "x", "id": "y"})
    )

    first = client.post(
        "/api/v1/billing/checkout",
        json={"plan": "builder", "interval": "monthly"},
        headers={"Origin": ORIGIN},
    )
    assert first.status_code == 200

    second = client.post(
        "/api/v1/billing/checkout",
        json={"plan": "builder", "interval": "monthly"},
        headers={"Origin": ORIGIN},
    )
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"
    assert "Retry-After" in second.headers


def test_item7_clover_shaped_invoice_and_subscription_payloads(client, app_and_db, monkeypatch):
    """Item 7/E3: `stripe==13.2.0` (`2025-03-31.basil`+) has NO top-level
    `Invoice.subscription` (moved to `invoice.parent.subscription_details.
    subscription`) and NO top-level `Subscription.current_period_end`
    (moved to `items.data[0].current_period_end`). Both shapes must parse."""
    _, SessionLocal = app_and_db
    monkeypatch.setattr(stripe.Subscription, "retrieve", staticmethod(lambda sub_id: {
        "id": sub_id,
        "customer": "cus_clover",
        "status": "active",
        "metadata": {"app": "billcommons", "plan": "builder"},
        "items": {
            "data": [
                {
                    "price": {"id": "price_builder_monthly"},
                    "current_period_end": int(
                        (datetime.now(timezone.utc) + timedelta(days=30)).timestamp()
                    ),
                }
            ]
        },
        "cancel_at_period_end": False,
    }))

    checkout_event = _stripe_event(
        "checkout.session.completed",
        {
            "id": "cs_clover",
            "mode": "subscription",
            "customer": "cus_clover",
            "subscription": "sub_clover",
            "customer_details": {"email": "clover@example.com"},
            "metadata": {"app": "billcommons", "plan": "builder"},
        },
    )
    res = _post_webhook(client, checkout_event)
    assert res.status_code == 200
    row = _subscription_row(SessionLocal, "sub_clover")
    assert row is not None
    assert row.current_period_end is not None, "basil+ shape (items.data[0].current_period_end) must parse"

    # basil+ invoice shape: no top-level `subscription`, only
    # `parent.subscription_details.subscription`.
    failed = _stripe_event(
        "invoice.payment_failed",
        {
            "id": "in_clover_fail",
            "customer": "cus_clover",
            "parent": {"subscription_details": {"subscription": "sub_clover"}},
        },
        # Postgres returns `last_event_created_at` already tz-AWARE (in
        # whatever the session's `timezone` GUC is -- not necessarily UTC);
        # SQLite hands back a naive one. `billing._aware` (a no-op on an
        # already-aware value) is the correct normalizer here -- a bare
        # `.replace(tzinfo=timezone.utc)` on an aware-but-non-UTC value
        # would silently SHIFT the instant instead of just labeling it.
        created=int(billing._aware(row.last_event_created_at).timestamp()) + 100,
    )
    res2 = _post_webhook(client, failed)
    assert res2.status_code == 200
    assert res2.json()["outcome"] == "processed"
    row2 = _subscription_row(SessionLocal, "sub_clover")
    assert row2.status == "past_due", "basil+ invoice.parent.subscription_details.subscription must resolve"


def test_item9_scale_to_builder_downgrade_expires_snapshot_entitlement(client, app_and_db):
    """Item 9: pre-fix, a Scale -> Builder downgrade (`status='active',
    plan='builder'`) matched NEITHER the cancel branch nor the
    `plan=='scale'` branch, so the customer kept an un-expiring full-corpus
    entitlement while paying Builder rates."""
    _, SessionLocal = app_and_db
    _seed_active_subscription_and_key(SessionLocal, "downgrade@example.com", "sub_downgrade", plan="scale")
    t0 = 2_000_000

    # First, land the Scale upgrade path to create the entitlement (already
    # 'scale' from seeding, but drive it through the handler once so the
    # entitlement row exists exactly like a real upgrade would create it).
    upgrade_event = _stripe_event(
        "customer.subscription.updated",
        {
            "id": "sub_downgrade",
            "customer": "cus_sub_downgrade",
            "status": "active",
            "metadata": {"app": "billcommons", "plan": "scale"},
        },
        created=t0 + 10,
    )
    assert _post_webhook(client, upgrade_event).json()["outcome"] == "processed"

    db = SessionLocal()
    customer_id = db.execute(select(ApiSubscription).where(ApiSubscription.stripe_subscription_id == "sub_downgrade")).scalar_one().customer_id
    ent = db.execute(
        select(SnapshotEntitlement).where(
            SnapshotEntitlement.customer_id == customer_id, SnapshotEntitlement.kind == "subscription"
        )
    ).scalar_one()
    db.close()
    assert ent.expires_at is None

    downgrade_event = _stripe_event(
        "customer.subscription.updated",
        {
            "id": "sub_downgrade",
            "customer": "cus_sub_downgrade",
            "status": "active",
            "metadata": {"app": "billcommons", "plan": "builder"},
        },
        created=t0 + 20,
    )
    assert _post_webhook(client, downgrade_event).json()["outcome"] == "processed"

    db = SessionLocal()
    ent2 = db.execute(
        select(SnapshotEntitlement).where(
            SnapshotEntitlement.customer_id == customer_id, SnapshotEntitlement.kind == "subscription"
        )
    ).scalar_one()
    db.close()
    assert ent2.expires_at is not None, "downgrade away from Scale must expire the entitlement"


def test_item10_resubscribe_to_scale_after_cancel_gets_fresh_entitlement(client, app_and_db):
    """Item 10: the re-subscribe probe used to match on `kind=='subscription'`
    with no `expires_at` filter, so a customer's EXPIRED row (stamped on
    their earlier cancel) always "counted" and a later resubscribe silently
    skipped creating a fresh entitlement."""
    _, SessionLocal = app_and_db
    _seed_active_subscription_and_key(SessionLocal, "resub@example.com", "sub_resub", plan="scale")
    t0 = 2_100_000

    upgrade_event = _stripe_event(
        "customer.subscription.updated",
        {"id": "sub_resub", "customer": "cus_sub_resub", "status": "active", "metadata": {"app": "billcommons", "plan": "scale"}},
        created=t0 + 10,
    )
    assert _post_webhook(client, upgrade_event).json()["outcome"] == "processed"

    cancel_event = _stripe_event(
        "customer.subscription.deleted",
        {"id": "sub_resub", "customer": "cus_sub_resub", "status": "canceled", "metadata": {"app": "billcommons"}},
        created=t0 + 20,
    )
    assert _post_webhook(client, cancel_event).json()["outcome"] == "processed"

    db = SessionLocal()
    customer_id = db.execute(select(ApiSubscription).where(ApiSubscription.stripe_subscription_id == "sub_resub")).scalar_one().customer_id
    db.close()

    new_sub_event = _stripe_event(
        "customer.subscription.created",
        {"id": "sub_resub_2", "customer": "cus_sub_resub", "status": "active", "metadata": {"app": "billcommons", "plan": "scale"}},
        created=t0 + 30,
    )
    assert _post_webhook(client, new_sub_event).json()["outcome"] == "processed"

    db = SessionLocal()
    active_ents = db.execute(
        select(SnapshotEntitlement).where(
            SnapshotEntitlement.customer_id == customer_id,
            SnapshotEntitlement.kind == "subscription",
            (SnapshotEntitlement.expires_at.is_(None)) | (SnapshotEntitlement.expires_at > datetime.now(timezone.utc)),
        )
    ).scalars().all()
    db.close()
    assert len(active_ents) == 1, "the resubscribe must create a FRESH, non-expired entitlement"


def test_item11_invoice_paid_after_deleted_does_not_revive_canceled(client, app_and_db):
    """Item 11: `invoice.paid` must never flip a `canceled` subscription
    back to `active` just because its `created` timestamp is newer than
    the `customer.subscription.deleted` that preceded it."""
    _, SessionLocal = app_and_db
    _seed_active_subscription_and_key(SessionLocal, "revive@example.com", "sub_revive")
    t0 = 2_200_000

    deleted = _stripe_event(
        "customer.subscription.deleted",
        {"id": "sub_revive", "customer": "cus_sub_revive", "status": "canceled", "metadata": {"app": "billcommons"}},
        created=t0 + 10,
    )
    assert _post_webhook(client, deleted).json()["outcome"] == "processed"
    assert _subscription_row(SessionLocal, "sub_revive").status == "canceled"

    late_paid = _stripe_event(
        "invoice.paid",
        {"id": "in_late", "customer": "cus_sub_revive", "subscription": "sub_revive"},
        created=t0 + 20,
    )
    res = _post_webhook(client, late_paid)
    assert res.status_code == 200
    assert _subscription_row(SessionLocal, "sub_revive").status == "canceled", (
        "a late invoice.paid must not revive a terminal cancellation"
    )


def test_item12_refund_cancel_access_reads_newest_not_oldest(client, app_and_db, monkeypatch):
    """Item 12: Stripe refund lists are newest-first. The OLDEST refund on
    a charge carries no `cancel_access` flag (a prior goodwill refund); the
    NEWEST one (index 0) is the one the runbook tells the operator to set
    `cancel_access="true"` on. `refunds.data[-1]` (the pre-fix read) would
    check the OLDEST and miss it."""
    _, SessionLocal = app_and_db
    customer_id = _make_customer(SessionLocal, email="multirefund@example.com", stripe_customer_id="cus_multi")
    db = SessionLocal()
    db.add(
        ApiSubscription(customer_id=customer_id, stripe_subscription_id="sub_multi", plan="builder", status="active")
    )
    db.commit()
    db.close()

    canceled_ids = []
    monkeypatch.setattr(stripe.Subscription, "cancel", staticmethod(lambda sub_id: canceled_ids.append(sub_id)))

    event = _stripe_event(
        "charge.refunded",
        {
            "id": "ch_multi",
            "customer": "cus_multi",
            "subscription": "sub_multi",
            # Newest-first: index 0 is the refund just issued, WITH the flag;
            # index 1 is an older goodwill refund with no metadata at all
            # (Stripe sends `"metadata": null` sometimes -- must not crash).
            "refunds": {"data": [{"metadata": {"cancel_access": "true"}}, {"metadata": None}]},
        },
    )
    res = _post_webhook(client, event)
    assert res.status_code == 200

    row = _subscription_row(SessionLocal, "sub_multi")
    assert row.status == "canceled"
    assert canceled_ids == ["sub_multi"]


def test_item16_unpaid_snapshot_checkout_does_not_provision(client, app_and_db):
    """Item 16: a delayed payment method can complete Checkout with
    `payment_status="unpaid"` -- money not yet received. Must not mint a
    key or record an entitlement until it's actually paid."""
    _, SessionLocal = app_and_db
    event = _stripe_event(
        "checkout.session.completed",
        {
            "id": "cs_unpaid",
            "mode": "payment",
            "payment_status": "unpaid",
            "customer_details": {"email": "notpaid@example.com"},
            "metadata": {"app": "billcommons", "scope": "full"},
            "payment_intent": "pi_unpaid",
        },
    )
    res = _post_webhook(client, event)
    assert res.status_code == 200
    assert res.json()["outcome"] == "processed"

    db = SessionLocal()
    ent = db.execute(
        select(SnapshotEntitlement).where(SnapshotEntitlement.stripe_payment_intent_id == "pi_unpaid")
    ).scalar_one_or_none()
    db.close()
    assert ent is None, "must not provision an entitlement before payment_status=='paid'"


def test_item17_subscription_event_falls_back_to_customer_metadata(client, app_and_db, monkeypatch):
    """Item 17: `customer.subscription.*` events checked ONLY the
    subscription object's own metadata, with no parent (customer) fallback
    -- contradicting this file's own documented A2 "subscription ->
    customer" rule."""
    _, SessionLocal = app_and_db
    monkeypatch.setattr(
        stripe.Customer,
        "retrieve",
        staticmethod(lambda cid: {"id": cid, "email": "tagged-customer@example.com", "metadata": {"app": "billcommons"}}),
    )

    event = _stripe_event(
        "customer.subscription.created",
        {
            "id": "sub_parent_fallback",
            "customer": "cus_parent_fallback",
            "status": "active",
            "metadata": {},  # no tag on the SUBSCRIPTION itself
        },
    )
    res = _post_webhook(client, event)
    assert res.status_code == 200
    assert res.json()["outcome"] == "processed", "must fall back to the CUSTOMER's own metadata.app tag"

    db = SessionLocal()
    cust = db.execute(select(ApiCustomer).where(ApiCustomer.email == "tagged-customer@example.com")).scalar_one_or_none()
    db.close()
    assert cust is not None


def test_item18_payment_failed_before_subscription_created_synthesizes_row(client, app_and_db, monkeypatch):
    """Item 18: Stripe makes no cross-event-type ordering guarantee --
    `invoice.payment_failed` can arrive before `customer.subscription.
    created` for the same subscription. Pre-fix, the missing local row
    made the handler drop the dunning signal entirely."""
    _, SessionLocal = app_and_db
    monkeypatch.setattr(stripe.Subscription, "retrieve", staticmethod(lambda sub_id: {
        "id": sub_id,
        "customer": "cus_ooo_invoice",
        "status": "active",
        "metadata": {"app": "billcommons", "plan": "builder"},
        "current_period_end": None,
        "cancel_at_period_end": False,
    }))
    monkeypatch.setattr(
        stripe.Customer,
        "retrieve",
        staticmethod(lambda cid: {"id": cid, "email": "ooo-invoice@example.com"}),
    )

    failed = _stripe_event(
        "invoice.payment_failed",
        {"id": "in_ooo", "customer": "cus_ooo_invoice", "subscription": "sub_ooo_invoice"},
        created=1_500_000,
    )
    res = _post_webhook(client, failed)
    assert res.status_code == 200
    assert res.json()["outcome"] == "processed"

    row = _subscription_row(SessionLocal, "sub_ooo_invoice")
    assert row is not None, "the subscription row must be SYNTHESIZED, not dropped"
    assert row.status == "past_due"
    assert row.past_due_since is not None


def test_item19_same_second_tie_prefers_terminal_transition(client, app_and_db):
    """Item 19: Stripe's `created` has one-second resolution -- an
    `updated` and the `deleted` that immediately follows it can share a
    timestamp. `>=` alone can't order them; a same-second CANCELED event
    must win over a same-second non-canceled one applied after it."""
    _, SessionLocal = app_and_db
    _seed_active_subscription_and_key(SessionLocal, "tie@example.com", "sub_tie")
    t0 = 2_300_000

    deleted = _stripe_event(
        "customer.subscription.deleted",
        {"id": "sub_tie", "customer": "cus_sub_tie", "status": "canceled", "metadata": {"app": "billcommons"}},
        created=t0 + 5,
    )
    assert _post_webhook(client, deleted).json()["outcome"] == "processed"
    assert _subscription_row(SessionLocal, "sub_tie").status == "canceled"

    # A same-second (not older) `updated` claiming "active" must NOT win.
    stale_tie = _stripe_event(
        "customer.subscription.updated",
        {
            "id": "sub_tie",
            "customer": "cus_sub_tie",
            "status": "active",
            "metadata": {"app": "billcommons", "plan": "builder"},
        },
        created=t0 + 5,
    )
    res = _post_webhook(client, stale_tie)
    assert res.status_code == 200
    assert _subscription_row(SessionLocal, "sub_tie").status == "canceled", (
        "a same-second non-canceled event must not un-cancel a terminal row"
    )


def test_item22_duplicate_snapshot_payment_intent_does_not_500_on_refund(client, app_and_db):
    """Item 22: a resent `checkout.session.completed` for the SAME
    `payment_intent` under a different event id must not create a second
    `snapshot_entitlements` row -- `ON CONFLICT DO NOTHING` makes it a
    no-op, so a later refund's `.scalars().first()` read never trips
    `MultipleResultsFound`."""
    _, SessionLocal = app_and_db

    def _mk_event(event_id):
        return _stripe_event(
            "checkout.session.completed",
            {
                "id": f"cs_{event_id}",
                "mode": "payment",
                "payment_status": "paid",
                "customer_details": {"email": "dedup@example.com"},
                "metadata": {"app": "billcommons", "scope": "full"},
                "payment_intent": "pi_dedup",
            },
            event_id=event_id,
        )

    assert _post_webhook(client, _mk_event("evt_dedup_1")).json()["outcome"] == "processed"
    assert _post_webhook(client, _mk_event("evt_dedup_2")).json()["outcome"] == "processed"

    db = SessionLocal()
    rows = db.execute(
        select(SnapshotEntitlement).where(SnapshotEntitlement.stripe_payment_intent_id == "pi_dedup")
    ).scalars().all()
    db.close()
    assert len(rows) == 1, "ON CONFLICT DO NOTHING must dedup the second insert"

    refund_event = _stripe_event(
        "charge.refunded",
        {
            "id": "ch_dedup",
            "payment_intent": "pi_dedup",
            "customer": "cus_dedup_missing",
            "refunds": {"data": [{"metadata": {}}]},
        },
    )
    res = _post_webhook(client, refund_event)
    assert res.status_code == 200, "must not 500 with MultipleResultsFound"


def test_item25_jurisdiction_rejects_path_traversal_shape(client):
    """Item 25 (Phase-3 landmine): `jurisdiction` must be exactly two
    letters -- Phase 3's snapshot builder is specified to turn this into a
    file path, and the pre-fix `str | None` capped at 8 chars let
    `"../AL"` through."""
    res = client.post(
        "/api/v1/billing/checkout/snapshot",
        json={"scope": "state", "jurisdiction": "../AL"},
        headers={"Origin": ORIGIN},
    )
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# 2026-08-21 round-2 fix-pass regression tests (E6/E7/E8, fixlist items)
# ---------------------------------------------------------------------------


def test_e6_subscription_active_before_checkout_completed_mints_exactly_one_paid_key(
    client, app_and_db, monkeypatch
):
    """E6/Gate A: `customer.subscription.updated` transitioning a
    subscription to `active` arrives BEFORE `checkout.session.completed`
    for the SAME subscription -- whichever event notices the
    active/trialing transition first must mint exactly one paid key,
    idempotent per subscription (`api_keys.subscription_id`, migration
    0021) regardless of which trigger wins the race."""
    _, SessionLocal = app_and_db
    monkeypatch.setattr(
        stripe.Customer, "retrieve", staticmethod(lambda cid: {"id": cid, "email": "e6-race@example.com"})
    )

    sub_obj = {
        "id": "sub_e6_race",
        "customer": "cus_e6_race",
        "status": "active",
        "metadata": {"app": "billcommons", "plan": "scale"},
        "current_period_end": None,
        "cancel_at_period_end": False,
    }
    update_event = _stripe_event("customer.subscription.updated", sub_obj, created=2_000_000)
    res1 = _post_webhook(client, update_event)
    assert res1.status_code == 200
    assert res1.json()["outcome"] == "processed"

    db = SessionLocal()
    cust = db.execute(select(ApiCustomer).where(ApiCustomer.email == "e6-race@example.com")).scalar_one()
    keys_after_sub_event = db.execute(select(ApiKey).where(ApiKey.customer_id == cust.id)).scalars().all()
    db.close()
    assert len(keys_after_sub_event) == 1, "the subscription-event path itself must mint (Gate A activation path)"
    assert keys_after_sub_event[0].plan == "scale"
    assert keys_after_sub_event[0].reveal_ciphertext is not None

    checkout_event = _stripe_event(
        "checkout.session.completed",
        {
            "id": "cs_e6_race",
            "mode": "subscription",
            "customer": "cus_e6_race",
            "subscription": "sub_e6_race",
            "customer_details": {"email": "e6-race@example.com"},
            "metadata": {"app": "billcommons", "plan": "scale"},
        },
        created=2_000_001,
    )
    res2 = _post_webhook(client, checkout_event)
    assert res2.status_code == 200

    db = SessionLocal()
    keys = db.execute(select(ApiKey).where(ApiKey.customer_id == cust.id)).scalars().all()
    db.close()
    assert len(keys) == 1, "checkout.session.completed arriving second must not mint a second key"


def test_e6_checkout_completed_with_incomplete_subscription_mints_nothing_and_no_409(
    client, app_and_db, monkeypatch
):
    """E6/item 1: `checkout.session.completed` fires while the
    subscription's first invoice is still unpaid (`status="incomplete"`)
    -- must mint nothing, and a later checkout retry by the same
    logged-in customer must NOT be 409'd (`incomplete` is not in
    `_BLOCKING_SUB_STATUSES`)."""
    _, SessionLocal = app_and_db
    monkeypatch.setattr(stripe.Subscription, "retrieve", staticmethod(lambda sub_id: {
        "id": sub_id, "customer": "cus_e6_incomplete", "status": "incomplete",
        "metadata": {"app": "billcommons", "plan": "builder"},
        "current_period_end": None, "cancel_at_period_end": False,
    }))

    event = _stripe_event(
        "checkout.session.completed",
        {
            "id": "cs_e6_incomplete",
            "mode": "subscription",
            "customer": "cus_e6_incomplete",
            "subscription": "sub_e6_incomplete",
            "customer_details": {"email": "e6-incomplete@example.com"},
            "metadata": {"app": "billcommons", "plan": "builder"},
        },
    )
    res = _post_webhook(client, event)
    assert res.status_code == 200
    assert res.json()["outcome"] == "processed"

    db = SessionLocal()
    cust = db.execute(
        select(ApiCustomer).where(ApiCustomer.email == "e6-incomplete@example.com")
    ).scalar_one()
    keys = db.execute(select(ApiKey).where(ApiKey.customer_id == cust.id)).scalars().all()
    db.close()
    assert keys == [], "an incomplete subscription must never mint a paid key"

    row = _subscription_row(SessionLocal, "sub_e6_incomplete")
    assert row.status == "incomplete"

    cookie_headers = _log_in(client, SessionLocal, "e6-incomplete@example.com")
    monkeypatch.setattr(
        stripe.checkout.Session, "create", staticmethod(lambda **kw: {"url": "https://checkout.stripe.com/x"})
    )
    # item 17's `_stripe_customer_id_for_checkout` re-tags the existing
    # Stripe Customer on every checkout attempt -- mock the real API call.
    monkeypatch.setattr(stripe.Customer, "modify", staticmethod(lambda cid, **kw: {"id": cid}))
    retry = client.post(
        "/api/v1/billing/checkout",
        json={"plan": "builder", "interval": "monthly"},
        headers={"Origin": ORIGIN, **cookie_headers},
    )
    assert retry.status_code == 200, "an incomplete subscription must not 409-lock a checkout retry"


def test_e6_invoice_payment_failed_on_never_paid_row_does_not_produce_past_due(client, app_and_db):
    """Item 1(d)/E6: `invoice.payment_failed` against a subscription's
    still-unpaid FIRST invoice (`status="incomplete"`) must not promote it
    to `past_due` -- `past_due` is plan-authoritative, so this would grant
    paid quota to a customer who never paid a cent."""
    _, SessionLocal = app_and_db
    customer_id = _make_customer(SessionLocal, email="e6-neverpaid@example.com", stripe_customer_id="cus_e6_np")
    db = SessionLocal()
    db.add(
        ApiSubscription(
            customer_id=customer_id,
            stripe_subscription_id="sub_e6_np",
            plan="builder",
            status="incomplete",
        )
    )
    db.commit()
    db.close()

    event = _stripe_event(
        "invoice.payment_failed",
        {"id": "in_e6_np", "customer": "cus_e6_np", "subscription": "sub_e6_np"},
    )
    res = _post_webhook(client, event)
    assert res.status_code == 200
    assert res.json()["outcome"] == "processed"

    row = _subscription_row(SessionLocal, "sub_e6_np")
    assert row.status == "incomplete", "a never-paid subscription must not become past_due"
    assert row.past_due_since is None


def test_e7_missing_stripe_price_env_is_500_not_permanent_error(client, app_and_db, monkeypatch):
    """E7/Gate B (rolls back round-1 item 23): our OWN misconfiguration
    (a missing price env var) is TRANSIENT -- it must 500, never be
    recorded/200'd as `permanent_error`. Exercised via the checkout
    endpoint, the one call site `_price_id` is actually reachable from;
    since this never reaches the webhook's `stripe_events` idempotency
    ledger, there is trivially no row for it to leave behind either."""
    monkeypatch.delenv("STRIPE_PRICE_BUILDER_MONTHLY", raising=False)
    res = client.post(
        "/api/v1/billing/checkout",
        json={"plan": "builder", "interval": "monthly"},
        headers={"Origin": ORIGIN},
    )
    assert res.status_code == 500

    _, SessionLocal = app_and_db
    db = SessionLocal()
    rows = db.execute(select(StripeEvent)).scalars().all()
    db.close()
    assert rows == [], "a checkout-endpoint misconfiguration must never touch stripe_events"


def test_e8_issue_login_token_failure_rolls_back_whole_webhook_transaction(client, app_and_db, monkeypatch):
    """E8: no helper called from inside the webhook may commit/rollback its
    caller's transaction -- `_issue_login_token` (called with `commit=False`
    from `billing._send_magic_link`) must propagate a failure instead of
    swallowing it, so the WHOLE webhook delivery 500s and the just-inserted
    `stripe_events` row (plus the customer/subscription/key it wrote)
    rolls back with it (item 2)."""
    _, SessionLocal = app_and_db
    monkeypatch.setattr(stripe.Subscription, "retrieve", staticmethod(lambda sub_id: {
        "id": sub_id, "customer": "cus_e8_boom", "status": "active",
        "metadata": {"app": "billcommons", "plan": "builder"},
        "current_period_end": None, "cancel_at_period_end": False,
    }))

    import billcommons_api.routers.account as account_module

    def _boom(db, email, request_ip, *, commit=True):
        raise RuntimeError("simulated token-issuance failure")

    monkeypatch.setattr(account_module, "_issue_login_token", _boom)

    event = _stripe_event(
        "checkout.session.completed",
        {
            "id": "cs_e8_boom",
            "mode": "subscription",
            "customer": "cus_e8_boom",
            "subscription": "sub_e8_boom",
            "customer_details": {"email": "e8-boom@example.com"},
            "metadata": {"app": "billcommons", "plan": "builder"},
        },
        event_id="evt_e8_boom",
    )
    res = _post_webhook(client, event)
    assert res.status_code == 500

    db = SessionLocal()
    stripe_event_row = db.execute(select(StripeEvent).where(StripeEvent.id == "evt_e8_boom")).scalar_one_or_none()
    customer_row = db.execute(select(ApiCustomer).where(ApiCustomer.email == "e8-boom@example.com")).scalar_one_or_none()
    subscription_row = db.execute(
        select(ApiSubscription).where(ApiSubscription.stripe_subscription_id == "sub_e8_boom")
    ).scalar_one_or_none()
    db.close()
    assert stripe_event_row is None, "the idempotency row must roll back with the rest of the transaction"
    assert customer_row is None, "the upserted customer must roll back too"
    assert subscription_row is None, "the synced subscription row must roll back too"


# ---------------------------------------------------------------------------
# 2026-08-25 R3 regressions
# ---------------------------------------------------------------------------


def test_r3_1_lost_provision_race_keeps_exactly_one_live_key(app_and_db, monkeypatch):
    """A second writer can win after the guard SELECT; the unique index
    backstop must turn our losing mint into an idempotent no-op."""
    import billcommons_api.api_keys as api_keys_module

    _, SessionLocal = app_and_db
    customer_id = _make_customer(SessionLocal, email="r3-race@example.com", stripe_customer_id="cus_r3_race")
    setup = SessionLocal()
    setup.add(
        ApiSubscription(
            customer_id=customer_id,
            stripe_subscription_id="sub_r3_race",
            plan="builder",
            status="active",
        )
    )
    setup.commit()
    setup.close()

    original_mint = billing._mint_paid_key_with_reveal

    def _second_session_wins(db, customer, plan, background_tasks, *, subscription_id=None):
        other = SessionLocal()
        try:
            winner, _ = api_keys_module.mint_key(other, customer.id, plan=plan)
            winner.subscription_id = subscription_id
            other.commit()
        finally:
            other.close()
        return original_mint(db, customer, plan, background_tasks, subscription_id=subscription_id)

    monkeypatch.setattr(billing, "_mint_paid_key_with_reveal", _second_session_wins)
    db = SessionLocal()
    customer = db.execute(select(ApiCustomer).where(ApiCustomer.id == customer_id)).scalar_one()
    subscription = db.execute(
        select(ApiSubscription).where(ApiSubscription.stripe_subscription_id == "sub_r3_race")
    ).scalar_one()
    billing._ensure_provisioned_for_subscription(db, customer, subscription, None)
    db.commit()
    keys = db.execute(select(ApiKey).where(ApiKey.subscription_id == subscription.id)).scalars().all()
    db.close()
    assert len(keys) == 1
    assert keys[0].status in ("active", "rotating")


def test_r3_4_checkout_existing_key_sends_one_magic_link(client, app_and_db, monkeypatch):
    import billcommons_api.api_keys as api_keys_module

    _, SessionLocal = app_and_db
    customer_id = _make_customer(SessionLocal, email="r3-email@example.com", stripe_customer_id="cus_r3_email")
    db = SessionLocal()
    api_keys_module.mint_key(db, customer_id, plan="developer")
    db.commit()
    db.close()
    sent_to = []
    monkeypatch.setattr(billing, "_send_magic_link", lambda db, customer, background_tasks: sent_to.append(customer.email))
    monkeypatch.setattr(
        stripe.Subscription,
        "retrieve",
        staticmethod(
            lambda sub_id: {
                "id": sub_id,
                "customer": "cus_r3_email",
                "status": "active",
                "metadata": {"app": "billcommons", "plan": "builder"},
            }
        ),
    )

    res = _post_webhook(
        client,
        _stripe_event(
            "checkout.session.completed",
            {
                "id": "cs_r3_email",
                "mode": "subscription",
                "customer": "cus_r3_email",
                "subscription": "sub_r3_email",
                "customer_details": {"email": "r3-email@example.com"},
                "metadata": {"app": "billcommons"},
            },
        ),
    )
    assert res.status_code == 200
    keys = _keys_for_customer(SessionLocal, customer_id)
    assert len(keys) == 1
    assert sent_to == ["r3-email@example.com"]


def test_r3_5_invoice_paid_recomputes_existing_key_plan(client, app_and_db):
    import billcommons_api.api_keys as api_keys_module

    _, SessionLocal = app_and_db
    customer_id = _make_customer(SessionLocal, email="r3-invoice@example.com", stripe_customer_id="cus_r3_invoice")
    db = SessionLocal()
    db.add(
        ApiSubscription(
            customer_id=customer_id,
            stripe_subscription_id="sub_r3_invoice",
            plan="scale",
            status="incomplete",
        )
    )
    _, full_key = api_keys_module.mint_key(db, customer_id, plan="developer")
    db.commit()
    db.close()

    res = _post_webhook(
        client,
        _stripe_event(
            "invoice.paid",
            {"id": "in_r3_invoice", "customer": "cus_r3_invoice", "subscription": "sub_r3_invoice"},
        ),
    )
    assert res.status_code == 200
    api_keys_module.clear_cache()
    assert api_keys_module.resolve_key(full_key).plan == "scale"


def test_r3_6_checkout_missing_subscription_is_permanent_error(client, app_and_db):
    _, SessionLocal = app_and_db
    event = _stripe_event(
        "checkout.session.completed",
        {
            "id": "cs_r3_missing_sub",
            "mode": "subscription",
            "customer": "cus_r3_missing_sub",
            "customer_details": {"email": "r3-missing-sub@example.com"},
            "metadata": {"app": "billcommons"},
        },
    )
    res = _post_webhook(client, event)
    assert res.status_code == 200
    assert res.json()["outcome"] == "permanent_error"
    assert _stripe_event_row(SessionLocal, event["id"]).outcome == "permanent_error"


def test_r3_9_deleted_stripe_customer_is_stale(app_and_db, monkeypatch):
    _, SessionLocal = app_and_db
    customer_id = _make_customer(SessionLocal, email="r3-deleted@example.com", stripe_customer_id="cus_deleted")
    monkeypatch.setattr(
        stripe.Customer, "retrieve", staticmethod(lambda stripe_customer_id: {"id": stripe_customer_id, "deleted": True})
    )
    db = SessionLocal()
    customer = billing._upsert_customer_by_email(db, "r3-deleted@example.com", "cus_replacement")
    db.commit()
    db.close()
    assert customer.id == customer_id
    assert customer.stripe_customer_id == "cus_replacement"


def test_r3_10_revoked_subscription_key_is_not_reminted(client, app_and_db):
    import billcommons_api.api_keys as api_keys_module

    _, SessionLocal = app_and_db
    customer_id = _make_customer(SessionLocal, email="r3-revoked@example.com", stripe_customer_id="cus_r3_revoked")
    db = SessionLocal()
    subscription = ApiSubscription(
        customer_id=customer_id,
        stripe_subscription_id="sub_r3_revoked",
        plan="builder",
        status="active",
    )
    db.add(subscription)
    db.flush()
    key, _ = api_keys_module.mint_key(db, customer_id, plan="builder")
    key.subscription_id = subscription.id
    key.status = "revoked"
    db.commit()
    db.close()

    updated = _post_webhook(
        client,
        _stripe_event(
            "customer.subscription.updated",
            {
                "id": "sub_r3_revoked",
                "customer": "cus_r3_revoked",
                "status": "active",
                "metadata": {"app": "billcommons", "plan": "builder"},
            },
            created=3_000_000,
        ),
    )
    paid = _post_webhook(
        client,
        _stripe_event(
            "invoice.paid",
            {"id": "in_r3_revoked", "customer": "cus_r3_revoked", "subscription": "sub_r3_revoked"},
            created=3_000_001,
        ),
    )
    assert updated.status_code == 200
    assert paid.status_code == 200
    keys = _keys_for_customer(SessionLocal, customer_id)
    assert len(keys) == 1
    assert keys[0].status == "revoked"


# ---------------------------------------------------------------------------
# 2026-08-25 R4 regressions
# ---------------------------------------------------------------------------


def test_r4_3_incomplete_scale_creates_no_entitlement_until_activation(client, app_and_db):
    _, SessionLocal = app_and_db
    customer_id = _make_customer(SessionLocal, "r4-scale@example.com", "cus_r4_scale")
    incomplete = _stripe_event(
        "customer.subscription.created",
        {
            "id": "sub_r4_scale",
            "customer": "cus_r4_scale",
            "status": "incomplete",
            "metadata": {"app": "billcommons", "plan": "scale"},
        },
        created=4_000_000,
    )
    assert _post_webhook(client, incomplete).status_code == 200
    db = SessionLocal()
    assert db.execute(
        select(SnapshotEntitlement).where(
            SnapshotEntitlement.customer_id == customer_id,
            SnapshotEntitlement.kind == "subscription",
        )
    ).scalar_one_or_none() is None
    db.close()

    active = _stripe_event(
        "customer.subscription.updated",
        {
            "id": "sub_r4_scale",
            "customer": "cus_r4_scale",
            "status": "active",
            "metadata": {"app": "billcommons", "plan": "scale"},
        },
        created=4_000_001,
    )
    assert _post_webhook(client, active).status_code == 200
    db = SessionLocal()
    entitlement = db.execute(
        select(SnapshotEntitlement).where(
            SnapshotEntitlement.customer_id == customer_id,
            SnapshotEntitlement.kind == "subscription",
        )
    ).scalar_one()
    db.close()
    assert entitlement.expires_at is None


def test_r4_5_invoice_paid_duplicate_active_is_canceled_not_500(client, app_and_db, monkeypatch):
    _, SessionLocal = app_and_db
    customer_id = _make_customer(SessionLocal, "r4-duplicate@example.com", "cus_r4_duplicate")
    db = SessionLocal()
    db.add_all(
        [
            ApiSubscription(customer_id=customer_id, stripe_subscription_id="sub_r4_b", plan="builder", status="active"),
            ApiSubscription(customer_id=customer_id, stripe_subscription_id="sub_r4_a", plan="scale", status="incomplete"),
        ]
    )
    db.commit()
    db.close()
    canceled = []
    monkeypatch.setattr(stripe.Subscription, "cancel", staticmethod(lambda sub_id: canceled.append(sub_id)))

    res = _post_webhook(
        client,
        _stripe_event(
            "invoice.paid",
            {"id": "in_r4_duplicate", "customer": "cus_r4_duplicate", "subscription": "sub_r4_a"},
            created=4_000_010,
        ),
    )
    assert res.status_code == 200
    assert res.json()["outcome"] == "duplicate_subscription_canceled"
    assert canceled == ["sub_r4_a"]
    assert _subscription_row(SessionLocal, "sub_r4_a").status == "canceled"
    assert _subscription_row(SessionLocal, "sub_r4_b").status == "active"


def test_r4_6_cancel_access_recomputes_expires_and_handles_terminal_stripe(client, app_and_db, monkeypatch):
    import billcommons_api.api_keys as api_keys_module

    _, SessionLocal = app_and_db
    customer_id = _make_customer(SessionLocal, "r4-refund@example.com", "cus_r4_refund")
    db = SessionLocal()
    subscription = ApiSubscription(
        customer_id=customer_id, stripe_subscription_id="sub_r4_refund", plan="scale", status="active"
    )
    db.add(subscription)
    db.flush()
    api_keys_module.mint_key(db, customer_id, plan="scale")
    db.add(SnapshotEntitlement(customer_id=customer_id, kind="subscription", scope="full"))
    db.commit()
    db.close()

    def _already_terminal(sub_id):
        raise stripe.InvalidRequestError("already canceled", None)

    monkeypatch.setattr(stripe.Subscription, "cancel", staticmethod(_already_terminal))
    res = _post_webhook(
        client,
        _stripe_event(
            "charge.refunded",
            {
                "id": "ch_r4_refund",
                "customer": "cus_r4_refund",
                "subscription": "sub_r4_refund",
                "refunds": {"data": [{"metadata": {"cancel_access": "true"}}]},
            },
            created=4_000_020,
        ),
    )
    assert res.status_code == 200
    db = SessionLocal()
    key = db.execute(select(ApiKey).where(ApiKey.customer_id == customer_id)).scalar_one()
    entitlement = db.execute(
        select(SnapshotEntitlement).where(SnapshotEntitlement.customer_id == customer_id)
    ).scalar_one()
    row = db.execute(select(ApiSubscription).where(ApiSubscription.id == subscription.id)).scalar_one()
    db.close()
    assert key.plan == "developer"
    assert entitlement.expires_at is not None
    assert row.status == "canceled"
    assert int(billing._aware(row.last_event_created_at).timestamp()) == 4_000_020


def test_r4_6_delayed_refund_only_targets_its_own_subscription(client, app_and_db, monkeypatch):
    _, SessionLocal = app_and_db
    customer_id = _make_customer(SessionLocal, "r4-old-refund@example.com", "cus_r4_old_refund")
    db = SessionLocal()
    db.add_all(
        [
            ApiSubscription(customer_id=customer_id, stripe_subscription_id="sub_r4_old", plan="builder", status="canceled"),
            ApiSubscription(customer_id=customer_id, stripe_subscription_id="sub_r4_current", plan="scale", status="active"),
        ]
    )
    db.commit()
    db.close()
    canceled = []
    monkeypatch.setattr(stripe.Subscription, "cancel", staticmethod(lambda sub_id: canceled.append(sub_id)))
    res = _post_webhook(
        client,
        _stripe_event(
            "charge.refunded",
            {
                "id": "ch_r4_old_refund",
                "customer": "cus_r4_old_refund",
                "invoice": {"subscription": "sub_r4_old"},
                "refunds": {"data": [{"metadata": {"cancel_access": "true"}}]},
            },
        ),
    )
    assert res.status_code == 200
    assert canceled == ["sub_r4_old"]
    assert _subscription_row(SessionLocal, "sub_r4_current").status == "active"


def test_r4_8_refund_before_snapshot_checkout_retries_then_revokes(client, app_and_db):
    _, SessionLocal = app_and_db
    refund = _stripe_event(
        "charge.refunded",
        {
            "id": "ch_r4_ordering",
            "payment_intent": "pi_r4_ordering",
            "metadata": {"app": "billcommons"},
            "refunds": {"data": [{"metadata": {}}]},
        },
        event_id="evt_r4_ordering_refund",
    )
    assert _post_webhook(client, refund).status_code == 500
    assert _stripe_event_row(SessionLocal, refund["id"]) is None

    checkout = _stripe_event(
        "checkout.session.completed",
        {
            "id": "cs_r4_ordering",
            "mode": "payment",
            "payment_status": "paid",
            "customer_details": {"email": "r4-ordering@example.com"},
            "metadata": {"app": "billcommons", "scope": "full"},
            "payment_intent": "pi_r4_ordering",
        },
    )
    assert _post_webhook(client, checkout).status_code == 200
    assert _post_webhook(client, refund).status_code == 200
    db = SessionLocal()
    entitlement = db.execute(
        select(SnapshotEntitlement).where(SnapshotEntitlement.stripe_payment_intent_id == "pi_r4_ordering")
    ).scalar_one()
    db.close()
    assert entitlement.expires_at is not None


def test_r4_10_expired_rotating_key_does_not_block_paid_mint(app_and_db):
    import billcommons_api.api_keys as api_keys_module

    _, SessionLocal = app_and_db
    customer_id = _make_customer(SessionLocal, "r4-expired-rotating@example.com")
    db = SessionLocal()
    subscription = ApiSubscription(
        customer_id=customer_id, stripe_subscription_id="sub_r4_rotating", plan="builder", status="active"
    )
    db.add(subscription)
    db.flush()
    expired, _ = api_keys_module.mint_key(db, customer_id, plan="developer")
    expired.status = "rotating"
    expired.revoke_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.flush()
    customer = db.execute(select(ApiCustomer).where(ApiCustomer.id == customer_id)).scalar_one()
    assert billing._ensure_provisioned_for_subscription(db, customer, subscription, None).minted is True
    db.commit()
    keys = db.execute(select(ApiKey).where(ApiKey.customer_id == customer_id)).scalars().all()
    db.close()
    assert len(keys) == 2
    assert any(key.subscription_id == subscription.id for key in keys)


def test_r4_14_fresh_checkout_queues_one_magic_link(client, app_and_db, monkeypatch):
    sent = []
    monkeypatch.setattr(
        stripe.Subscription,
        "retrieve",
        staticmethod(
            lambda sub_id: {
                "id": sub_id,
                "customer": "cus_r4_mail",
                "status": "active",
                "metadata": {"app": "billcommons", "plan": "builder"},
            }
        ),
    )
    monkeypatch.setattr(billing, "_send_magic_link", lambda db, customer, background_tasks: sent.append(customer.email))
    res = _post_webhook(
        client,
        _stripe_event(
            "checkout.session.completed",
            {
                "id": "cs_r4_mail",
                "mode": "subscription",
                "customer": "cus_r4_mail",
                "subscription": "sub_r4_mail",
                "customer_details": {"email": "r4-mail@example.com"},
                "metadata": {"app": "billcommons", "plan": "builder"},
            },
        ),
    )
    assert res.status_code == 200
    assert sent == ["r4-mail@example.com"]


def test_r5_refund_configures_stripe_before_payment_intent_retrieve(app_and_db, monkeypatch):
    _, SessionLocal = app_and_db
    configured = []
    monkeypatch.setattr(billing, "_configure_stripe", lambda: configured.append(True))

    def _retrieve(payment_intent_id):
        assert configured
        return {"id": payment_intent_id, "metadata": {}}

    monkeypatch.setattr(stripe.PaymentIntent, "retrieve", staticmethod(_retrieve))
    monkeypatch.setattr(stripe.checkout.Session, "list", staticmethod(lambda **kwargs: {"data": []}))
    monkeypatch.setattr(stripe.Refund, "list", staticmethod(lambda **kwargs: {"data": []}))
    db = SessionLocal()
    assert billing._handle_charge_refunded(db, {"payment_intent": "pi_r5_config"}, 1, None) == "skipped_foreign_app"
    db.close()


def test_r5_refund_uses_invoice_subscription_and_rejects_foreign_metadata(client, app_and_db, monkeypatch):
    _, SessionLocal = app_and_db
    own_customer = _make_customer(SessionLocal, "r5-own@example.com", "cus_r5_own")
    foreign_customer = _make_customer(SessionLocal, "r5-foreign@example.com", "cus_r5_foreign")
    db = SessionLocal()
    db.add(ApiSubscription(customer_id=own_customer, stripe_subscription_id="sub_r5_own", plan="builder", status="active"))
    db.add(ApiSubscription(customer_id=foreign_customer, stripe_subscription_id="sub_r5_foreign", plan="builder", status="active"))
    db.commit()
    db.close()
    canceled = []
    monkeypatch.setattr(stripe.Subscription, "cancel", staticmethod(lambda sub_id: canceled.append(sub_id)))
    res = _post_webhook(
        client,
        _stripe_event(
            "charge.refunded",
            {
                "id": "ch_r5_invoice",
                "customer": "cus_r5_own",
                "invoice": {"subscription": "sub_r5_own"},
                "metadata": {"stripe_subscription_id": "sub_r5_foreign"},
                "refunds": {"data": [{"metadata": {"cancel_access": "true"}}]},
            },
        ),
    )
    assert res.status_code == 200
    assert canceled == ["sub_r5_own"]
    assert _subscription_row(SessionLocal, "sub_r5_foreign").status == "active"


def test_r5_unresolvable_cancel_access_refund_is_permanent_and_notifies(client, app_and_db, monkeypatch):
    _, SessionLocal = app_and_db
    _make_customer(SessionLocal, "r5-unresolved@example.com", "cus_r5_unresolved")
    notices = []
    monkeypatch.setattr(billing, "_notify_operator", lambda *args: notices.append(args[1]))
    res = _post_webhook(
        client,
        _stripe_event(
            "charge.refunded",
            {
                "id": "ch_r5_unresolved",
                "customer": "cus_r5_unresolved",
                "refunds": {"data": [{"metadata": {"cancel_access": "true"}}]},
            },
        ),
    )
    assert res.status_code == 200
    assert res.json()["outcome"] == "permanent_error"
    assert any("could not be mapped" in subject for subject in notices)


def test_r5_delayed_old_refund_keeps_current_scale_entitlement(client, app_and_db, monkeypatch):
    _, SessionLocal = app_and_db
    customer_id = _make_customer(SessionLocal, "r5-entitlement@example.com", "cus_r5_entitlement")
    db = SessionLocal()
    db.add_all(
        [
            ApiSubscription(customer_id=customer_id, stripe_subscription_id="sub_r5_old", plan="builder", status="active"),
            ApiSubscription(customer_id=customer_id, stripe_subscription_id="sub_r5_scale", plan="scale", status="active"),
            SnapshotEntitlement(customer_id=customer_id, kind="subscription", scope="full"),
        ]
    )
    db.commit()
    db.close()
    monkeypatch.setattr(stripe.Subscription, "cancel", staticmethod(lambda sub_id: None))
    res = _post_webhook(
        client,
        _stripe_event(
            "charge.refunded",
            {
                "id": "ch_r5_old",
                "customer": "cus_r5_entitlement",
                "invoice": {"subscription": "sub_r5_old"},
                "refunds": {"data": [{"metadata": {"cancel_access": "true"}}]},
            },
        ),
    )
    assert res.status_code == 200
    db = SessionLocal()
    entitlement = db.execute(select(SnapshotEntitlement).where(SnapshotEntitlement.customer_id == customer_id)).scalar_one()
    db.close()
    assert entitlement.expires_at is None


def test_r5_guest_refund_converges_after_checkout_provisioning(client, app_and_db, monkeypatch):
    _, SessionLocal = app_and_db
    monkeypatch.setattr(stripe.PaymentIntent, "retrieve", staticmethod(lambda _: {"metadata": {"app": "billcommons"}}))
    monkeypatch.setattr(
        stripe.checkout.Session,
        "list",
        staticmethod(lambda **kwargs: {"data": [{"metadata": {"app": "billcommons"}, "customer_details": {"email": "r5-guest@example.com"}}]}),
    )
    refund = _stripe_event(
        "charge.refunded",
        {"id": "ch_r5_guest", "payment_intent": "pi_r5_guest", "refunds": {"data": [{"metadata": {}}]}},
        event_id="evt_r5_guest_refund",
    )
    assert _post_webhook(client, refund).status_code == 500
    assert _stripe_event_row(SessionLocal, refund["id"]) is None
    _make_customer(SessionLocal, "r5-guest@example.com")
    assert _post_webhook(client, refund).status_code == 200


def test_r5_invalid_metadata_still_applies_terminal_transition(client, app_and_db):
    _, SessionLocal = app_and_db
    customer_id = _make_customer(SessionLocal, "r5-invalid-plan@example.com", "cus_r5_invalid_plan")
    db = SessionLocal()
    db.add(ApiSubscription(customer_id=customer_id, stripe_subscription_id="sub_r5_invalid_plan", plan="builder", status="active"))
    db.commit()
    db.close()
    res = _post_webhook(
        client,
        _stripe_event(
            "customer.subscription.deleted",
            {"id": "sub_r5_invalid_plan", "customer": "cus_r5_invalid_plan", "metadata": {"app": "billcommons", "plan": "bogus"}},
        ),
    )
    assert res.status_code == 200
    assert _subscription_row(SessionLocal, "sub_r5_invalid_plan").status == "canceled"


def test_r5_new_paid_subscription_replaces_dunning_incumbent(client, app_and_db, monkeypatch):
    _, SessionLocal = app_and_db
    customer_id = _make_customer(SessionLocal, "r5-dunning@example.com", "cus_r5_dunning")
    db = SessionLocal()
    db.add(ApiSubscription(customer_id=customer_id, stripe_subscription_id="sub_r5_dunning", plan="builder", status="past_due"))
    db.commit()
    db.close()
    canceled = []
    monkeypatch.setattr(stripe.Subscription, "cancel", staticmethod(lambda sub_id: canceled.append(sub_id)))
    res = _post_webhook(
        client,
        _stripe_event(
            "customer.subscription.created",
            {"id": "sub_r5_paid", "customer": "cus_r5_dunning", "status": "active", "metadata": {"app": "billcommons", "plan": "scale"}},
        ),
    )
    assert res.status_code == 200
    assert canceled == ["sub_r5_dunning"]
    assert _subscription_row(SessionLocal, "sub_r5_dunning").status == "canceled"
    assert _subscription_row(SessionLocal, "sub_r5_paid").status == "active"


def test_r5_nonterminal_stripe_cancel_error_retries_webhook(client, app_and_db, monkeypatch):
    _, SessionLocal = app_and_db
    customer_id = _make_customer(SessionLocal, "r5-cancel-error@example.com", "cus_r5_cancel_error")
    db = SessionLocal()
    db.add(ApiSubscription(customer_id=customer_id, stripe_subscription_id="sub_r5_incumbent", plan="builder", status="active"))
    db.commit()
    db.close()
    monkeypatch.setattr(
        stripe.Subscription,
        "cancel",
        staticmethod(lambda sub_id: (_ for _ in ()).throw(stripe.InvalidRequestError("temporary Stripe failure", None))),
    )
    event = _stripe_event(
        "customer.subscription.created",
        {"id": "sub_r5_new", "customer": "cus_r5_cancel_error", "status": "active", "metadata": {"app": "billcommons", "plan": "builder"}},
    )
    assert _post_webhook(client, event).status_code == 500
    assert _stripe_event_row(SessionLocal, event["id"]) is None
