"""Verify ingest test collection fails closed before database imports."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


CONFTST = Path(__file__).resolve().parents[2] / "workers" / "ingest" / "tests" / "conftest.py"
SAFE_URL = "postgresql+psycopg://test_user:secret@127.0.0.1:54329/billcommons_test"


@pytest.mark.parametrize(
    ("name", "database_url", "extra_environment"),
    [
        ("missing URL", None, {}),
        ("wrong scheme", "mysql://test_user:secret@127.0.0.1/billcommons_test", {}),
        ("non-loopback host", "postgresql://test_user:secret@db.example/billcommons_test", {}),
        ("unsafe database suffix", "postgresql://test_user:secret@127.0.0.1/production", {}),
        ("missing destructive acknowledgement", SAFE_URL, {}),
        ("malformed URL", "postgresql://[malformed/billcommons_test", {}),
        ("query override", SAFE_URL + "?host=production", {"BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE": "1"}),
        ("fragment override", SAFE_URL + "#production", {"BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE": "1"}),
        ("non-loopback PGHOSTADDR", SAFE_URL, {"BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE": "1", "PGHOSTADDR": "10.0.0.8"}),
        ("PGSERVICE override", SAFE_URL, {"BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE": "1", "PGSERVICE": "production"}),
        ("PGSERVICEFILE override", SAFE_URL, {"BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE": "1", "PGSERVICEFILE": "/tmp/production-service.conf"}),
    ],
)
def test_ingest_conftest_rejects_unsafe_targets_before_database_import(
    name: str,
    database_url: str | None,
    extra_environment: dict[str, str],
) -> None:
    environment = os.environ.copy()
    for key in (
        "DATABASE_URL",
        "BILLCOMMONS_TEST_DATABASE_URL",
        "BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE",
        "PGHOSTADDR",
        "PGSERVICE",
        "PGSERVICEFILE",
    ):
        environment.pop(key, None)
    if database_url is not None:
        environment["DATABASE_URL"] = database_url
    environment.update(extra_environment)
    blocker = (
        "import importlib.abc, runpy, sys\n"
        "class Blocker(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname == 'billcommons_shared.db': raise RuntimeError('FORBIDDEN_IMPORT')\n"
        "sys.meta_path.insert(0, Blocker())\n"
        f"runpy.run_path({str(CONFTST)!r})"
    )
    result = subprocess.run(
        [sys.executable, "-c", blocker],
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, name
    assert "FORBIDDEN_IMPORT" not in result.stderr, name
    assert "ingest tests " in result.stderr, name
    assert "secret" not in result.stderr, name
