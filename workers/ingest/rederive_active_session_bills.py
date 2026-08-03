#!/usr/bin/env python3
"""Re-derive bills wrongly marked died_on_adjournment by a stale estimate.

`sessions.end_date` holds the upstream `expected_adjournment` -- a PREDICTION.
The adjournment sweep marked every live bill dead once that date passed, without
consulting `sessions.active`. On 2026-08-02 that had killed 29,227 bills across
eight jurisdictions whose sessions the same source still reports as active.

This re-runs the (now fixed) derivation over exactly those bills. With
`session_active` respected, a bill in an active session falls back to whatever
its own action record supports.

Scoped deliberately narrowly: only bills that are BOTH currently
`died_on_adjournment` AND in a session flagged active. It cannot touch a bill
whose death was recorded normally, and it is idempotent -- a second run finds
nothing left to change.

    python rederive_active_session_bills.py            # dry run, prints the plan
    python rederive_active_session_bills.py --apply    # writes
    python rederive_active_session_bills.py --apply --only MA,NC,DE,AK
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select, text

from billcommons_ingest.cli import recompute_status_for_bills
from billcommons_shared.db import get_session

AFFECTED = text(
    """
    select b.id
      from bills b
      join sessions s on s.id = b.session_id
      join jurisdictions j on j.id = b.jurisdiction_id
     where b.status = 'died_on_adjournment'
       and s.active is true
       and (cast(:codes as text) is null or j.abbreviation = any(string_to_array(cast(:codes as text), ',')))
    """
)

# Every bill in a contradictory session, whatever its current status.
#
# Needed for a SECOND pass: once the first pass moved a bill off
# died_on_adjournment, a later refinement of the rule can no longer find it by
# status. South Carolina is the case in point -- its bills were re-derived to
# live statuses on the strength of clerical filings, and correcting that means
# revisiting bills that are no longer marked dead.
ALL_IN_CONTRADICTORY_SESSIONS = text(
    """
    select b.id
      from bills b
      join sessions s on s.id = b.session_id
      join jurisdictions j on j.id = b.jurisdiction_id
     where s.active is true
       and s.end_date is not null
       and s.end_date < current_date
       and (cast(:codes as text) is null or j.abbreviation = any(string_to_array(cast(:codes as text), ',')))
    """
)

SUMMARY = text(
    """
    select j.abbreviation, count(*) n
      from bills b
      join sessions s on s.id = b.session_id
      join jurisdictions j on j.id = b.jurisdiction_id
     where b.status = 'died_on_adjournment'
       and s.active is true
       and (cast(:codes as text) is null or j.abbreviation = any(string_to_array(cast(:codes as text), ',')))
     group by 1 order by n desc
    """
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--only", help="comma-separated jurisdiction codes, e.g. MA,NC,DE,AK")
    ap.add_argument(
        "--all-in-session",
        action="store_true",
        help="recompute EVERY bill in the contradictory sessions, not just the "
        "ones still marked died_on_adjournment (needed for a second pass)",
    )
    args = ap.parse_args()
    codes = args.only.upper() if args.only else None

    db = get_session()
    try:
        if args.all_in_session:
            ids_preview = list(
                db.execute(ALL_IN_CONTRADICTORY_SESSIONS, {"codes": codes}).scalars()
            )
            print(
                f"{'DRY RUN' if not args.apply else 'APPLYING'} -- ALL bills in "
                f"contradictory sessions, scope: {codes or 'ALL'} -> {len(ids_preview)}"
            )
            if not args.apply:
                print("\n(dry run -- re-run with --apply to write)")
                return 0
            changed = 0
            for i in range(0, len(ids_preview), 2000):
                chunk = ids_preview[i : i + 2000]
                updated, _c = recompute_status_for_bills(db, chunk, stamp=True)
                db.commit()
                changed += updated
                print(f"  ...{i + len(chunk)}/{len(ids_preview)}, {changed} changed", flush=True)
            print(f"done: {changed} bill(s) re-derived")
            return 0

        rows = list(db.execute(SUMMARY, {"codes": codes}))
        total = sum(r.n for r in rows)
        print(f"{'DRY RUN' if not args.apply else 'APPLYING'} -- scope: {codes or 'ALL affected'}")
        for r in rows:
            print(f"  {r.abbreviation:3} {r.n:>7}")
        print(f"  {'TOTAL':3} {total:>7}")
        if not total:
            print("nothing to do")
            return 0
        if not args.apply:
            print("\n(dry run -- re-run with --apply to write)")
            return 0

        ids = list(db.execute(AFFECTED, {"codes": codes}).scalars())
        changed = 0
        # Batched: recompute_status_for_bills builds IN lists, and one
        # jurisdiction here is 18,000 bills.
        for i in range(0, len(ids), 2000):
            chunk = ids[i : i + 2000]
            # stamp=True: these ARE real changes a watchlist should see. The
            # backfill's stamp=False exists so a wholesale re-derivation cannot
            # flood every feed; this is a bounded correction of ~29k rows that
            # consumers were actively misinformed about.
            updated, _cleared = recompute_status_for_bills(db, chunk, stamp=True)
            db.commit()
            changed += updated
            print(f"  ...{i + len(chunk)}/{len(ids)} processed, {changed} changed", flush=True)
        print(f"done: {changed} bill(s) re-derived")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
