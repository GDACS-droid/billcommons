"""Safety gate for API tests that use the real PostgreSQL schema.

The public API contract tests create fixture rows and exercise Postgres-only
search behaviour.  They must never silently follow the normal application
``DATABASE_URL`` (including its local-env-file fallback) to Railway.
"""
from __future__ import annotations

import os
from urllib.parse import parse_qs, urlsplit


TEST_DATABASE_URL_ENV = "BILLCOMMONS_TEST_DATABASE_URL"
DESTRUCTIVE_ACK_ENV = "BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE"


def require_disposable_postgres_url() -> str:
    """Return the acknowledged local test URL or fail before a DB connection.

    A Unix-socket connection is local only when its ``host`` query parameter
    is the standard local Postgres socket directory.  TCP is restricted to
    localhost.  Requiring a ``_test`` database name prevents a plausible
    looking local production database from becoming a test target.
    """
    raw = os.environ.get(TEST_DATABASE_URL_ENV)
    if not raw:
        raise RuntimeError(
            f"REFUSING API regression tests without {TEST_DATABASE_URL_ENV}. "
            "Set it to an explicitly disposable local Postgres database ending in _test."
        )
    parsed = urlsplit(raw)
    if parsed.scheme not in {"postgres", "postgresql", "postgresql+psycopg"}:
        raise RuntimeError("REFUSING API regression tests: test database must be PostgreSQL")
    database_name = parsed.path.rstrip("/").rsplit("/", 1)[-1].lower()
    query = parse_qs(parsed.query, keep_blank_values=True)
    query_hosts = query.get("host", [])
    host = (parsed.hostname or "").lower()
    ambiguous_target = (
        len(query_hosts) > 1
        or bool(host and query_hosts)
        or any(query.get(key) for key in ("hostaddr", "service", "servicefile"))
    )
    query_host = query_hosts[0] if len(query_hosts) == 1 else ""
    is_local_tcp = host in {"localhost", "127.0.0.1", "::1"}
    is_local_socket = not host and query_host == "/var/run/postgresql"
    if ambiguous_target or not database_name.endswith("_test") or not (is_local_tcp or is_local_socket):
        raise RuntimeError(
            "REFUSING API regression tests: require localhost/127.0.0.1/::1 "
            "or host=/var/run/postgresql and a database name ending _test"
        )
    if os.environ.get(DESTRUCTIVE_ACK_ENV) != "1":
        raise RuntimeError(
            f"REFUSING API regression tests without {DESTRUCTIVE_ACK_ENV}=1; "
            "these tests create and delete fixture rows."
        )
    return raw


def configure_disposable_postgres() -> str:
    """Pin application DB resolution to the admitted test URL.

    This is deliberately called before any test fixture creates the FastAPI
    app.  The shared DB module otherwise accepts an ignored local env-file
    fallback, which may identify a live deployment database.
    """
    url = require_disposable_postgres_url()
    os.environ["DATABASE_URL"] = url
    return url
