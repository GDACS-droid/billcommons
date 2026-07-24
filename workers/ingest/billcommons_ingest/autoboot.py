"""Self-healing worker entrypoint for deployed environments.

On start: seed the registry, bootstrap every jurisdiction whose coverage is
below BOOTSTRAPPED using the bulk zips listed in data/registry/bulk-urls.json
(downloaded on demand — the data.openstates.org URLs are public), recompute
coverage, then drop into the long-lived queue worker loop.

Idempotent by construction: bootstrap re-runs skip unchanged checksums, so a
restart mid-way just resumes where it left off.
"""
import json
import pathlib
import re
import sys
import tempfile
import time
import urllib.request

from sqlalchemy import text

from billcommons_shared.db import get_session

from . import cli

ROOT = pathlib.Path(__file__).resolve().parents[3]
BULK_URLS = ROOT / "data" / "registry" / "bulk-urls.json"
UA = "BillCommons/0.1 (+https://billcommons.org; contact@billcommons.org)"
STATES_BELOW_BOOTSTRAPPED = ("NOT_STARTED", "SOURCE_IDENTIFIED")


def states_needing_bootstrap() -> set[str]:
    db = get_session()
    try:
        rows = db.execute(
            text(
                """
                SELECT j.abbreviation
                FROM jurisdiction_coverage c
                JOIN jurisdictions j ON j.id = c.jurisdiction_id
                GROUP BY j.abbreviation
                HAVING bool_and(c.status IN ('NOT_STARTED','SOURCE_IDENTIFIED'))
                    OR sum(coalesce(c.bill_count,0)) = 0
                """
            )
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        db.close()


def bootstrap_state(state: str, entries: list[dict]) -> bool:
    ok = True
    for entry in entries:
        url = entry["url"]
        slug = re.search(r"/latest/[A-Za-z]{2}_(.+?)_csv_", url)
        label = slug.group(1) if slug else "session"
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=True) as tmp:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=600) as r:
                    while chunk := r.read(1 << 20):
                        tmp.write(chunk)
                tmp.flush()
                rc = cli.main(
                    ["bootstrap", "--state", state, "--zip", tmp.name, "--source-url", url]
                )
                print(f"autoboot: {state} {label} rc={rc}", flush=True)
                ok = ok and rc == 0
            except Exception as exc:  # noqa: BLE001 - keep the fleet moving
                print(f"autoboot: FAIL {state} {label}: {exc}", flush=True)
                ok = False
        time.sleep(1.0)
    return ok


def main() -> int:
    print("autoboot: seeding registry", flush=True)
    cli.main(["seed-registry"])
    reg = json.loads(BULK_URLS.read_text())["jurisdictions"]
    pending = states_needing_bootstrap()
    order = [s for s in reg if not reg[s].get("optional") and s in pending]
    print(f"autoboot: {len(order)} jurisdictions need bootstrap: {sorted(order)}", flush=True)
    failures = []
    for state in sorted(order):
        if not bootstrap_state(state, reg[state]["current"]):
            failures.append(state)
        cli.main(["recompute-coverage"])
    if failures:
        print(f"autoboot: FAILURES (will retry on next restart): {failures}", flush=True)
    print("autoboot: entering worker loop", flush=True)
    return cli.main(["worker"])


if __name__ == "__main__":
    sys.exit(main())
