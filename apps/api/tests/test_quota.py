"""Integration tests for `billcommons_api.quota.QuotaMiddleware` (2026-08-21
monetization spec, Phase 1 gates) against the throwaway SQLite harness (see
`_monetization_sqlite.py`; conftest.py's live-Postgres fixtures don't have
migration 0019's tables until the operator applies it).

Two ad hoc routes are added to the app AFTER `create_app()` for tests that
need a deterministic 200/500 without touching any real business table:
`/api/v1/_test_ok` and `/api/v1/_test_boom`. Neither collides with a real
router path, and both are ordinary (non-heavy, non-exempt) routes so the
full middleware pipeline still applies to them.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from datetime import date, datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

import billcommons_api.api_keys as api_keys
import billcommons_api.quota as quota_module
import billcommons_api.routers.account as account_module
import billcommons_shared.plans as plans_module
from billcommons_schema.models import ApiCustomer, ApiKey, ApiSubscription

from tests._monetization_sqlite import build_sqlite_app


@pytest.fixture()
def small_plans(monkeypatch):
    """Small enough limits to exercise burst/quota exhaustion in a handful
    of requests, without waiting for a real per-minute/per-day window."""
    from billcommons_shared.plans import PlanLimits

    # Burst deliberately left large here (well above anything these quota
    # tests issue) so the daily-quota gate is the one that trips, not the
    # burst gate -- burst has its own dedicated small-limit test below.
    small = {
        "developer": PlanLimits(requests_per_day=5, heavy_per_day=2, burst_per_minute=1000),
        "builder": PlanLimits(requests_per_day=50, heavy_per_day=10, burst_per_minute=1000),
        "scale": PlanLimits(requests_per_day=500, heavy_per_day=100, burst_per_minute=1000),
        "enterprise": PlanLimits(requests_per_day=5000, heavy_per_day=1000, burst_per_minute=1000),
    }
    monkeypatch.setattr(plans_module, "PLAN_LIMITS", small)
    return small


@pytest.fixture()
def app_and_db(monkeypatch, small_plans):
    monkeypatch.setenv("BILLCOMMONS_REVEAL_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ACCOUNT_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("BILLCOMMONS_ADMIN_TOKEN", "test-admin-token")
    # Anonymous tiers small too, so the anonymous-path test doesn't need
    # hundreds of requests.
    monkeypatch.setenv("BILLCOMMONS_API_RATE_LIMIT_DEFAULT", "3/minute")
    monkeypatch.setenv("BILLCOMMONS_ANON_DAILY_LIMIT", "1000")
    monkeypatch.setenv("BILLCOMMONS_ANON_DAILY_LIMIT_SUBNET", "2000")

    app, SessionLocal = build_sqlite_app(monkeypatch)

    @app.get("/api/v1/_test_ok")
    def _ok():
        return {"ok": True}

    @app.get("/api/v1/_test_boom")
    def _boom():
        raise RuntimeError("boom")

    return app, SessionLocal


@pytest.fixture()
def client(app_and_db):
    app, _ = app_and_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _mint_key(SessionLocal, email="dev@example.com", plan="developer", environment="live"):
    db = SessionLocal()
    customer = ApiCustomer(email=email)
    db.add(customer)
    db.flush()
    db.commit()
    row, full_key = api_keys.mint_key(db, customer.id, environment=environment, plan=plan)
    db.commit()
    db.close()
    return customer, row, full_key


# ---------------------------------------------------------------------------
# Anonymous path
# ---------------------------------------------------------------------------


def test_anonymous_response_shape_unchanged(client):
    headers = {"X-Forwarded-For": "203.0.113.10"}
    res = client.get("/api/v1/_test_ok", headers=headers)
    assert res.status_code == 200
    assert "X-RateLimit-Limit" in res.headers
    assert "X-RateLimit-Remaining" in res.headers
    assert "X-RateLimit-Reset" in res.headers
    # Anonymous responses never carry keyed-only headers.
    assert "X-Quota-Limit" not in res.headers
    assert "X-Plan" not in res.headers


def test_anonymous_429_envelope_shape(client):
    headers = {"X-Forwarded-For": "203.0.113.11"}
    statuses = [client.get("/api/v1/_test_ok", headers=headers).status_code for _ in range(6)]
    assert 429 in statuses
    res = client.get("/api/v1/_test_ok", headers=headers)
    assert res.status_code == 429
    body = res.json()
    assert body["error"]["code"] == "rate_limited"
    assert "docs" in body["error"]
    assert "Retry-After" in res.headers


def test_trusted_client_bypasses_everything(client, monkeypatch):
    monkeypatch.setenv("BILLCOMMONS_INTERNAL_CLIENT_SECRET", "shhh")
    headers = {"X-Forwarded-For": "203.0.113.12", "X-Billcommons-Internal": "shhh"}
    statuses = [client.get("/api/v1/_test_ok", headers=headers).status_code for _ in range(10)]
    assert all(s == 200 for s in statuses)


# ---------------------------------------------------------------------------
# Keyed path -- auth
# ---------------------------------------------------------------------------


def test_unknown_key_401_with_www_authenticate(client):
    res = client.get(
        "/api/v1/_test_ok", headers={"Authorization": "Bearer bc_live_" + "z" * 32}
    )
    assert res.status_code == 401
    assert res.headers["WWW-Authenticate"] == "Bearer"
    assert res.json()["error"]["code"] == "invalid_api_key"


def test_revoked_key_401(client, app_and_db):
    _, SessionLocal = app_and_db
    customer, row, full_key = _mint_key(SessionLocal)
    db = SessionLocal()
    api_keys.revoke_key(db, row.id, customer.id)
    db.commit()
    db.close()

    res = client.get("/api/v1/_test_ok", headers={"Authorization": f"Bearer {full_key}"})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "invalid_api_key"


def test_valid_key_raises_ceiling_past_anonymous_limit(client, app_and_db):
    """A key's own (much larger) quota lets it keep succeeding well past
    the point an anonymous caller from the same IP would have been 429'd."""
    _, SessionLocal = app_and_db
    _, _, full_key = _mint_key(SessionLocal, plan="scale")  # 100/min burst, 500/day
    headers = {"Authorization": f"Bearer {full_key}", "X-Forwarded-For": "203.0.113.20"}
    statuses = [client.get("/api/v1/_test_ok", headers=headers).status_code for _ in range(5)]
    assert all(s == 200 for s in statuses)


