"""Shared throwaway-DB harness for the Phase 1 monetization tests
(`test_api_keys.py`, `test_quota.py`).

`conftest.py`'s own `app`/`client` fixtures point at the LIVE Railway
Postgres DB (see its docstring) -- correct for every other test file, but
migration 0019's tables do not exist there until the operator applies it.
Rather than block this suite on that migration landing, these two test
files build their OWN app.

By default that's an in-memory SQLite engine (shared across threads via
`StaticPool`, since `TestClient` runs sync endpoints in AnyIO's
threadpool) with ONLY the monetization tables created -- the rest of this
repo's schema uses `JSONB`/other Postgres-only types that SQLite's
dialect cannot compile, so `Base.metadata.create_all` is called with an
explicit `tables=[...]` list, never the whole registry.

2026-08-21 fix-pass addition: set `BILLCOMMONS_TEST_DATABASE_URL` to run
this SAME suite against a real Postgres instance instead (e.g. the
throwaway staging DB migration 0019 was applied to) -- this is where the
`FOR UPDATE` locks (`mint_key`/`rotate_key`/`revoke_key`/
`mint_developer_key_if_first_login`), the `ON CONFLICT` upserts, and the
`uuid = text` binding SQLite can't emulate get their first REAL exercise.
Migrations must already be applied to that database (`alembic upgrade
head` from `packages/schema`); this harness only DELETEs rows from the
monetization tables (reverse-FK order, no CASCADE) before each test for a
clean slate, it never creates schema against Postgres.

2026-08-21 fix-pass, round 2 (fixlist item 1): the harness REFUSES to run
against anything that isn't provably disposable. Two gates, both
required, checked as soon as `BILLCOMMONS_TEST_DATABASE_URL` is read (at
import time, and again defensively inside `_make_postgres_engine`):
(a) the URL's host must be `127.0.0.1`/`localhost`, OR the URL text must
contain `_staging`/`_test`; (b) `BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE=1`
must be set explicitly. Either gate failing raises `RuntimeError` loudly
rather than silently deleting rows. This exists because the original
`TRUNCATE ... CASCADE` form, run against a miswired `DATABASE_URL`
(copy-paste from the operator's shell into `BILLCOMMONS_TEST_DATABASE_URL`),
would empty every table with an FK to a monetization table -- including
`webhook_subscriptions` (migration 0019 gave it one) -- a live production
table this suite has nothing to do with. See
`docs/operations/monetization-runbook.md` for the full writeup and the
env var reference.

`billcommons_api.api_keys._session_factory` and
`billcommons_api.quota._session_factory` are looked up via module attribute
specifically so this can monkeypatch them (see each module's own comment).
`billcommons_api.routers.admin.get_session` is a plain imported name used at
call time, so it monkeypatches the same way. FastAPI's own
`app.dependency_overrides[get_db]` handles every router that uses
`Depends(get_db)` (`account.py`).
"""
from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit

from sqlalchemy import create_engine, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, StaticPool


@compiles(JSONB, "sqlite")
def _compile_jsonb_as_text_on_sqlite(element, compiler, **kw):
    """`stripe_events.payload` is Postgres `JSONB`; SQLite's dialect has no
    JSONB type and can't render `CREATE TABLE` DDL for it at all (unlike
    plain `JSON`, which IS SQLite-compilable and already worked in this
    harness for other tables). Phase 2 is the first monetization table
    with a JSONB column added to this SQLite-only test harness, so this
    compiler hook -- not needed until now -- teaches the sqlite dialect to
    render the column as `TEXT`. `billcommons_api.routers.billing` never
    round-trips `payload` through the ORM (it's written via a raw `INSERT
    ... ON CONFLICT DO NOTHING RETURNING` and never read back in this
    process), so no Python-side (de)serialization behavior depends on this
    -- it only has to exist as A column for the idempotency INSERT to
    target.
    """
    return "TEXT"

import billcommons_api.api_keys as api_keys_module
import billcommons_api.quota as quota_module
import billcommons_api.routers.admin as admin_module
from billcommons_api.deps import get_db
from billcommons_schema.base import Base
from billcommons_schema.models import (
    AccountLoginToken,
    ApiCustomer,
    ApiCustomerUsage,
    ApiKey,
    ApiKeyUsage,
    ApiKeyUsageSubnet,
    ApiSubscription,
    SnapshotEntitlement,
    StripeEvent,
)

_MONETIZATION_MODELS = (
    ApiCustomer,
    ApiKey,
    ApiSubscription,
    ApiCustomerUsage,
    ApiKeyUsage,
    ApiKeyUsageSubnet,
    AccountLoginToken,
    StripeEvent,
    SnapshotEntitlement,
)

# Reverse-FK order for a plain `DELETE FROM` clear (children before
# parents) -- deliberately NOT `TRUNCATE ... CASCADE` (fixlist item 1):
# CASCADE reaches outside this table list to anything else with an FK to
# one of them (e.g. `webhook_subscriptions.customer_id` -> `api_customers`,
# added by migration 0019), which is exactly how a miswired
# `BILLCOMMONS_TEST_DATABASE_URL` becomes production data loss. Deleting
# in this order satisfies the FK constraints without CASCADE.
_DELETE_ORDER = (
    ApiKeyUsage,
    ApiKeyUsageSubnet,
    ApiCustomerUsage,
    SnapshotEntitlement,
    ApiSubscription,
    AccountLoginToken,
    StripeEvent,
    ApiKey,
    ApiCustomer,
)
assert set(_DELETE_ORDER) == set(_MONETIZATION_MODELS)


