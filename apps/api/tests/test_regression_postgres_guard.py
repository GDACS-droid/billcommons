"""Pure refusal tests for the broad PostgreSQL regression harness."""
from __future__ import annotations

import pytest

from tests._regression_postgres import require_disposable_postgres_url


@pytest.mark.parametrize(
    "url",
    [
        "postgresql:///billcommons_regression_test?host=/var/run/postgresql&host=example.invalid",
        "postgresql:///billcommons_regression_test?host=/var/run/postgresql&hostaddr=203.0.113.10",
        "postgresql:///billcommons_regression_test?host=/var/run/postgresql&service=remote",
        "postgresql:///billcommons_regression_test?host=/var/run/postgresql&servicefile=/tmp/pg_service.conf",
        "postgresql://localhost/billcommons_regression_test?host=/var/run/postgresql",
    ],
)
def test_refuses_ambiguous_libpq_target_before_connect(monkeypatch, url):
    monkeypatch.setenv("BILLCOMMONS_TEST_DATABASE_URL", url)
    monkeypatch.setenv("BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE", "1")
    with pytest.raises(RuntimeError, match="REFUSING API regression tests"):
        require_disposable_postgres_url()


def test_allows_acknowledged_standard_local_socket(monkeypatch):
    url = "postgresql:///billcommons_regression_test?host=/var/run/postgresql"
    monkeypatch.setenv("BILLCOMMONS_TEST_DATABASE_URL", url)
    monkeypatch.setenv("BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE", "1")
    assert require_disposable_postgres_url() == url