def test_r4_1_valid_key_bypasses_saturated_anonymous_buckets(client, app_and_db, monkeypatch):
    """A valid paid key must resolve before the anonymous probe guard.
    Saturating both anonymous windows from its IP cannot turn it into a 429."""
    monkeypatch.setenv("BILLCOMMONS_ANON_DAILY_LIMIT", "3")
    monkeypatch.setenv("BILLCOMMONS_ANON_DAILY_LIMIT_SUBNET", "3")
    _, SessionLocal = app_and_db
    _, _, full_key = _mint_key(SessionLocal, email="r4-keyed@example.com", plan="builder")
    ip_headers = {"X-Forwarded-For": "203.0.113.201"}
    assert [client.get("/api/v1/_test_ok", headers=ip_headers).status_code for _ in range(3)] == [200, 200, 200]
    res = client.get(
        "/api/v1/_test_ok",
        headers={**ip_headers, "Authorization": f"Bearer {full_key}"},
    )
    assert res.status_code == 200
    assert res.headers["X-Plan"] == "builder"


def test_test_mode_key_gets_test_environment(client, app_and_db):
    _, SessionLocal = app_and_db
    _, _, full_key = _mint_key(SessionLocal, environment="test", plan="builder")
    assert full_key.startswith("bc_test_")
    res = client.get("/api/v1/_test_ok", headers={"Authorization": f"Bearer {full_key}"})
    assert res.status_code == 200
    assert res.headers["X-Plan"] == "builder"


def test_suspended_customer_403(client, app_and_db):
    _, SessionLocal = app_and_db
    customer, _, full_key = _mint_key(SessionLocal)
    db = SessionLocal()
    from sqlalchemy import select

    row = db.execute(select(ApiCustomer).where(ApiCustomer.id == customer.id)).scalar_one()
    row.suspended_at = datetime.now(timezone.utc)
    row.suspension_reason = "test"
    db.commit()
    db.close()

    res = client.get("/api/v1/_test_ok", headers={"Authorization": f"Bearer {full_key}"})
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "account_suspended"


# ---------------------------------------------------------------------------
# Quota / burst
# ---------------------------------------------------------------------------


def test_quota_exceeded_429_with_headers(client, app_and_db):
    _, SessionLocal = app_and_db
    # developer: requests_per_day=5, effective ceiling = floor(5*1.10) = 5
    _, _, full_key = _mint_key(SessionLocal, plan="developer")
    headers = {"Authorization": f"Bearer {full_key}"}
    statuses = [client.get("/api/v1/_test_ok", headers=headers).status_code for _ in range(8)]
    assert 429 in statuses
    res = client.get("/api/v1/_test_ok", headers=headers)
    assert res.status_code == 429
    body = res.json()
    assert body["error"]["code"] == "quota_exceeded"
    assert "X-Quota-Limit" in res.headers
    assert res.headers["X-Quota-Remaining"] == "0"
    assert int(res.headers["Retry-After"]) > 0
    # Retry-After should be seconds to UTC midnight -- within a day, always.
    assert int(res.headers["Retry-After"]) <= 86400


def test_5xx_responses_are_not_counted_against_quota(client, app_and_db):
    _, SessionLocal = app_and_db
    _, _, full_key = _mint_key(SessionLocal, plan="developer")
    headers = {"Authorization": f"Bearer {full_key}"}

    for _ in range(10):
        res = client.get("/api/v1/_test_boom", headers=headers)
        assert res.status_code == 500

    # Quota was never consumed by the 5xx responses -- a subsequent 200
    # should still report full remaining quota.
    ok = client.get("/api/v1/_test_ok", headers=headers)
    assert ok.status_code == 200
    assert int(ok.headers["X-Quota-Remaining"]) == 4  # 5 - this one successful request


def test_two_keys_of_one_customer_share_one_daily_quota(client, app_and_db):
    """Round-2 amendment C1."""
    _, SessionLocal = app_and_db
    db = SessionLocal()
    customer = ApiCustomer(email="shared@example.com")
    db.add(customer)
    db.flush()
    db.commit()
    _, key_a = api_keys.mint_key(db, customer.id, name="a", plan="developer")
    db.commit()
    _, key_b = api_keys.mint_key(db, customer.id, name="b", plan="developer")
    db.commit()
    db.close()

    # developer effective daily ceiling = floor(5*1.10) = 5
    statuses = []
    for i in range(6):
        key = key_a if i % 2 == 0 else key_b
        res = client.get("/api/v1/_test_ok", headers={"Authorization": f"Bearer {key}"})
        statuses.append(res.status_code)
    assert 429 in statuses, "two keys of one customer must share ONE daily budget"


