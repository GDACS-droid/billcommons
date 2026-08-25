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


def test_allows_staging_named_url_with_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE", "1")
    _assert_destructive_test_db_allowed(
        "postgresql://bc:pw@some-internal-host.example.net:5432/bc_staging"
    )
