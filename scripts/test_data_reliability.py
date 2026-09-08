#!/usr/bin/env python3
"""Run this mission's checks only inside pg_virtualenv's disposable cluster.

Usage (from repository root):
  pg_virtualenv -v 16 /path/to/venv/bin/python scripts/test_data_reliability.py

No application credential file is loaded. The inherited ephemeral libpq
credentials stay in the environment and are never written or displayed.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote


def main() -> int:
    # pg_virtualenv supplies these variables. Require loopback, a non-default
    # port, and its generated password rather than risking ambient app DB.
    if (os.environ.get("PGHOST") != "localhost"
            or not os.environ.get("PGPORT", "").isdigit()
            or os.environ.get("PGPORT") == "5432"
            or not os.environ.get("PGPASSWORD")
            or not os.environ.get("PGUSER")):
        print("REFUSING: run this script under pg_virtualenv on its disposable local cluster.", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    name = "billcommons_reliability_test"
    subprocess.run(["createdb", name], check=True)
    url = (f"postgresql+psycopg://{quote(os.environ['PGUSER'], safe='')}@localhost:"
           f"{os.environ['PGPORT']}/{name}")
    os.environ["DATABASE_URL"] = url
    os.environ["BILLCOMMONS_TEST_DATABASE_URL"] = url
    os.environ["BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE"] = "1"
    os.environ["PYTHONPATH"] = os.pathsep.join(str(root / path) for path in (
        "packages/schema", "packages/shared", "packages/search", "workers/ingest", "apps/api",
    ))
    subprocess.run([sys.executable, "-m", "alembic", "-c", "packages/schema/alembic.ini",
                    "upgrade", "head"], check=True)
    # Both service suites have a top-level `tests` package. Separate Python
    # processes avoid conftest/import collisions and preserve their real
    # fixture contracts without monkeypatching pytest's module loader.
    groups = [
        ["workers/ingest/tests/test_scheduler.py",
         "workers/ingest/tests/test_official_ca_actions.py",
         "workers/ingest/tests/test_official_discovery.py",
         "workers/ingest/tests/test_official_discovery_database.py",
         "workers/ingest/tests/test_official_observer.py",
         "workers/ingest/tests/test_official_worker.py",
         "workers/ingest/tests/test_source_budget.py",
         "workers/ingest/tests/test_openstates_api.py",
         "workers/ingest/tests/test_api_sync.py",
         "workers/ingest/tests/test_data_health_cli.py",
         "workers/ingest/tests/test_data_health_database.py",
         "packages/shared/tests/test_data_health.py",
         "packages/shared/tests/test_reconciliation.py"],
        ["apps/api/tests/test_data_health.py",
         "apps/api/tests/test_official_evidence.py",
         "apps/api/tests/test_official_evidence_database.py"],
    ]
    for tests in groups:
        result = subprocess.call([sys.executable, "-m", "pytest", *tests, "-q"])
        if result:
            return result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
