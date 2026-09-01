"""Guard tests for `_monetization_sqlite.py`'s destructive-DB refusal
(fixlist item 1). These call `_assert_destructive_test_db_allowed`
directly rather than exercising the module's import-time check, since
that check runs once per process against whatever
`BILLCOMMONS_TEST_DATABASE_URL` happens to already be set to (this test
run's own harness may be pointed at the throwaway Postgres instance).
"""
from __future__ import annotations

import pytest

from tests._monetization_sqlite import _assert_destructive_test_db_allowed


def test_refuses_railway_host(monkeypatch):
    monkeypatch.delenv("BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE", raising=False)
    with pytest.raises(RuntimeError, match="not provably disposable"):
        _assert_destructive_test_db_allowed(
            "postgresql://bc:pw@monorail.proxy.railway.app:12345/railway"
        )


def test_refuses_rlwy_net_host(monkeypatch):
    monkeypatch.delenv("BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE", raising=False)
    with pytest.raises(RuntimeError, match="not provably disposable"):
        _assert_destructive_test_db_allowed(
            "postgresql://bc:pw@viaduct.proxy.rlwy.net:54321/railway"
        )


def test_refuses_disposable_host_without_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE", raising=False)
    with pytest.raises(RuntimeError, match="BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE"):
        _assert_destructive_test_db_allowed("postgresql://bc@127.0.0.1:54329/bc_staging")


def test_allows_localhost_with_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE", "1")
    _assert_destructive_test_db_allowed("postgresql://bc@127.0.0.1:54329/bc_staging")


def test_allows_standard_local_socket_with_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE", "1")
    _assert_destructive_test_db_allowed(
        "postgresql:///billcommons_regression_test?host=/var/run/postgresql"
    )


def test_refuses_nonstandard_socket_even_with_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE", "1")
    with pytest.raises(RuntimeError, match="not provably disposable"):
        _assert_destructive_test_db_allowed(
            "postgresql:///billcommons_regression_test?host=/tmp/other-postgres"
        )


def test_allows_staging_named_url_with_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE", "1")
    _assert_destructive_test_db_allowed(
        "postgresql://bc:pw@billing.internal:5432/bc_staging"
    )


def test_refuses_test_marker_in_username_on_production_database(monkeypatch):
    """R4-7: the disposable marker must be in the database name, never a
    coincidental substring in credentials or the host."""
    monkeypatch.setenv("BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE", "1")
    with pytest.raises(RuntimeError, match="not provably disposable"):
        _assert_destructive_test_db_allowed("postgresql://bc_test@prod-host/railway")


@pytest.mark.parametrize(
    "url",
    [
        "postgresql:///billcommons_regression_test?host=/var/run/postgresql&host=example.invalid",
        "postgresql:///billcommons_regression_test?host=/var/run/postgresql&hostaddr=203.0.113.10",
        "postgresql:///billcommons_regression_test?host=/var/run/postgresql&service=remote",
        "postgresql:///billcommons_regression_test?host=/var/run/postgresql&servicefile=/tmp/pg_service.conf",
    ],
)
def test_refuses_ambiguous_libpq_targets_even_with_explicit_opt_in(monkeypatch, url):
    monkeypatch.setenv("BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE", "1")
    with pytest.raises(RuntimeError, match="not provably disposable"):
        _assert_destructive_test_db_allowed(url)