def _assert_destructive_test_db_allowed(pg_url: str) -> None:
    """Refuse to run destructive statements (DELETE, formerly TRUNCATE
    CASCADE) against `pg_url` unless it is provably disposable. Both
    gates are required (fixlist item 1):

    (a) the URL's host is `127.0.0.1`/`localhost`, OR the URL text
        contains `_staging`/`_test` (covers e.g. a Railway/RDS throwaway
        instance named accordingly); AND
    (b) `BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE=1` is set explicitly --
        this can't be inferred from the URL alone, it's the operator
        affirmatively saying "yes, wipe this one".

    Raises `RuntimeError` (loud, not a silent no-op) if either gate
    fails. See `docs/operations/monetization-runbook.md` for the
    operational writeup.
    """
    host = (urlsplit(pg_url).hostname or "").lower()
    disposable_host = host in ("127.0.0.1", "localhost")
    disposable_name = "_staging" in pg_url or "_test" in pg_url
    if not (disposable_host or disposable_name):
        raise RuntimeError(
            "REFUSING to run the monetization test harness: "
            f"BILLCOMMONS_TEST_DATABASE_URL host={host!r} is not provably "
            "disposable (must be 127.0.0.1/localhost, or the URL must "
            "contain '_staging'/'_test'). This harness DELETEs rows from "
            "monetization tables before every test -- see "
            "docs/operations/monetization-runbook.md."
        )
    if os.environ.get("BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE") != "1":
        raise RuntimeError(
            "REFUSING to run the monetization test harness: "
            "BILLCOMMONS_TEST_DATABASE_URL is set but "
            "BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE=1 was not. This harness "
            "DELETEs rows from monetization tables before every test -- "
            "see docs/operations/monetization-runbook.md."
        )


# Gate at import time too, not just at first use -- a test file doing
# `from _monetization_sqlite import build_sqlite_app` with a miswired env
# var already set should fail loud before any test runs, not on whichever
# test happens to call `build_sqlite_app` first.
_pg_url_at_import = os.environ.get("BILLCOMMONS_TEST_DATABASE_URL")
if _pg_url_at_import:
    _assert_destructive_test_db_allowed(_pg_url_at_import)


def _make_sqlite_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # The schema's `server_default=text("gen_random_uuid()")` (pgcrypto)
    # has no SQLite equivalent -- register it as a SQLite user function
    # instead of touching the shared, Postgres-authoritative model
    # definitions (`UUIDPkMixin` et al) for a test-only concern.
    @event.listens_for(engine, "connect")
    def _register_gen_random_uuid(dbapi_connection, _connection_record):
        # .hex (32 lowercase chars, no dashes) -- SQLAlchemy's generic Uuid
        # type (what postgresql.UUID emulates on a non-PG dialect) stores
        # app-supplied UUIDs in exactly that form on SQLite; a hyphenated
        # str(uuid4()) here would silently NEVER equal a Python-side FK
        # value bound through the ORM, breaking every join.
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)

    Base.metadata.create_all(engine, tables=[m.__table__ for m in _MONETIZATION_MODELS])
    return engine


def _make_postgres_engine(pg_url: str):
    from billcommons_shared.db import _use_psycopg3

    # Defense in depth: also gate here, not just at import. Same checks,
    # cheap to repeat.
    _assert_destructive_test_db_allowed(pg_url)

    # NullPool: each test gets its own short-lived connection instead of a
    # pooled one held open across the whole test session -- this harness
    # is exercised by many independent test functions, not a long-running
    # process.
    engine = create_engine(_use_psycopg3(pg_url), poolclass=NullPool)
    # Schema is assumed already migrated (`alembic upgrade head` from
    # packages/schema) -- this harness only clears rows, it never creates
    # tables against Postgres. Plain `DELETE FROM` in `_DELETE_ORDER`
    # (children before parents), NOT `TRUNCATE ... CASCADE` -- CASCADE
    # reaches outside this table list to anything else with an FK to one
    # of these tables (fixlist item 1).
    with engine.begin() as conn:
        for model in _DELETE_ORDER:
            conn.execute(text(f"DELETE FROM {model.__tablename__}"))
    return engine


def build_sqlite_app(monkeypatch):
    """Returns (app, SessionLocal). Call from a test that received the
    `monkeypatch` fixture; every patch is auto-reverted at teardown.

    Despite the name (kept for the existing call sites), this now backs
    onto SQLite by default and onto a real Postgres instance when
    `BILLCOMMONS_TEST_DATABASE_URL` is set -- see the module docstring.
    """
    pg_url = os.environ.get("BILLCOMMONS_TEST_DATABASE_URL")
    engine = _make_postgres_engine(pg_url) if pg_url else _make_sqlite_engine()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    monkeypatch.setattr(api_keys_module, "_session_factory", SessionLocal)
    monkeypatch.setattr(quota_module, "_session_factory", SessionLocal)
    monkeypatch.setattr(admin_module, "get_session", SessionLocal)

    # Never let this suite make a REAL Resend API call. The shell's own
    # environment may carry a live RESEND_API_KEY (auto-sourced per-repo
    # secrets, unrelated to this test run) -- account.py's magic-link
    # email falls back to a WARN log with no key set (its own documented
    # behavior), which is exactly what a test run should do.
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    api_keys_module.clear_cache()

    from billcommons_api.app import create_app

    app = create_app()

    def _override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    return app, SessionLocal