def test_two_keys_of_one_customer_share_the_burst(monkeypatch):
    """Round-2 amendment D6. Uses its OWN small-burst plan config (large
    daily quota, tiny burst) so it's the burst gate that trips here, not
    the daily-quota gate the fixture above is tuned for."""
    from billcommons_shared.plans import PlanLimits

    monkeypatch.setattr(
        plans_module,
        "PLAN_LIMITS",
        {
            "developer": PlanLimits(requests_per_day=5000, heavy_per_day=1000, burst_per_minute=3),
            "builder": PlanLimits(requests_per_day=5000, heavy_per_day=1000, burst_per_minute=3),
            "scale": PlanLimits(requests_per_day=5000, heavy_per_day=1000, burst_per_minute=3),
            "enterprise": PlanLimits(requests_per_day=5000, heavy_per_day=1000, burst_per_minute=3),
        },
    )
    monkeypatch.setenv("BILLCOMMONS_REVEAL_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ACCOUNT_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("BILLCOMMONS_ADMIN_TOKEN", "test-admin-token")

    app, SessionLocal = build_sqlite_app(monkeypatch)

    @app.get("/api/v1/_test_ok")
    def _ok():
        return {"ok": True}

    db = SessionLocal()
    customer = ApiCustomer(email="burst-shared@example.com")
    db.add(customer)
    db.flush()
    db.commit()
    _, key_a = api_keys.mint_key(db, customer.id, name="a", plan="developer")
    db.commit()
    _, key_b = api_keys.mint_key(db, customer.id, name="b", plan="developer")
    db.commit()
    db.close()

    with TestClient(app, raise_server_exceptions=False) as client:
        # developer burst_per_minute=3 -- alternate keys past that in one minute.
        statuses = []
        for i in range(5):
            key = key_a if i % 2 == 0 else key_b
            res = client.get("/api/v1/_test_ok", headers={"Authorization": f"Bearer {key}"})
            statuses.append(res.status_code)
    assert 429 in statuses, "two keys of one customer must share ONE burst budget"


# ---------------------------------------------------------------------------
# Admin usage endpoint (R13) + account CSRF/Origin gate (B7)
# ---------------------------------------------------------------------------


def test_admin_usage_404_on_bad_token(client):
    res = client.get("/api/v1/admin/usage", headers={"Authorization": "Bearer wrong"})
    assert res.status_code == 404


def test_admin_usage_404_when_no_token_configured(monkeypatch):
    monkeypatch.setenv("BILLCOMMONS_REVEAL_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("BILLCOMMONS_ADMIN_TOKEN", raising=False)
    app, _ = build_sqlite_app(monkeypatch)
    with TestClient(app) as c:
        res = c.get("/api/v1/admin/usage", headers={"Authorization": "Bearer anything"})
    assert res.status_code == 404


def test_admin_usage_200_on_correct_token(client, app_and_db):
    _, SessionLocal = app_and_db
    _mint_key(SessionLocal, plan="developer")
    res = client.get(
        "/api/v1/admin/usage", headers={"Authorization": "Bearer test-admin-token"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["days"] == 7
    assert any(k["plan"] == "developer" for k in body["keys"])


def test_per_key_usage_attribution_is_readable_back(client, app_and_db):
    """Fixlist item 7 regression: `_record_usage`'s `api_key_usage` upsert
    and `_record_subnet`'s `api_key_usage_subnets` upsert used to bind
    `str(key_id)` (the DASHED UUID form) via raw `text()` SQL, while every
    ORM read of those tables (`GET /account/me`'s `usage_today`, the admin
    report's per-key `requests`, `distinct_subnets`) binds the HEX form
    SQLAlchemy's generic `Uuid` type uses under this SQLite harness -- so
    the two forms never joined and both reads silently returned 0/0,
    untestable. This is the positive assertion the fixlist's own
    verification step 4 says was missing (the only prior assertion on a
    per-key `requests` value was `== 0`, an exclusion)."""
    customer, row, full_key = _mint_key(SessionLocal=(app_and_db[1]), plan="developer")
    headers = {"Authorization": f"Bearer {full_key}", "X-Forwarded-For": "198.51.100.7"}
    for _ in range(3):
        res = client.get("/api/v1/_test_ok", headers=headers)
        assert res.status_code == 200

    # Admin report: per-key `requests` and `distinct_subnets` must reflect
    # the 3 requests just made, not 0.
    admin_res = client.get(
        "/api/v1/admin/usage", headers={"Authorization": "Bearer test-admin-token"}
    )
    assert admin_res.status_code == 200
    entry = next(k for k in admin_res.json()["keys"] if k["key_prefix"] == row.key_prefix)
    assert entry["requests"] == 3
    assert entry["distinct_subnets"] == 1

    # GET /account/me: `usage_today` must reflect the same 3 requests.
    client.cookies.set("bc_session", account_module._sign_session(customer.id))
    me_res = client.get("/api/v1/account/me")
    assert me_res.status_code == 200
    me_key = next(k for k in me_res.json()["keys"] if k["key_prefix"] == row.key_prefix)
    assert me_key["usage_today"]["requests"] == 3


def test_account_post_without_origin_is_403(client):
    res = client.post("/api/v1/account/keys", json={"name": "x"})
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "bad_origin"


def test_account_post_with_disallowed_origin_is_403(client):
    res = client.post(
        "/api/v1/account/keys",
        json={"name": "x"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "bad_origin"


def test_magic_link_always_202(client):
    res = client.post("/api/v1/account/magic-link", json={"email": "someone@example.com"})
    assert res.status_code == 202
    assert res.json()["accepted"] is True

    # Even a garbage email gets the same 202 -- never reveal validation
    # details either.
    res2 = client.post("/api/v1/account/magic-link", json={"email": "not-an-email"})
    assert res2.status_code == 202


def test_magic_link_out_of_bounds_length_email_still_202(client):
    """Fixlist item 15 regression: `MagicLinkRequest.email` used to carry
    `Field(min_length=3, max_length=320)`, so FastAPI itself rejected an
    empty or 321-char email with 422 BEFORE `request_magic_link` ever ran
    -- a response distinguishable by length from the route's documented
    uniform 202. Both boundary violations must now still 202."""
    too_short = client.post("/api/v1/account/magic-link", json={"email": "a"})
    assert too_short.status_code == 202

    too_long = client.post(
        "/api/v1/account/magic-link", json={"email": "a" * 400 + "@example.com"}
    )
    assert too_long.status_code == 202


def test_session_out_of_bounds_length_token_is_404_not_422(client):
    """Fixlist item 15 regression, the `SessionRequest.token` half: a
    too-short/too-long token used to 422 instead of this route's intended
    uniform 404 `invalid_token`."""
    too_short = client.post(
        "/api/v1/account/session", json={"token": "x"}, headers={"Origin": "https://billcommons.org"}
    )
    assert too_short.status_code == 404
    assert too_short.json()["error"]["code"] == "invalid_token"

    too_long = client.post(
        "/api/v1/account/session",
        json={"token": "x" * 500},
        headers={"Origin": "https://billcommons.org"},
    )
    assert too_long.status_code == 404
    assert too_long.json()["error"]["code"] == "invalid_token"


def _issue_and_hash_login_token(SessionLocal, email: str) -> str:
    """Mirrors `account._issue_login_token` without the email side-effect,
    for tests that need a live token without waiting on the mail thread."""
    import hashlib
    import secrets as secrets_mod
    from datetime import datetime, timedelta, timezone

    from billcommons_schema.models import AccountLoginToken

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
    return token


def test_session_first_login_mints_key_inline(app_and_db):
    """Amendments D5 + D7: first login for a brand-new customer (zero
    keys, no Stripe, no subscription) auto-mints a Developer key and
    reveals it INLINE in the POST /session response body."""
    app, SessionLocal = app_and_db
    token = _issue_and_hash_login_token(SessionLocal, "new-customer@example.com")

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/account/session",
            json={"token": token},
            headers={"Origin": "https://billcommons.org"},
        )
    assert res.status_code == 200
    assert res.json()["key"].startswith("bc_live_")
    assert "bc_session" in res.cookies


def test_session_missing_secret_fails_before_burning_the_token_or_minting_a_key(
    app_and_db, monkeypatch
):
    """Fixlist item 6 regression: a misconfigured instance
    (`ACCOUNT_SESSION_SECRET` unset) must fail BEFORE the magic-link token
    is claimed and BEFORE the customer's one auto-minted Developer key is
    created -- pre-fix, both writes committed and only THEN did
    `_sign_session` raise, permanently burning the token and occupying a
    key slot with a plaintext that could never be read again (no
    `reveal_ciphertext` on this inline-reveal path). Confirm: (a) the
    request 500s, (b) the token is STILL unused afterward (a retry with a
    correctly-configured instance must be able to consume it), and (c) no
    key was minted for the customer."""
    app, SessionLocal = app_and_db
    monkeypatch.delenv("ACCOUNT_SESSION_SECRET", raising=False)
    token = _issue_and_hash_login_token(SessionLocal, "misconfigured@example.com")

    with TestClient(app, raise_server_exceptions=False) as client:
        res = client.post(
            "/api/v1/account/session",
            json={"token": token},
            headers={"Origin": "https://billcommons.org"},
        )
    assert res.status_code == 500

    db = SessionLocal()
    from billcommons_schema.models import AccountLoginToken

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = db.execute(
        select(AccountLoginToken).where(AccountLoginToken.token_hash == token_hash)
    ).scalar_one()
    assert row.used_at is None  # NOT burned -- a retry with the secret set must still work

    customer = db.execute(
        select(ApiCustomer).where(ApiCustomer.email == "misconfigured@example.com")
    ).scalar_one_or_none()
    assert customer is None  # no customer/key was ever created either
    db.close()


def test_session_second_login_does_not_remint(app_and_db):
    """D5: a customer who already has a key gets 204, never a second key."""
    app, SessionLocal = app_and_db
    email = "returning-customer@example.com"
    token1 = _issue_and_hash_login_token(SessionLocal, email)

    with TestClient(app) as client:
        first = client.post(
            "/api/v1/account/session",
            json={"token": token1},
            headers={"Origin": "https://billcommons.org"},
        )
        assert first.status_code == 200

        token2 = _issue_and_hash_login_token(SessionLocal, email)
        second = client.post(
            "/api/v1/account/session",
            json={"token": token2},
            headers={"Origin": "https://billcommons.org"},
        )
    assert second.status_code == 204
    assert second.content == b""


def test_session_invalid_token_404(app_and_db):
    app, _ = app_and_db
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/account/session",
            json={"token": "not-a-real-token-not-a-real-token"},
            headers={"Origin": "https://billcommons.org"},
        )
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "invalid_token"


# ---------------------------------------------------------------------------
# 2026-08-21 fix-pass regressions
# ---------------------------------------------------------------------------


def _login_client(app, customer_id) -> TestClient:
    """Logs a TestClient in as `customer_id` directly via a hand-signed
    session cookie, bypassing the magic-link round trip -- ACCOUNT_SESSION_SECRET
    is already set by the `app_and_db` fixture."""
    client = TestClient(app)
    client.cookies.set("bc_session", account_module._sign_session(customer_id))
    return client


def test_unknown_key_401_still_charges_the_anonymous_ip_bucket(client, app_and_db, monkeypatch):
    """Item 1 regression (the `quota.py` half -- `api_keys.py` holds the
    cache-bounding half): probing an unauthenticated, unknown/garbage
    `bc_live_`-shaped bearer must consume the SAME anonymous IP/subnet
    budget a keyless caller would, so unlimited probing eventually gets
    429'd instead of an endless stream of free 401s."""
    monkeypatch.setenv("BILLCOMMONS_ANON_DAILY_LIMIT", "1000")
    headers = {
        "Authorization": "Bearer bc_live_" + "z" * 32,
        "X-Forwarded-For": "203.0.113.60",
    }
    # BILLCOMMONS_API_RATE_LIMIT_DEFAULT is "3/minute" in this fixture --
    # the 4th probe from the same IP in the same minute must be 429, not
    # another free 401, proving the anonymous default-ip tier was charged
    # by every prior 401.
    statuses = [client.get("/api/v1/_test_ok", headers=headers).status_code for _ in range(4)]
    assert statuses[:3] == [401, 401, 401]
    assert statuses[3] == 429


def test_probe_still_resolves_before_anonymous_saturation_is_applied(client, app_and_db, monkeypatch):
    """R4-1 deliberately accepts one indexed lookup for every probe: only
    a failed resolution may consult the anonymous saturation guard, because
    resolving first prevents a valid key behind that IP from being 429'd."""
    monkeypatch.setenv("BILLCOMMONS_ANON_DAILY_LIMIT", "1000")
    calls = []
    original_resolve_key = quota_module.resolve_key

    def _counting_resolve_key(presented):
        calls.append(presented)
        return original_resolve_key(presented)

    monkeypatch.setattr(quota_module, "resolve_key", _counting_resolve_key)

    headers = {
        "Authorization": "Bearer bc_live_" + "z" * 32,
        "X-Forwarded-For": "203.0.113.61",
    }
    # BILLCOMMONS_API_RATE_LIMIT_DEFAULT is "3/minute".  All failed keys
    # resolve first; the fourth and later are then refused by the anon peek.
    statuses = [client.get("/api/v1/_test_ok", headers=headers).status_code for _ in range(6)]
    assert statuses[:3] == [401, 401, 401]
    assert all(s == 429 for s in statuses[3:])
    assert len(calls) == 6


def test_post_response_metering_failure_does_not_500_the_response(client, app_and_db, monkeypatch):
    """Item 2 regression: a DB failure in the post-response accounting block
    must be swallowed (logged, not raised) -- the caller still gets the
    200 the handler already computed, and is simply not metered for that
    one request."""
    _, SessionLocal = app_and_db
    _, _, full_key = _mint_key(SessionLocal, plan="developer")

    def _boom(self, db, customer_id, key_id, heavy):
        raise RuntimeError("simulated DB failure")

    import billcommons_api.quota as quota_module

    monkeypatch.setattr(quota_module.QuotaMiddleware, "_record_usage", _boom)

    res = client.get("/api/v1/_test_ok", headers={"Authorization": f"Bearer {full_key}"})
    assert res.status_code == 200  # NOT 500, even though metering blew up


def test_pre_check_quota_read_failure_fails_closed_503(client, app_and_db, monkeypatch):
    """Fixlist item 13 regression: unlike the post-response accounting
    block (item 2, tested above), the PRE-check `SELECT` had a bare
    `try/finally` with no `except` -- a transient DB failure there raised
    out of `QuotaMiddleware.dispatch`, past Starlette's
    `ExceptionMiddleware` (where this API's own error-envelope handlers
    live), and surfaced as a bare un-enveloped 500. It must instead fail
    CLOSED as a clean, enveloped `503 quota_unavailable`."""
    _, SessionLocal = app_and_db
    _, _, full_key = _mint_key(SessionLocal, plan="developer")

    def _boom(self, customer_id):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(quota_module.QuotaMiddleware, "_read_usage_threadpool", _boom)

    res = client.get("/api/v1/_test_ok", headers={"Authorization": f"Bearer {full_key}"})
    assert res.status_code == 503
    body = res.json()
    assert body["error"]["code"] == "quota_unavailable"


def test_resolve_key_unexpected_exception_fails_closed_503(client, app_and_db, monkeypatch):
    """R6: a malformed/crafted presented key must not turn an unexpected
    resolver exception into Starlette's bare 500 response."""
    import billcommons_api.quota as quota_module

    monkeypatch.setattr(
        quota_module,
        "resolve_key",
        lambda presented: (_ for _ in ()).throw(RuntimeError("crafted key parser failure")),
    )
    res = client.get("/api/v1/_test_ok", headers={"Authorization": "Bearer bc_live_" + "x" * 32})
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "quota_unavailable"


def test_anon_daily_tier_429_reports_its_own_limit_not_per_minute():
    """Fixlist item 14 regression: a refusal from the `anon-daily-ip` tier
    used to always render `X-RateLimit-Limit` from `self._default.ip`
    (the PER-MINUTE ceiling) regardless of which tier actually failed --
    an internally incoherent pair (a per-minute limit advertised alongside
    a ~21-hour `Retry-After`). The failing tier's OWN limit must be
    reported."""
    import time

    from billcommons_api.quota import QuotaMiddleware
    from billcommons_api.rate_limit import quota_bucket

    async def _dummy_app(scope, receive, send):  # pragma: no cover - never called
        raise AssertionError("call_next should never run for this test")

    mw = QuotaMiddleware(
        _dummy_app,
        limit=1000,  # per-minute tier never trips in this test
        subnet_limit=1000,
        heavy_limit=1000,
        heavy_subnet_limit=1000,
        window=60.0,
        clock=time.monotonic,
        anon_daily_limit=2,  # daily-ip tier trips first, on the 3rd call
        anon_daily_subnet_limit=1000,
    )

    class _FakeRequest:
        def __init__(self):
            self.headers = {"x-forwarded-for": "203.0.113.63"}
            self.url = type("U", (), {"path": "/api/v1/_test_ok"})()
            self.client = type("C", (), {"host": "203.0.113.63"})()

    request = _FakeRequest()
    quota_bucket("203.0.113.63")  # match the fixture's own usage above; unused otherwise

    assert mw._anon_tier_check(request)[0] is None
    assert mw._anon_tier_check(request)[0] is None
    failed, _ = mw._anon_tier_check(request)
    assert failed is not None
    assert failed.status_code == 429
    assert failed.headers["X-RateLimit-Limit"] == "2"  # the daily-ip limit, not the 1000/minute one


def test_session_reused_token_never_succeeds_twice(app_and_db):
    """Item 3 (part 1) regression, sequential: a token already consumed by
    one `POST /session` call must 404 `invalid_token` on every subsequent
    call with the same token -- the atomic `UPDATE ... WHERE used_at IS
    NULL ... RETURNING` claim makes this hold even though it's the same
    check-then-set-shaped code path as before; the difference the fix
    makes is under concurrency (see the threaded test below), not here."""
    app, SessionLocal = app_and_db
    token = _issue_and_hash_login_token(SessionLocal, "reuse@example.com")
    with TestClient(app) as c:
        first = c.post(
            "/api/v1/account/session",
            json={"token": token},
            headers={"Origin": "https://billcommons.org"},
        )
        assert first.status_code == 200
        second = c.post(
            "/api/v1/account/session",
            json={"token": token},
            headers={"Origin": "https://billcommons.org"},
        )
    assert second.status_code == 404
    assert second.json()["error"]["code"] == "invalid_token"


# Item 3's true concurrent-race guarantee (two simultaneous
# `POST /account/session` calls with the SAME token) is POSTGRES-ONLY to
# test meaningfully: this harness's SQLite engine hands every session the
# SAME raw DBAPI connection (`StaticPool`, `check_same_thread=False`), so
# two threads issuing statements against it concurrently is not a real
# concurrent-transaction environment -- it reliably either interleaves in
# ways that don't reflect Postgres MVCC, or deadlocks the single shared
# connection outright (observed while writing this test). The atomic
# `UPDATE ... RETURNING` claim, the customer-row `FOR UPDATE` lock in
# `mint_developer_key_if_first_login`, and `rotate_key`/`revoke_key`'s
# locks are all exercised for real only against the staging Postgres pass
# (`scripts/monetize_smoke.py` + `alembic upgrade/downgrade/upgrade`, see
# the fix-pass report). What SQLite CAN verify is the single-use
# invariant that must hold regardless of concurrency -- that's
# `test_session_reused_token_never_succeeds_twice` above.


def test_reveal_decrypt_failure_preserves_ciphertext_and_returns_503(app_and_db, monkeypatch):
    """Item 4 regression: a Fernet decrypt failure must NOT null the
    reveal columns -- pre-fix, the columns were nulled and committed BEFORE
    checking whether decryption succeeded, so a rotated/misconfigured
    `BILLCOMMONS_REVEAL_KEY` destroyed the only copy of a live credential
    and then 404'd, leaving the key active and unreadable forever."""
    app, SessionLocal = app_and_db
    db = SessionLocal()
    customer = ApiCustomer(email="reveal-fail@example.com")
    db.add(customer)
    db.flush()
    db.commit()
    row, full_key = api_keys.mint_key(db, customer.id)
    row.reveal_ciphertext = api_keys.encrypt_for_reveal(full_key)
    row.reveal_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    db.commit()
    customer_id = customer.id
    key_id = row.id
    db.close()

    monkeypatch.setattr(account_module, "decrypt_reveal", lambda ciphertext: None)

    client = _login_client(app, customer_id)
    res = client.post(f"/api/v1/account/keys/{key_id}/reveal", headers={"Origin": "https://billcommons.org"})
    assert res.status_code == 503

    db2 = SessionLocal()
    fresh = db2.execute(select(ApiKey).where(ApiKey.id == key_id)).scalar_one()
    assert fresh.reveal_ciphertext is not None  # NEVER destroyed on decrypt failure
    assert fresh.status == "active"
    db2.close()


def test_reveal_expired_window_auto_revokes_key(app_and_db):
    """Item 5 regression: an expired-but-unrevealed reveal window must
    actually REVOKE the key (mirroring `expire_stale_reveal`'s C7 logic),
    not just null the reveal columns while leaving `status='active'` --
    otherwise the key occupies an active slot forever with no one who can
    ever read it, and C7's own lazy auto-revoke can never fire again once
    `reveal_ciphertext` is already None."""
    app, SessionLocal = app_and_db
    db = SessionLocal()
    customer = ApiCustomer(email="reveal-expired@example.com")
    db.add(customer)
    db.flush()
    db.commit()
    row, full_key = api_keys.mint_key(db, customer.id)
    row.reveal_ciphertext = api_keys.encrypt_for_reveal(full_key)
    row.reveal_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    customer_id = customer.id
    key_id = row.id
    db.close()

    client = _login_client(app, customer_id)
    res = client.post(f"/api/v1/account/keys/{key_id}/reveal", headers={"Origin": "https://billcommons.org"})
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "reveal_expired"

    db2 = SessionLocal()
    fresh = db2.execute(select(ApiKey).where(ApiKey.id == key_id)).scalar_one()
    assert fresh.status == "revoked"  # not merely nulled -- actually revoked
    assert fresh.reveal_ciphertext is None
    db2.close()


def test_reveal_is_consumed_exactly_once(app_and_db):
    """Fixlist item 9 regression: the conditional-claim half. A second
    reveal call for the same key -- after the first has already consumed
    it -- must 404 `nothing_to_reveal`, never a second 200 with the
    plaintext. Sequential here (SQLite has no real row locks to race), but
    exercises the same conditional
    `UPDATE ... WHERE reveal_ciphertext IS NOT NULL RETURNING id` claim
    that also closes the genuinely concurrent race under Postgres (per
    the fixlist's own verification-steps section)."""
    app, SessionLocal = app_and_db
    db = SessionLocal()
    customer = ApiCustomer(email="reveal-once@example.com")
    db.add(customer)
    db.flush()
    db.commit()
    row, full_key = api_keys.mint_key(db, customer.id)
    row.reveal_ciphertext = api_keys.encrypt_for_reveal(full_key)
    row.reveal_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    db.commit()
    customer_id = customer.id
    key_id = row.id
    db.close()

    client = _login_client(app, customer_id)
    first = client.post(f"/api/v1/account/keys/{key_id}/reveal", headers={"Origin": "https://billcommons.org"})
    assert first.status_code == 200
    assert first.json()["key"] == full_key

    second = client.post(f"/api/v1/account/keys/{key_id}/reveal", headers={"Origin": "https://billcommons.org"})
    assert second.status_code == 404
    assert second.json()["error"]["code"] == "nothing_to_reveal"


def test_create_key_invalid_environment_is_422_not_500(app_and_db):
    """Item 7 regression: `environment` is now a `Literal["live", "test"]`,
    so Pydantic itself rejects a bad value with 422 -- pre-fix,
    `mint_key_material` raised a bare `ValueError` that `create_key` never
    caught, 500ing with a stack trace."""
    app, SessionLocal = app_and_db
    db = SessionLocal()
    customer = ApiCustomer(email="badenv@example.com")
    db.add(customer)
    db.flush()
    db.commit()
    customer_id = customer.id
    db.close()

    client = _login_client(app, customer_id)
    res = client.post(
        "/api/v1/account/keys",
        json={"name": "x", "environment": "prod"},
        headers={"Origin": "https://billcommons.org"},
    )
    assert res.status_code == 422


def test_create_key_ceiling_is_409_not_400(app_and_db):
    """Fixlist item 11 regression: `create_key`'s router mapped
    `KeyLimitExceeded` to `bad_request` -> 400. This is a conflict
    (existing-state precondition failure), matching `api_keys.py`'s own
    docstring ("refused (409, raised as KeyLimitExceeded here)") and
    `rotate_key_endpoint`'s equivalent path."""
    app, SessionLocal = app_and_db
    db = SessionLocal()
    customer = ApiCustomer(email="ceiling@example.com")
    db.add(customer)
    db.flush()
    db.commit()
    api_keys.mint_key(db, customer.id, name="a")
    db.commit()
    api_keys.mint_key(db, customer.id, name="b")
    db.commit()
    customer_id = customer.id
    db.close()

    client = _login_client(app, customer_id)
    res = client.post(
        "/api/v1/account/keys",
        json={"name": "c", "environment": "live"},
        headers={"Origin": "https://billcommons.org"},
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "key_limit_exceeded"


def test_rotate_key_ceiling_is_409_not_400(app_and_db):
    """Fixlist item 11 regression, the rotation half: `SPEC-LOCKED.md`
    B3 ("Rotation refused (409) if it would exceed the ceiling") and A9
    ("At most one rotating key per customer at a time (409 otherwise)")
    both say 409 -- only the router disagreed with both."""
    app, SessionLocal = app_and_db
    db = SessionLocal()
    customer = ApiCustomer(email="rotate-conflict@example.com")
    db.add(customer)
    db.flush()
    db.commit()
    row, _ = api_keys.mint_key(db, customer.id)
    db.commit()
    api_keys.rotate_key(db, row.id, customer.id)  # now `rotating` -- a second rotation must conflict
    db.commit()
    second_row, _ = api_keys.mint_key(db, customer.id, name="second")
    db.commit()
    customer_id = customer.id
    second_id = second_row.id
    db.close()

    client = _login_client(app, customer_id)
    res = client.post(
        f"/api/v1/account/keys/{second_id}/rotate", headers={"Origin": "https://billcommons.org"}
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "rotation_refused"


def test_create_key_inherits_customer_subscription_plan(app_and_db):
    """Item 8 regression: minting an extra key from `/account` must derive
    the plan from the customer's current subscription -- pre-fix it was
    hardcoded to `"developer"`, metering a paying customer's second key at
    free-tier limits (a straight revenue leak)."""
    app, SessionLocal = app_and_db
    db = SessionLocal()
    customer = ApiCustomer(email="paid-customer@example.com")
    db.add(customer)
    db.flush()
    db.commit()
    db.add(ApiSubscription(customer_id=customer.id, plan="builder", status="active"))
    db.commit()
    customer_id = customer.id
    db.close()

    client = _login_client(app, customer_id)
    res = client.post(
        "/api/v1/account/keys",
        json={"name": "second"},
        headers={"Origin": "https://billcommons.org"},
    )
    assert res.status_code == 201

    db2 = SessionLocal()
    key = db2.execute(select(ApiKey).where(ApiKey.customer_id == customer_id)).scalars().first()
    assert key.plan == "builder"
    db2.close()


def test_r4_2_incomplete_expired_subscription_is_not_plan_authoritative(app_and_db):
    """`/me` and a newly minted account key must both fall back to Developer
    when the only Scale row is an abandoned checkout."""
    app, SessionLocal = app_and_db
    db = SessionLocal()
    customer = ApiCustomer(email="r4-incomplete-expired@example.com")
    db.add(customer)
    db.flush()
    db.add(
        ApiSubscription(
            customer_id=customer.id,
            stripe_subscription_id="sub_r4_incomplete_expired",
            plan="scale",
            status="incomplete_expired",
        )
    )
    db.commit()
    customer_id = customer.id
    db.close()

    client = _login_client(app, customer_id)
    me = client.get("/api/v1/account/me")
    assert me.status_code == 200
    assert me.json()["plan"] == "developer"
    minted = client.post(
        "/api/v1/account/keys",
        json={"name": "r4 developer key"},
        headers={"Origin": "https://billcommons.org"},
    )
    assert minted.status_code == 201
    db = SessionLocal()
    key = db.execute(select(ApiKey).where(ApiKey.customer_id == customer_id)).scalar_one()
    db.close()
    assert key.plan == "developer"


def test_magic_link_short_circuits_email_limiter_when_ip_already_blocked(app_and_db):
    """Item 10 regression: once the IP limiter has already refused, the
    email limiter must never even be consulted -- pre-fix, it was called
    unconditionally, letting one blocked IP create attacker-chosen bucket
    keys (arbitrary email strings) in the email limiter's dict at full
    request rate."""
    app, _ = app_and_db
    ip = "203.0.113.90"
    for _ in range(30):
        account_module._ip_limiter.allow(ip)  # exhaust the 20/hour IP limit directly

    with TestClient(app) as c:
        res = c.post(
            "/api/v1/account/magic-link",
            json={"email": "short-circuit-target@example.com"},
            headers={"X-Forwarded-For": ip},
        )
    assert res.status_code == 202  # always 202, IP-blocked or not
    assert "short-circuit-target@example.com" not in account_module._email_limiter._buckets


def test_admin_check_token_non_ascii_never_raises(monkeypatch):
    """Item 11 regression (admin half): a non-ASCII bearer must be
    evaluated (and rejected) without raising -- pre-fix,
    `hmac.compare_digest(str, str)` itself raises `TypeError` for a
    non-ASCII-compatible argument, which would 500 instead of this
    module's own deliberate 404. (Exercised as a direct unit test, not
    through `TestClient`/httpx, because httpx's own header encoding
    refuses a literal non-ASCII `str` header value client-side before a
    request is even sent -- this isolates the server-side `hmac` bug from
    that client-library restriction.)
    """
    from billcommons_api.routers.admin import _check_admin_token

    monkeypatch.setenv("BILLCOMMONS_ADMIN_TOKEN", "expected-token")

    class _FakeRequest:
        headers = {"authorization": "Bearer café"}

    assert _check_admin_token(_FakeRequest()) is False  # rejected, never raises


def test_account_me_non_ascii_session_signature_is_401_not_500(app_and_db):
    """Item 11 regression (account half): a crafted `bc_session` cookie
    whose decoded signature contains a non-ASCII-compatible character must
    401, not 500 -- pre-fix, `hmac.compare_digest` sat OUTSIDE the only
    try/except in `_verify_session`."""
    app, _ = app_and_db
    payload = "00000000-0000-0000-0000-000000000000.9999999999"
    bad_sig = "café-not-a-real-signature"
    cookie_value = base64.urlsafe_b64encode(f"{payload}.{bad_sig}".encode()).decode()
    with TestClient(app) as c:
        c.cookies.set("bc_session", cookie_value)
        res = c.get("/api/v1/account/me")
    assert res.status_code == 401


def test_issue_login_token_propagates_commit_failure():
    """E8/item 2 (round-2 fix pass, supersedes round-1 item 12):
    `_issue_login_token` must NOT swallow a DB failure itself anymore --
    it is called from inside the Stripe webhook's single all-or-nothing
    transaction (`billing._send_magic_link`, `commit=False`), where a
    swallowed failure used to let the whole delivery silently discard
    everything written earlier in the same transaction under a `200
    processed`. The failure must propagate; `request_magic_link` (this
    router's OWN route, `commit=True`) is responsible for catching it to
    keep ITS "always 202" contract -- see the next test."""

    class _BoomSession:
        def add(self, obj):
            pass

        def flush(self):
            pass

        def commit(self):
            raise RuntimeError("simulated DB failure")

        def rollback(self):
            pass

    with pytest.raises(RuntimeError):
        account_module._issue_login_token(_BoomSession(), "boom@example.com", "203.0.113.1", commit=True)


def test_magic_link_still_202_when_token_issuance_fails(app_and_db, monkeypatch):
    """`request_magic_link` wraps its own call to `_issue_login_token` in
    its own try/except (item 10) -- a failure there still degrades to the
    route's documented always-202, never-reveal-DB-liveness contract, even
    though the helper itself no longer swallows anything."""
    app, _ = app_and_db

    def _boom(db, email, ip, *, commit=True):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(account_module, "_issue_login_token", _boom)
    with TestClient(app) as c:
        res = c.post("/api/v1/account/magic-link", json={"email": "db-down@example.com"})
    assert res.status_code == 202
    assert res.json()["accepted"] is True


def test_admin_usage_date_window_excludes_the_days_plus_one_row(client, app_and_db):
    """Item 13 regression: `since = today - timedelta(days=days-1)` makes
    the window exactly `days` calendar days inclusive of today. Pre-fix
    (`days` with no `-1`), a usage row exactly `days` days before today was
    WRONGLY included, inflating `pct_of_quota` by up to ~14% at the
    default `days=7`."""
    _, SessionLocal = app_and_db
    _, row, _ = _mint_key(SessionLocal, plan="developer")
    db = SessionLocal()
    from billcommons_schema.models import ApiKeyUsage

    today = date.today()
    db.add(
        ApiKeyUsage(
            key_id=row.id,
            usage_date=today - timedelta(days=7),  # exactly `days` (default 7) ago
            requests=1000,
            heavy_requests=0,
        )
    )
    db.commit()
    db.close()

    res = client.get("/api/v1/admin/usage", headers={"Authorization": "Bearer test-admin-token"})
    body = res.json()
    entry = next(k for k in body["keys"] if k["key_prefix"] == row.key_prefix)
    assert entry["requests"] == 0  # the days=7-ago row must be excluded now


def test_admin_key_sharing_flag_is_per_day_not_window_total(client, app_and_db):
    """Fixlist item 12 regression: `SPEC-LOCKED.md` R13 defines the
    key-sharing signal as ">20 distinct /24 per key PER DAY". A key seen
    from 4 disjoint /24s each day for 7 days accumulates 28 DISTINCT
    subnets across the whole window (window-total `count(distinct
    subnet)` > 20) but never exceeds 4 on any SINGLE day -- pre-fix, that
    was a FALSE `key_sharing_flag`. The flag must be based on the worst
    SINGLE day, not the window total."""
    _, SessionLocal = app_and_db
    _, row, _ = _mint_key(SessionLocal, plan="developer")
    db = SessionLocal()
    from billcommons_schema.models import ApiKeyUsageSubnet

    # The admin query anchors its calendar window to UTC; use the same clock
    # in the fixture so the test remains deterministic around local midnight.
    today = datetime.now(timezone.utc).date()
    for day_offset in range(7):  # 7 days, 4 distinct subnets EACH day
        for subnet_n in range(4):
            db.add(
                ApiKeyUsageSubnet(
                    key_id=row.id,
                    usage_date=today - timedelta(days=day_offset),
                    subnet=f"203.0.{day_offset}.{subnet_n}/24",
                    requests=1,
                )
            )
    db.commit()
    db.close()

    res = client.get("/api/v1/admin/usage", headers={"Authorization": "Bearer test-admin-token"})
    entry = next(k for k in res.json()["keys"] if k["key_prefix"] == row.key_prefix)
    assert entry["distinct_subnets"] == 28  # window total, reported but NOT the flag basis
    assert entry["max_daily_distinct_subnets"] == 4  # the worst single day
    assert entry["key_sharing_flag"] is False  # 4 <= 20 on every day -- must NOT be flagged


def test_anon_tier_check_stops_at_first_failed_tier(monkeypatch):
    """Item 14 regression: once one tier fails, later tiers in the same
    call must NOT be charged at all -- pre-fix, every `.allow()` call was
    made unconditionally (building the whole `results` list eagerly), so a
    client already 429'd by the per-minute tier kept burning its daily
    IP/subnet budgets on every subsequent request in the same minute."""
    import time

    from billcommons_api.quota import QuotaMiddleware
    from billcommons_api.rate_limit import quota_bucket

    async def _dummy_app(scope, receive, send):  # pragma: no cover - never called
        raise AssertionError("call_next should never run for this test")

    mw = QuotaMiddleware(
        _dummy_app,
        limit=1,
        subnet_limit=1000,
        heavy_limit=1000,
        heavy_subnet_limit=1000,
        window=60.0,
        clock=time.monotonic,
        anon_daily_limit=1000,
        anon_daily_subnet_limit=1000,
    )

    class _FakeRequest:
        def __init__(self):
            self.headers = {"x-forwarded-for": "203.0.113.70"}
            self.url = type("U", (), {"path": "/api/v1/_test_ok"})()
            self.client = type("C", (), {"host": "203.0.113.70"})()

    request = _FakeRequest()
    ip_key = quota_bucket("203.0.113.70")

    # First call: passes (limit=1, first request in the window) -- ALL
    # tiers, including anon-daily-ip, are legitimately consulted once for
    # any request that doesn't fail anywhere, so its bucket count is 1
    # after this.
    failed, _ = mw._anon_tier_check(request)
    assert failed is None
    count_after_first = mw._anon_daily_ip._buckets[ip_key][1]
    assert count_after_first == 1

    # Second call: the default-ip tier (limit=1) now fails -- this must
    # short-circuit BEFORE the anon-daily-ip tier is ever touched again, so
    # its count stays exactly what it was, not incremented a second time.
    failed, _ = mw._anon_tier_check(request)
    assert failed is not None
    count_after_second = mw._anon_daily_ip._buckets[ip_key][1]
    assert count_after_second == count_after_first


def test_account_cors_vary_header_appends_not_overwrites():
    """Item 15 regression: `AccountCorsMiddleware` must APPEND to an
    existing `Vary` header (e.g. `Accept-Encoding`, set by the innermost
    `GZipMiddleware`), never overwrite it -- pre-fix, plain assignment
    destroyed `Vary: Accept-Encoding` on every `/account`/`/billing`
    response."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from billcommons_api.middleware import AccountCorsMiddleware

    async def endpoint(request):
        return PlainTextResponse("ok", headers={"Vary": "Accept-Encoding"})

    app = Starlette(routes=[Route("/api/v1/account/_probe", endpoint)])
    app.add_middleware(AccountCorsMiddleware)

    with TestClient(app) as c:
        res = c.get("/api/v1/account/_probe", headers={"Origin": "https://billcommons.org"})

    vary = res.headers.get("vary", "")
    assert "Accept-Encoding" in vary
    assert "Origin" in vary


def test_successful_keyed_request_writes_last_used_at(client, app_and_db):
    """Item 16 regression: `api_keys.last_used_at` is surfaced by `GET /me`
    and the account page but was written by nothing anywhere in the tree --
    it read `null` forever. A successful keyed request must now set it."""
    _, SessionLocal = app_and_db
    _, row, full_key = _mint_key(SessionLocal, plan="developer")
    res = client.get("/api/v1/_test_ok", headers={"Authorization": f"Bearer {full_key}"})
    assert res.status_code == 200

    db = SessionLocal()
    fresh = db.execute(select(ApiKey).where(ApiKey.id == row.id)).scalar_one()
    assert fresh.last_used_at is not None
    db.close()


def test_keyed_dispatch_runs_db_work_in_threadpool_not_the_loop(client, app_and_db, monkeypatch):
    """Fixlist item 3 regression: `QuotaMiddleware.dispatch` is `async`
    (`BaseHTTPMiddleware` runs it on the event-loop thread), but the pre-
    check SELECT, `resolve_key`'s cache-miss DB round trip, and the
    post-response upserts+commit are all synchronous SQLAlchemy/psycopg
    I/O -- every one of them must go through `run_in_threadpool`, not run
    inline on the loop thread. Spies on `quota_module.run_in_threadpool`
    and confirms it is actually invoked (with the expected callables) for
    a single successful keyed request."""
    _, SessionLocal = app_and_db
    _, _, full_key = _mint_key(SessionLocal, plan="developer")

    calls = []
    original_run_in_threadpool = quota_module.run_in_threadpool

    async def _spy(func, *args, **kwargs):
        calls.append(func)
        return await original_run_in_threadpool(func, *args, **kwargs)

    monkeypatch.setattr(quota_module, "run_in_threadpool", _spy)

    res = client.get("/api/v1/_test_ok", headers={"Authorization": f"Bearer {full_key}"})
    assert res.status_code == 200

    called_names = {getattr(f, "__name__", None) or getattr(f, "__qualname__", str(f)) for f in calls}
    # `resolve_key` (api_keys module-level function), the pre-check read,
    # and the post-response accounting block must all have gone through
    # run_in_threadpool for this one request.
    assert "resolve_key" in called_names
    assert any("_read_usage_threadpool" in n for n in called_names)
    assert any("_record_usage_and_subnet_threadpool" in n for n in called_names)


def test_create_app_logs_error_when_monetization_secrets_missing(monkeypatch, caplog):
    """Item 20 regression: `ACCOUNT_SESSION_SECRET`/`BILLCOMMONS_REVEAL_KEY`
    are validated at `create_app()` startup now -- a missing var must log
    an ERROR immediately rather than surfacing only as a runtime 500 on the
    first login/mint/reveal request."""
    monkeypatch.delenv("ACCOUNT_SESSION_SECRET", raising=False)
    monkeypatch.delenv("BILLCOMMONS_REVEAL_KEY", raising=False)

    from billcommons_api.app import create_app

    with caplog.at_level(logging.ERROR, logger="billcommons_api.app"):
        create_app()

    messages = " ".join(r.message for r in caplog.records)
    assert "ACCOUNT_SESSION_SECRET" in messages
    assert "BILLCOMMONS_REVEAL_KEY" in messages
