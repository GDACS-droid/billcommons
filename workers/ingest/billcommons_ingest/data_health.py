"""Standalone read-only CLI for the shared data-reliability report.

Usage::

    python -m billcommons_ingest.data_health
    python -m billcommons_ingest.data_health --json --fail-on error
"""
from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import text

from billcommons_shared.data_health import FAIL_ON_ORDER, collect_report, exit_code, render_text
from billcommons_shared.db import get_session


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Bill Commons ingestion reliability report")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--quiet", action="store_true", help="emit no report body")
    parser.add_argument(
        "--fail-on",
        choices=tuple(FAIL_ON_ORDER),
        help="return 1 when the ledger contains this severity or a higher one",
    )
    args = parser.parse_args(argv)

    db = get_session()
    try:
        # The report functions only issue SELECTs.  Do not commit here: this
        # command is safe to run while diagnosing a production incident.
        db.execute(text("SET TRANSACTION READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        report = collect_report(db)
    except Exception as exc:  # noqa: BLE001 - a broken control plane must be visible to monitoring
        if not args.quiet:
            print(f"[CHECK-FAILED] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        db.close()

    if not args.quiet:
        print(json.dumps(report, indent=2, sort_keys=True) if args.json else render_text(report))
    return exit_code(report, args.fail_on)


if __name__ == "__main__":
    raise SystemExit(main())
