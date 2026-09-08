"""Verify ingest test collection fails closed without a disposable DB target."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


CONFTST = Path(__file__).resolve().parents[2] / "workers" / "ingest" / "tests" / "conftest.py"


def test_ingest_conftest_rejects_missing_database_url_without_connecting() -> None:
    environment = os.environ.copy()
    environment.pop("DATABASE_URL", None)
    environment.pop("BILLCOMMONS_TEST_DATABASE_URL", None)
    environment.pop("BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE", None)
    result = subprocess.run(
        [sys.executable, "-c", f"import runpy; runpy.run_path({str(CONFTST)!r})"],
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "explicit DATABASE_URL" in result.stderr
    assert "postgresql://" not in result.stderr
