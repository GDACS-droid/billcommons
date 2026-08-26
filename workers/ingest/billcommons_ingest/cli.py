"""`python -m billcommons_ingest {subcommand}` entrypoint.

Subcommands (per BRIEF-wave2.md):
    seed-registry                     Upsert all 51 jurisdictions/sessions/coverage
                                       rows from data/registry/sessions-2026.json.
    bootstrap --state XX --zip PATH   Ingest a session bulk-CSV zip for state XX.
    api-sync --state XX               Incremental sync via the v3 API (updated_since
                                       the jurisdiction's last successful run).
    recompute-coverage                Recompute jurisdiction_coverage + write
                                       docs/state-coverage/coverage-latest.json.
    enqueue-fulltext [--limit N]      Enqueue fetch_text jobs for bill_documents
                                       lacking extracted_text (idempotent).
    reset-fetch-attempts [filters]    Hand documents their fetch retry budget
                                       back (clears fetch_attempts + the
                                       permanently_failed note) after an
                                       outage or a fixed source. Requires an
                                       explicit filter (--document-id /
                                       --url-like / --jurisdiction / --all);
                                       --dry-run reports. --only-permanently-
                                       failed narrows to just that status
                                       (excludes worker_error) -- e.g.
                                       `reset-fetch-attempts --jurisdiction MA
                                       --only-permanently-failed` after a
                                       url_resolvers.py fix.
    validate --state XX [--sample N]  QA-sample bills against source-of-truth
                                       (search API + official source_url) and
                                       record validation_runs + coverage.
    validate --all                    Validate every loaded jurisdiction.
    schedule-refresh                  Enqueue api_sync jobs for jurisdictions
                                       due per SPEC "Refresh targets" cadence.
    worker                            Long-running loop: claim + process ingest_jobs,
                                       plus periodic schedule-refresh, fulltext top-up,
                                       coverage-recompute, and validate-schedule passes.
    validate-worker                   DEDICATED long-running validation-only loop,
                                       separate from `worker` -- never claims/touches
                                       fetch_text/api_sync jobs. Calls the transaction-
                                       free validation core directly (no DB session
                                       held during external HTTP) so it can never
                                       starve the crawl worker's connections. See
                                       cmd_validate_worker's docstring.
"""
from __future__ import annotations

import os
import argparse
import math
import random
import socket
import sys
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import case, func, or_, select, text, update

from billcommons_ingest import api_sync as api_sync_mod
from billcommons_ingest import browser_fetch as browser_fetch_mod
from billcommons_ingest import coverage as coverage_mod
from billcommons_ingest import events as events_mod
from billcommons_ingest import fulltext as fulltext_mod
from billcommons_ingest import host_auth as host_auth_mod
from billcommons_ingest import queue as queue_mod
from billcommons_ingest import registry as registry_mod
from billcommons_ingest import scheduler as scheduler_mod
from billcommons_ingest import status as status_mod
from billcommons_ingest import validation as validation_mod
from billcommons_ingest.openstates_api import OpenStatesDailyBudgetExceeded
from billcommons_ingest.openstates_bulk import ingest_session_csv_zip, peek_session_slug
from billcommons_ingest.session_match import (
    MatchPath,
    SessionCandidate,
    infer_classification_for_new_session,
    resolve_session,
)
from billcommons_schema.models import (
    Bill,
    BillAction,
    BillDocument,
    BillVersion,
    IngestJob,
    IngestionRun,
    Jurisdiction,
    JurisdictionCoverage,
    RelatedBill,
    Session as SessionModel,
)
from billcommons_shared.db import get_session
from billcommons_shared.normalize import normalize_bill_number
from billcommons_shared.rawstore import FilesystemRawStore

# Ceiling on one adjournment-sweep pass. A sine die can retire several thousand
# bills at once; capping the pass keeps a single cycle bounded, and whatever is
# left is picked up next cycle because the query is self-selecting.
ADJOURNMENT_SWEEP_BATCH = 20000

# How recently a session must have seen ANY filed action to count as
# corroborated-live when the source's `active` flag and its own
# `expected_adjournment` date contradict each other.
#
# 30 days is the midpoint of what a seven-model review converged on (14-45).
# It is itself a threshold, i.e. the same class of assumption as the predicted
# adjournment date that caused the original bug -- which is precisely why it is
# only ever used to CONFIRM life, never to infer death. Silence past this
# window yields "unknown", not "dead".
SESSION_ACTIVITY_WINDOW_DAYS = int(
    os.environ.get("BILLCOMMONS_SESSION_ACTIVITY_WINDOW_DAYS", "30") or 30
)

# Action classifications that only occur while a chamber is actually sitting.
#
# Volume alone is not evidence of a live session. South Carolina's most recent
# filings were "Scrivener's error corrected", "Effective date 06/30/26" and
# "Act No. 250" -- clerical bookkeeping on laws already passed, filed weeks
# after the chamber went home. Counting those as proof of life would have kept
# 3,685 SC bills reported as pending on the strength of a typo correction.
#
# Executive-side actions are excluded for the same reason: a governor signs,
# vetoes and receives bills long after adjournment, and Alaska's entire recent
# record is exactly that. Unclassified actions are excluded too -- they cannot
# be shown to be chamber activity, and this signal may only ever CONFIRM life.
CHAMBER_ACTIVITY_PATTERNS = (
    "%referral-committee%",
    "%committee-passage%",
    "%reading-%",
    "%passage%",
    "%amendment-%",
    "%introduction%",
    "%failure%",
    "%withdrawal%",
    "%deferral%",
)

# Jurisdictions queried per cycle for missing session end dates. The upstream
# free tier is 250 requests/day shared with the actual bill sync, so this stays
# small -- starving the sync to fill a date would be a bad trade, and the set
# shrinks to zero as dates land.
SESSION_DATE_TOPUP_PER_CYCLE = 5

DEFAULT_REGISTRY_PATH = "data/registry/sessions-2026.json"
DEFAULT_COVERAGE_OUTPUT = "docs/state-coverage/coverage-latest.json"

# Job kinds the crawl worker (`cmd_worker`) must never claim, no matter who
# queued them. An api_sync job makes many rate-limited Open States v3 calls;
# running it inside the crawl loop holds that loop's DB session open across
# minutes of HTTP, which shows up as `idle in transaction` and stalls every
# fetch_text behind it. This froze the crawl twice, so the rule is enforced
# at the claim rather than left to whoever enqueues. api_sync is meant to run
# as its own paced process/service (the validate-worker pattern).
CRAWL_WORKER_EXCLUDED_KINDS = (scheduler_mod.API_SYNC_KIND,)


def _record_run(db, jurisdiction_id, session_id, source_name: str):
    run = IngestionRun(
        jurisdiction_id=jurisdiction_id,
        session_id=session_id,
        source_name=source_name,
        started_at=datetime.now(timezone.utc),
        status="running",
    )
    db.add(run)
    db.flush()
    return run


def cmd_seed_registry(args: argparse.Namespace) -> int:
    db = get_session()
    try:
        counts = registry_mod.seed_registry(db, args.registry or DEFAULT_REGISTRY_PATH)
        db.commit()
        print(
            f"seed-registry: jurisdictions={counts['jurisdictions']} "
            f"sessions={counts['sessions']} coverage_rows={counts['coverage_rows']}"
        )
        return 0
    except Exception:
        db.rollback()
        traceback.print_exc()
        return 1
    finally:
        db.close()


def _resolve_or_create_session(
    db, jurisdiction: Jurisdiction, args: argparse.Namespace
) -> SessionModel | None:
    """Resolve which `sessions` row a bootstrap zip belongs to, trying (in
    order): (a) exact identifier match, (b) fuzzy slug match against the
    jurisdiction's existing sessions, (c) create a new session row from the
    zip's own slug metadata. Logs which path was taken. Returns None only if
    every path failed (should be unreachable in practice since (c) always
    succeeds, but kept defensive)."""
    # --- path (a): exact identifier match ---------------------------------
    identifier = args.session
    if identifier is None:
        # No explicit --session: sniff the zip's own session_identifier
        # column (e.g. "2026rs") so paths (a)/(b)/(c) below have a slug to
        # work with, same as if the caller had passed --session explicitly.
        identifier = peek_session_slug(args.zip)

    if identifier:
        session_row = db.execute(
            select(SessionModel).where(
                SessionModel.jurisdiction_id == jurisdiction.id,
                SessionModel.identifier == identifier,
            )
        ).scalar_one_or_none()
        if session_row is not None:
            print(f"bootstrap {args.state}: session resolved via path=exact identifier={identifier!r}")
            return session_row

    if identifier is None:
        # Truly nothing to key off of (no --session, no session_identifier
        # column in the zip) -- fall back to the jurisdiction's active
        # session, same as the pre-fix behavior.
        session_row = db.execute(
            select(SessionModel)
            .where(SessionModel.jurisdiction_id == jurisdiction.id, SessionModel.active.is_(True))
            .order_by(SessionModel.start_date.desc().nulls_last())
        ).scalars().first()
        if session_row is not None:
            print(f"bootstrap {args.state}: session resolved via path=active-session-fallback")
        return session_row

    # --- path (b): fuzzy match against existing sessions -------------------
    existing_sessions = db.execute(
        select(SessionModel).where(SessionModel.jurisdiction_id == jurisdiction.id)
    ).scalars().all()
    candidates = [
        SessionCandidate(identifier=s.identifier, classification=s.classification)
        for s in existing_sessions
    ]
    match = resolve_session(identifier, candidates)
    if match.path == MatchPath.FUZZY:
        session_row = next(
            s for s in existing_sessions if s.identifier == match.candidate.identifier
        )
        print(
            f"bootstrap {args.state}: session resolved via path=fuzzy "
            f"slug={identifier!r} -> identifier={session_row.identifier!r}"
        )
        return session_row

    # --- path (c): create a new session row from the zip's own slug -------
    print(
        f"bootstrap {args.state}: no exact/fuzzy match for slug={identifier!r} "
        f"({match.reason}); creating a new session row (path=create)"
    )
    classification = infer_classification_for_new_session(identifier)
    session_row = SessionModel(
        jurisdiction_id=jurisdiction.id,
        identifier=identifier,
        classification=classification,
        active=True,
        source_name="Open States bulk",
        source_url=args.source_url,
    )
    db.add(session_row)
    db.flush()

    coverage = db.execute(
        select(JurisdictionCoverage).where(
            JurisdictionCoverage.jurisdiction_id == jurisdiction.id,
            JurisdictionCoverage.session_id == session_row.id,
        )
    ).scalar_one_or_none()
    if coverage is None:
        coverage = JurisdictionCoverage(
            jurisdiction_id=jurisdiction.id,
            session_id=session_row.id,
            status="SOURCE_IDENTIFIED",
        )
        db.add(coverage)
        db.flush()

    print(
        f"bootstrap {args.state}: created session identifier={identifier!r} "
        f"classification={classification!r} + jurisdiction_coverage row"
    )
    return session_row


def cmd_bootstrap(args: argparse.Namespace) -> int:
    db = get_session()
    try:
        jurisdiction = db.execute(
            select(Jurisdiction).where(Jurisdiction.abbreviation == args.state.upper())
        ).scalar_one_or_none()
        if jurisdiction is None:
            print(f"bootstrap: no jurisdiction row for state {args.state!r}; run seed-registry first")
            return 1

        session_row = _resolve_or_create_session(db, jurisdiction, args)

        if session_row is None:
            print(f"bootstrap: no matching session row for state {args.state!r}; run seed-registry first")
            return 1

        run = _record_run(db, jurisdiction.id, session_row.id, "openstates_bulk_csv")
        rawstore = FilesystemRawStore()
        try:
            result = ingest_session_csv_zip(
                db,
                args.zip,
                session_row=session_row,
                rawstore=rawstore,
                progress_prefix=f"{args.state} {session_row.identifier}",
            )
            run.status = "success"
            run.finished_at = datetime.now(timezone.utc)
            run.bills_created = result.bills_created
            run.bills_updated = result.bills_updated
            db.commit()
            print(
                f"bootstrap {args.state}: created={result.bills_created} "
                f"updated={result.bills_updated} unchanged={result.bills_unchanged} "
                f"actions={result.actions} sponsorships={result.sponsorships} "
                f"versions={result.versions} documents={result.documents} "
                f"votes={result.vote_events} raw_ref={result.raw_ref}"
            )
            if result.warnings:
                print(f"bootstrap {args.state}: {len(result.warnings)} warning(s):")
                for warning in result.warnings[:20]:
                    print(f"  - {warning}")
            return 0
        except Exception as exc:
            run.status = "failed"
            run.finished_at = datetime.now(timezone.utc)
            run.error = str(exc)[:4000]
            db.commit()
            raise
    except Exception:
        db.rollback()
        traceback.print_exc()
        return 1
    finally:
        db.close()


def cmd_api_sync(args: argparse.Namespace) -> int:
    db = get_session()
    try:
        result = api_sync_mod.run_api_sync_job(db, args.state)
        db.commit()
        print(
            f"api-sync {args.state}: pages={result.pages_fetched} "
            f"created={result.bills_created} updated={result.bills_updated} "
            f"unchanged={result.bills_unchanged} actions={result.actions} "
            f"sponsorships={result.sponsorships} versions={result.versions} "
            f"documents={result.documents} next_page={result.next_page}"
        )
        if result.warnings:
            print(f"api-sync {args.state}: {len(result.warnings)} warning(s):")
            for warning in result.warnings[:20]:
                print(f"  - {warning}")
        return 0
    except Exception:
        db.rollback()
        traceback.print_exc()
        return 1
    finally:
        db.close()


_BACKFILL_TOTAL_KEYS = (
    "bills_created",
    "bills_updated",
    "bills_unchanged",
    "actions",
    "sponsorships",
    "versions",
    "documents",
    "pages_fetched",
)


def run_api_versions_backfill(
    state: str,
    since: str,
    *,
    start_page: int = 1,
    page_budget: int = 10,
    commit_pages: int = 5,
    client: api_sync_mod.OpenStatesClient | None = None,
    session_factory=get_session,
) -> dict:
    """Page-resumable one-shot replay of the Open States v3 API for one
    jurisdiction, from an explicit `--since` timestamp, to backfill
    `bill_versions`/`bill_documents` a metadata-only `api_sync` run missed
    (see the "catch-up design" doc). Calls `api_sync.sync_state` directly,
    chunked at `commit_pages` pages per transaction, and NEVER writes an
    `ingestion_runs(source_name='openstates_api_sync')` row -- the ordinary
    incremental-sync watermark must not move, and must not even be read,
    for this explicit-`--since` path (`sync_state`'s
    `updated_since_override` bypasses it entirely). Never enqueues fulltext
    directly -- the crawl worker's existing periodic top-up discovers the
    new `bill_documents` rows on its own schedule.

    Constructs exactly ONE `OpenStatesClient` for the whole invocation (its
    6/minute token bucket must span every chunk's requests) -- never one
    per page/chunk.

    Returns a dict of aggregate counters plus `status` (`"complete"` once
    `next_page` comes back `None`, `"partial"` if the page budget ran out
    first, `"error"` if a chunk raised) and `resume_page`/`next_page`. A
    later chunk's failure is reported (and the resume point printed) rather
    than raised, so callers can inspect `result["status"]` and choose their
    own exit code; earlier committed chunks are preserved untouched.
    """
    if commit_pages > page_budget:
        commit_pages = page_budget

    client = client or api_sync_mod.OpenStatesClient()

    resolve_db = session_factory()
    try:
        jurisdiction_row = resolve_db.execute(
            select(Jurisdiction).where(Jurisdiction.abbreviation == state.upper())
        ).scalar_one_or_none()
        if jurisdiction_row is None:
            raise ValueError(f"no jurisdiction row for state {state!r}; run seed-registry first")
        jurisdiction_id = jurisdiction_row.id
    finally:
        resolve_db.close()

    totals = {key: 0 for key in _BACKFILL_TOTAL_KEYS}
    page = start_page
    remaining = page_budget
    max_page_seen = 0
    next_page = None
    status = "complete"
    resume_page = None
    error = None

    while remaining > 0:
        chunk_pages = min(commit_pages, remaining)
        db = session_factory()
        try:
            jurisdiction = db.get(Jurisdiction, jurisdiction_id)
            result = api_sync_mod.sync_state(
                db,
                jurisdiction,
                client=client,
                max_pages=chunk_pages,
                updated_since_override=since,
                start_page=page,
            )
            db.commit()
        except Exception as exc:  # noqa: BLE001 - reported as resume point, not raised
            db.rollback()
            status = "error"
            error = str(exc)
            resume_page = page
            print(
                f"backfill-api-versions {state}: chunk starting at page {page} FAILED: {exc}",
                flush=True,
            )
            print(
                f"backfill-api-versions {state}: resume point is page {resume_page}",
                flush=True,
            )
            break
        finally:
            db.close()

        for key in _BACKFILL_TOTAL_KEYS:
            totals[key] += getattr(result, key)
        max_page_seen = max(max_page_seen, result.max_page_seen)
        next_page = result.next_page
        remaining -= result.pages_fetched
        print(
            f"backfill-api-versions {state}: chunk page={page} pages_fetched={result.pages_fetched} "
            f"created={result.bills_created} updated={result.bills_updated} "
            f"unchanged={result.bills_unchanged} actions={result.actions} "
            f"sponsorships={result.sponsorships} versions={result.versions} "
            f"documents={result.documents} max_page_seen={result.max_page_seen} "
            f"next_page={result.next_page}",
            flush=True,
        )

        if next_page is None:
            status = "complete"
            break
        page = next_page
        if remaining <= 0:
            status = "partial"
            break

    if status == "partial":
        print(
            f"backfill-api-versions {state}: PARTIAL -- page budget exhausted before "
            f"next_page was None. Resume with: python -m billcommons_ingest "
            f"backfill-api-versions --state {state} --since {since} --start-page {next_page} "
            f"--page-budget {page_budget} --commit-pages {commit_pages}",
            flush=True,
        )
    elif status == "complete":
        print(
            f"backfill-api-versions {state}: COMPLETE -- next_page is None, replay window "
            f"fully covered",
            flush=True,
        )

    return {
        "status": status,
        "resume_page": resume_page,
        "next_page": next_page,
        "max_page_seen": max_page_seen,
        "error": error,
        **totals,
    }


def cmd_backfill_api_versions(args: argparse.Namespace) -> int:
    try:
        datetime.fromisoformat(args.since)
    except ValueError:
        print(f"backfill-api-versions: --since {args.since!r} is not a valid ISO-8601 timestamp")
        return 1
    if args.start_page < 1:
        print("backfill-api-versions: --start-page must be >= 1")
        return 1
    if args.page_budget < 1:
        print("backfill-api-versions: --page-budget must be >= 1")
        return 1
    if args.commit_pages < 1:
        print("backfill-api-versions: --commit-pages must be >= 1")
        return 1
    try:
        result = run_api_versions_backfill(
            args.state,
            args.since,
            start_page=args.start_page,
            page_budget=args.page_budget,
            commit_pages=args.commit_pages,
        )
    except ValueError:
        traceback.print_exc()
        return 1
    return 0 if result["status"] in ("complete", "partial") else 1


def cmd_validate(args: argparse.Namespace) -> int:
    if not args.all and not args.state:
        print("validate: one of --state XX or --all is required")
        return 1

    db = get_session()
    try:
        if args.all:
            jurisdictions = db.execute(select(Jurisdiction).order_by(Jurisdiction.abbreviation)).scalars().all()
        else:
            jurisdiction = db.execute(
                select(Jurisdiction).where(Jurisdiction.abbreviation == args.state.upper())
            ).scalar_one_or_none()
            if jurisdiction is None:
                print(f"validate: no jurisdiction row for state {args.state!r}; run seed-registry first")
                return 1
            jurisdictions = [jurisdiction]

        exit_code = 0
        for jurisdiction in jurisdictions:
            summary, run = validation_mod.validate_and_record(
                db, jurisdiction, sample_size=args.sample
            )
            db.commit()
            rate = f"{summary.pass_rate:.0%}" if summary.pass_rate is not None else "n/a"
            print(
                f"validate {jurisdiction.abbreviation}: sampled={len(summary.bills)} "
                f"checks_run={summary.checks_run} checks_failed={summary.checks_failed} "
                f"pass_rate={rate}"
            )
            for bill in summary.bills:
                for leg in bill.legs:
                    print(f"  {jurisdiction.abbreviation} {bill.identifier}: {leg.leg}={leg.status} ({leg.detail})")
                if any(leg.status == "fail" for leg in bill.legs):
                    exit_code = 1
        return exit_code
    except Exception:
        db.rollback()
        traceback.print_exc()
        return 1
    finally:
        db.close()


def cmd_schedule_refresh(args: argparse.Namespace) -> int:
    db = get_session()
    try:
        enqueued = scheduler_mod.run_schedule_pass(db)
        db.commit()
        print(f"schedule-refresh: enqueued api_sync for {len(enqueued)} jurisdiction(s): {sorted(enqueued)}")
        return 0
    except Exception:
        db.rollback()
        traceback.print_exc()
        return 1
    finally:
        db.close()


def cmd_recompute_coverage(args: argparse.Namespace) -> int:
    db = get_session()
    try:
        report = coverage_mod.recompute_and_write(db, args.output or DEFAULT_COVERAGE_OUTPUT)
        db.commit()
        print(
            f"recompute-coverage: {report['jurisdiction_count']} jurisdictions, "
            f"{len(report['rows'])} coverage rows -> {args.output or DEFAULT_COVERAGE_OUTPUT}"
        )
        return 0
    except Exception:
        db.rollback()
        traceback.print_exc()
        return 1
    finally:
        db.close()


def cmd_enqueue_fulltext(args: argparse.Namespace) -> int:
    db = get_session()
    try:
        count = fulltext_mod.enqueue_fulltext_jobs(db, limit=args.limit)
        db.commit()
        print(f"enqueue-fulltext: enqueued {count} fetch_text job(s)")
        return 0
    except Exception:
        db.rollback()
        traceback.print_exc()
        return 1
    finally:
        db.close()


def cmd_browser_fetch(
    args: argparse.Namespace,
    *,
    tunnel_check=browser_fetch_mod.tunnel_is_up,
    session_factory=get_session,
    rawstore_factory=FilesystemRawStore,
) -> int:
    """Run the human-attended CDP fetch path, never the normal crawl path."""
    hosts = browser_fetch_mod.ALLOWLIST if args.all_hosts else (args.host,)
    # A dry-run reads only the queue.  It deliberately remains useful while
    # the attended browser and reverse tunnel are offline.
    if not args.dry_run and not tunnel_check():
        print("browser-fetch: tunnel down")
        return 0

    db = session_factory()
    try:
        rawstore = rawstore_factory()
        if args.dry_run:
            summary = browser_fetch_mod.run_browser_fetch(
                db,
                hosts=hosts,
                limit=args.limit,
                pace=args.pace,
                dry_run=True,
                rawstore=rawstore,
                max_seconds=getattr(args, "max_seconds", 1500.0),
                # Dry runs never call the injected fetcher.
                fetch_via_browser=lambda _url: (_ for _ in ()).throw(AssertionError("dry run fetched")),
            )
        else:
            with browser_fetch_mod.connected_browser_fetcher(hosts) as fetch_via_browser:
                summary = browser_fetch_mod.run_browser_fetch(
                    db,
                    hosts=hosts,
                    limit=args.limit,
                    pace=args.pace,
                    dry_run=False,
                    rawstore=rawstore,
                    fetch_via_browser=fetch_via_browser,
                    max_seconds=getattr(args, "max_seconds", 1500.0),
                )
        db.commit()
        browser_fetch_mod.print_summary(summary)
        return 0
    except browser_fetch_mod.BrowserTunnelLost:
        db.rollback()
        print("browser-fetch: tunnel lost after 0 docs")
        return 0
    except Exception:
        db.rollback()
        traceback.print_exc()
        return 1
    finally:
        db.close()


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _non_negative_finite_float(value: str) -> float:
    """Reject negative/non-finite (`nan`/`inf`) values at the argparse
    layer -- `run_browser_fetch`'s own `ValueError`/`sleep()` would
    otherwise surface as a full traceback via `cmd_browser_fetch`'s generic
    `except Exception` instead of a clean CLI-level rejection (R3-9)."""
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative, finite number")
    return parsed


RESETTABLE_DEFAULT_STATUSES = ("permanently_failed", "worker_error")


_LIKE_ESCAPE = "\\"


def _escape_like(value: str) -> str:
    """Escape LIKE metacharacters (`\\`, `%`, `_`) so a host containing one of
    these (legal in a hostname label for `_`) cannot over-match a
    differently-spelled host in a stored URL."""
    return (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _robots_exempt_url_filter(hosts: frozenset[str]):
    """SQL predicate for configured exact hosts' normal HTTP(S) URLs.

    Compares against `func.lower(BillDocument.url)` so a stored URL with a
    different host case (`HTTPS://LIMS.DCCOUNCIL.GOV/...`) still matches --
    the runtime exemption check (`host_auth.robots_exempt`) lowercases the
    parsed hostname via `urlsplit`, and this filter must agree with it or a
    differently-cased document becomes unresettable forever.

    Covers every URL shape `urlsplit().hostname` would resolve to this same
    host: with a path, with a port, with a bare query string (no path, no
    port), and the host with nothing after it at all (no path/query/port) --
    the four `.../%`+`...: %` LIKE patterns alone miss the last two shapes.
    Host strings are escaped before being embedded in the LIKE pattern so a
    literal `_`/`%`/`\\` in a configured host cannot act as a wildcard.

    https-only: `host_auth.HostAuth.robots_exempt` requires `scheme ==
    "https"`, so an `http://` stored URL can never carry a live exemption --
    only https:// patterns belong here.
    """
    if not hosts:
        return BillDocument.id.is_(None)
    lowered_url = func.lower(BillDocument.url)
    return or_(
        *(
            or_(
                lowered_url.like(f"https://{_escape_like(host.lower())}/%", escape=_LIKE_ESCAPE),
                lowered_url.like(f"https://{_escape_like(host.lower())}:%", escape=_LIKE_ESCAPE),
                lowered_url.like(f"https://{_escape_like(host.lower())}?%", escape=_LIKE_ESCAPE),
                lowered_url.like(f"https://{_escape_like(host.lower())}", escape=_LIKE_ESCAPE),
            )
            for host in hosts
        )
    )


def cmd_reset_fetch_attempts(args: argparse.Namespace) -> int:
    """Give documents their fetch-retry budget back.

    `bill_documents.fetch_attempts` is what stops a permanently-broken URL from
    being re-fetched forever, but a counter with no way DOWN turns any wide
    failure into permanent, silent data loss: an hour of expired object-store
    credentials, a flapping connection pool, or one bad deploy can charge
    MAX_FETCH_ATTEMPTS failures to whatever documents were in flight, stamp
    them `fulltext_status=permanently_failed` (terminal, so
    enqueue_fulltext_jobs never offers them again), and nothing in the system
    would ever retry them. The same applies, less dramatically, when a state
    finally fixes a source that 404ed for a week.

    cmd_worker only charges failures it can attribute to the document itself
    (fulltext.is_document_specific_failure), so this should be rare -- but
    "should be rare" is not a recovery plan, and hand-written UPDATEs against
    production are not one either.

    Deliberately explicit: one of --document-id / --url-like / --jurisdiction
    / --all is REQUIRED. The unfiltered form would hand every one of ~690k
    documents a fresh budget and re-open the poison loop the counter exists
    to close.

    Only license_note values named by --status (default: permanently_failed
    and worker_error -- the self-inflicted ones) are cleared. A
    robots_disallowed document is reset only when its exact URL host currently
    carries configured, token-authorized robots exemption; all other robots
    verdicts remain terminal.
    --only-permanently-failed narrows that further to JUST permanently_failed
    (excluding worker_error) -- the requeue path after a URL-resolver fix
    (see url_resolvers.py), where the fix is document-specific and a
    worker_error is unrelated infrastructure noise that shouldn't be
    conflated with it.
    """
    robots_exempt_hosts = host_auth_mod.robots_exempt_hosts()
    default_statuses = list(RESETTABLE_DEFAULT_STATUSES)
    if robots_exempt_hosts:
        default_statuses.append(fulltext_mod.STATUS_ROBOTS_DISALLOWED)
    if args.only_permanently_failed:
        statuses = [fulltext_mod.STATUS_PERMANENTLY_FAILED]
    else:
        requested_statuses = [args.status] if isinstance(args.status, str) else args.status
        statuses = requested_statuses or default_statuses
    # license_note_matches_status tolerates the decorated forms _mark_status
    # can stamp (e.g. `permanently_failed browser_attempted_at=...`,
    # ` robots=api_token_exempt`) -- an exact-string match alone leaves the
    # decorated note un-cleared even after fetch_attempts is reset, silently
    # excluding it from enqueue_fulltext_jobs forever (R3-1). robots_disallowed
    # gets one extra guard: it is resettable only for a URL whose host
    # currently carries a configured, token-authorized robots exemption --
    # every other robots verdict remains terminal even if named explicitly.
    status_predicates = []
    for status in statuses:
        predicate = fulltext_mod.license_note_matches_status(BillDocument.license_note, [status])
        if status == fulltext_mod.STATUS_ROBOTS_DISALLOWED:
            predicate = predicate & _robots_exempt_url_filter(robots_exempt_hosts)
        status_predicates.append(predicate)
    note_matches = or_(*status_predicates)

    filters = []
    if args.document_id:
        try:
            ids = [uuid.UUID(str(value)) for value in args.document_id]
        except ValueError as exc:
            print(f"reset-fetch-attempts: bad --document-id ({exc})")
            return 2
        filters.append(BillDocument.id.in_(ids))
    if args.url_like:
        filters.append(BillDocument.url.like(args.url_like))
    if args.jurisdiction:
        abbreviations = [abbr.upper() for abbr in args.jurisdiction]
        jurisdiction_doc_ids = (
            select(BillDocument.id)
            .select_from(BillDocument)
            .join(BillVersion, BillVersion.id == BillDocument.bill_version_id)
            .join(Bill, Bill.id == BillVersion.bill_id)
            .join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id)
            .where(Jurisdiction.abbreviation.in_(abbreviations))
        )
        filters.append(BillDocument.id.in_(jurisdiction_doc_ids))
    if not filters and not args.all:
        print(
            "reset-fetch-attempts: refusing to run unfiltered -- pass "
            "--document-id/--url-like/--jurisdiction, or --all if you really "
            "mean every document"
        )
        return 2

    # Nothing to reset unless the document actually carries state: either a
    # spent budget or one of the resettable notes. Keeps --all from rewriting
    # (and bumping updated_at on) hundreds of thousands of untouched rows.
    filters.append(or_(BillDocument.fetch_attempts > 0, note_matches))

    db = get_session()
    try:
        breakdown = db.execute(
            select(
                BillDocument.license_note,
                func.count().label("n"),
                func.sum(case((BillDocument.fetch_attempts > 0, 1), else_=0)).label("with_attempts"),
            )
            .where(*filters)
            .group_by(BillDocument.license_note)
            .order_by(func.count().desc())
        ).all()
        total = sum(row.n for row in breakdown)
        print(f"reset-fetch-attempts: {total:,} document(s) match")
        for row in breakdown:
            print(f"  {str(row.license_note):45} {row.n:>8,}  ({row.with_attempts or 0:,} with attempts)")
        if args.dry_run:
            print("reset-fetch-attempts: --dry-run, nothing written")
            return 0
        if total == 0:
            return 0

        scope = list(filters)
        if args.limit:
            ids = db.execute(select(BillDocument.id).where(*filters).limit(args.limit)).scalars().all()
            scope = [BillDocument.id.in_(ids)]

        # A single UPDATE, not two: the counter reset and the note-clearing
        # decision must be evaluated against the SAME row snapshot under the
        # SAME row lock. Two back-to-back UPDATEs leave a gap between them --
        # a row outside `scope` when the first statement ran (e.g.
        # fetch_attempts == 0 with a non-resettable note) can be pushed into
        # `scope` by a concurrent worker committing fetch_attempts=1 plus a
        # resettable note in that gap; the second statement, re-evaluating
        # `scope AND resettable_note_filter` at its own write time, would
        # then match and null the note the first statement never touched --
        # silently erasing a note the first statement's WHERE never even
        # considered. Folding both writes into one statement closes the gap:
        # there is no second write that can see a different snapshot.
        result = db.execute(
            update(BillDocument)
            .where(*scope)
            .values(
                fetch_attempts=0,
                license_note=case(
                    (note_matches, None),
                    else_=BillDocument.license_note,
                ),
            )
            .execution_options(synchronize_session=False)
        )
        db.commit()
        print(
            f"reset-fetch-attempts: reset {result.rowcount:,} document(s); they become "
            f"eligible again on the next enqueue-fulltext / worker top-up pass"
        )
        return 0
    except Exception:
        db.rollback()
        traceback.print_exc()
        return 1
    finally:
        db.close()


def cmd_worker(args: argparse.Namespace) -> int:
    worker_id = args.worker_id or f"{socket.gethostname()}-{sys.argv[0]}"
    poll_interval = args.poll_interval
    reschedule_interval = args.reschedule_interval
    print(f"worker {worker_id}: polling every {poll_interval}s (Ctrl-C to stop)")

    # Shared across the whole worker-loop lifetime so per-host robots.txt
    # caching and the politeness rate-limit bucket persist across jobs
    # instead of resetting on every claim.
    fulltext_fetcher = fulltext_mod.FullTextFetcher()
    rawstore = FilesystemRawStore()
    last_schedule_pass = 0.0
    last_fulltext_topup = 0.0
    # Keep the fetch_text queue fed toward full-text coverage of the whole
    # corpus. enqueue_fulltext_jobs is idempotent (skips already-queued and
    # terminal-status documents), so topping up when the queue runs low
    # steadily drains all ~730k documents without re-enqueuing finished work.
    fulltext_topup_interval = getattr(args, "fulltext_topup_interval", 600)
    fulltext_topup_batch = getattr(args, "fulltext_topup_batch", 5000)
    fulltext_topup_floor = getattr(args, "fulltext_topup_floor", 1000)
    # Coverage-convergence loop (docs/SPEC.md "Coverage state machine + GREEN
    # criteria"): recompute keeps bill_count/full_text_count fresh + advances
    # non-terminal rows to FULL_TEXT_SEARCHABLE as the fulltext crawl lands
    # text; the validation scheduler then keeps feeding the ONLY thing that
    # can move a row into/out of VALIDATING/GREEN/DEGRADED (validation.py).
    # Neither pass alone converges coverage to GREEN -- see cli.py module
    # docstring / this function's surrounding periodic-pass comments.
    last_coverage_recompute = 0.0
    last_validate_schedule = 0.0
    coverage_recompute_interval = getattr(args, "coverage_recompute_interval", 1200.0)
    validate_schedule_interval = getattr(args, "validate_schedule_interval", 1800.0)
    validate_batch = getattr(args, "validate_batch", scheduler_mod.DEFAULT_VALIDATE_BATCH)

    try:
        while True:
            # Periodic re-scheduling pass (SPEC "Refresh targets"): runs
            # inside this same loop rather than a second process, at its own
            # cadence independent of the job-claim poll_interval.
            now_monotonic = time.monotonic()
            if reschedule_interval > 0 and now_monotonic - last_schedule_pass >= reschedule_interval:
                db_sched = get_session()
                try:
                    enqueued = scheduler_mod.run_schedule_pass(db_sched)
                    db_sched.commit()
                    if enqueued:
                        print(f"worker {worker_id}: schedule-refresh enqueued {sorted(enqueued)}")
                except Exception:
                    db_sched.rollback()
                    traceback.print_exc()
                finally:
                    db_sched.close()
                last_schedule_pass = now_monotonic

            # Periodic full-text queue top-up: when the fetch_text backlog
            # falls below the floor, enqueue another batch so extraction
            # progresses across the whole corpus over time.
            if (
                fulltext_topup_interval > 0
                and now_monotonic - last_fulltext_topup >= fulltext_topup_interval
            ):
                db_ft = get_session()
                try:
                    queued = queue_mod.count_claimable(db_ft, fulltext_mod.FETCH_TEXT_KIND)
                    if queued < fulltext_topup_floor:
                        added = fulltext_mod.enqueue_fulltext_jobs(
                            db_ft, limit=fulltext_topup_batch
                        )
                        db_ft.commit()
                        if added:
                            print(f"worker {worker_id}: fulltext top-up enqueued {added}")
                    else:
                        db_ft.commit()
                except Exception:
                    db_ft.rollback()
                    traceback.print_exc()
                finally:
                    db_ft.close()
                last_fulltext_topup = now_monotonic

            # Periodic coverage recompute: refreshes bill_count/full_text_count
            # for every jurisdiction_coverage row from the actual bills/
            # bill_documents tables and advances non-terminal rows toward
            # FULL_TEXT_SEARCHABLE as extracted_text lands from the fulltext
            # crawl. Cheap, set-based, no external calls -- never touches a
            # row already at GREEN/DEGRADED/BLOCKED/VALIDATING (see
            # coverage._TERMINAL_STATES_NOT_AUTO_ADVANCED).
            if (
                coverage_recompute_interval > 0
                and now_monotonic - last_coverage_recompute >= coverage_recompute_interval
            ):
                db_cov = get_session()
                try:
                    rows = coverage_mod.recompute_all_coverage(db_cov)
                    db_cov.commit()
                    by_status: dict[str, int] = {}
                    for row in rows:
                        by_status[row.status] = by_status.get(row.status, 0) + 1
                    print(
                        f"worker {worker_id}: coverage-recompute {len(rows)} row(s) -> "
                        f"{dict(sorted(by_status.items()))}"
                    )
                except Exception:
                    db_cov.rollback()
                    traceback.print_exc()
                finally:
                    db_cov.close()
                last_coverage_recompute = now_monotonic

            # Periodic validation scheduler: enqueues a small batch of
            # `validate` jobs, prioritizing jurisdictions ready to be
            # promoted to GREEN (FULL_TEXT_SEARCHABLE) or overdue for a
            # DEGRADED recheck (see scheduler.enqueue_validation_jobs).
            # Actual validation work (external HTTP to the search API + each
            # jurisdiction's official site) happens when the queued job is
            # claimed + dispatched below, paced by the queue's normal serial
            # claim like any other job kind.
            if (
                validate_schedule_interval > 0
                and now_monotonic - last_validate_schedule >= validate_schedule_interval
            ):
                db_val = get_session()
                try:
                    queued = scheduler_mod.enqueue_validation_jobs(db_val, validate_batch)
                    db_val.commit()
                    if queued:
                        print(f"worker {worker_id}: validate-schedule enqueued {sorted(queued)}")
                except Exception:
                    db_val.rollback()
                    traceback.print_exc()
                finally:
                    db_val.close()
                last_validate_schedule = now_monotonic

            db = get_session()
            try:
                job = queue_mod.claim_job(
                    db, worker_id, exclude_kinds=CRAWL_WORKER_EXCLUDED_KINDS
                )
                if job is None:
                    db.commit()
                    time.sleep(poll_interval)
                    continue
                print(f"worker {worker_id}: claimed job {job.id} kind={job.kind}", flush=True)
                # Read the post-claim attempt count NOW, while this session's
                # uncommitted increment is still live in memory. Both failure
                # handlers below call db.rollback(), which discards that
                # increment AND expires `job`, so a later `job.attempts` would
                # silently re-read the pre-claim value from the database and
                # hand fail_job a count that never grows -- the job then
                # requeues forever and can never reach the dead-letter cap.
                # That is exactly what deadlocked the crawl on 2026-07-25: 1,215
                # permanently-failing jobs all sitting at attempts=0, retried
                # every 30s indefinitely, holding `queued` above the top-up
                # floor so no new work could ever be enqueued either.
                claimed_attempts = job.attempts
                # Total function on purpose -- see _fetch_text_document_id.
                # Anything that can RAISE here raises OUTSIDE the try below,
                # escapes the worker loop, and leaves the job stuck `running`
                # forever, which is the exact starvation shape the comment
                # above documents.
                claimed_document_id = _fetch_text_document_id(job)
                try:
                    # Dispatch by kind is intentionally minimal here; concrete
                    # job kinds (bootstrap/api-sync/recompute-coverage) are
                    # invoked the same way the CLI subcommands above do, keyed
                    # off job.payload. Left as a clear extension point rather
                    # than duplicating the subcommand bodies.
                    if job.kind == "recompute-coverage":
                        coverage_mod.recompute_and_write(db, DEFAULT_COVERAGE_OUTPUT)
                    elif job.kind == "seed-registry":
                        registry_mod.seed_registry(db, DEFAULT_REGISTRY_PATH)
                    elif job.kind == fulltext_mod.FETCH_TEXT_KIND:
                        result = fulltext_mod.process_fetch_text_job(
                            db,
                            job.payload.get("document_id"),
                            fetcher=fulltext_fetcher,
                            rawstore=rawstore,
                        )
                        print(
                            f"worker {worker_id}: fetch_text {result.document_id} "
                            f"status={result.status} chars={result.extracted_chars}"
                        )
                    elif job.kind == scheduler_mod.API_SYNC_KIND:
                        result = api_sync_mod.run_api_sync_job(db, job.payload.get("state"))
                        print(
                            f"worker {worker_id}: api_sync {result.state} "
                            f"created={result.bills_created} updated={result.bills_updated} "
                            f"unchanged={result.bills_unchanged}"
                        )
                    elif job.kind == scheduler_mod.VALIDATE_KIND:
                        abbr = job.payload.get("jurisdiction")
                        jurisdiction = db.execute(
                            select(Jurisdiction).where(Jurisdiction.abbreviation == abbr)
                        ).scalar_one_or_none()
                        if jurisdiction is None:
                            raise ValueError(f"validate job: no jurisdiction row for {abbr!r}")
                        sample_size = job.payload.get("sample") or validation_mod.DEFAULT_SAMPLE_SIZE
                        summary, _run = validation_mod.validate_and_record(
                            db, jurisdiction, sample_size=sample_size
                        )
                        rate = f"{summary.pass_rate:.0%}" if summary.pass_rate is not None else "n/a"
                        print(
                            f"worker {worker_id}: validate {abbr} sampled={len(summary.bills)} "
                            f"checks_run={summary.checks_run} checks_failed={summary.checks_failed} "
                            f"pass_rate={rate}"
                        )
                    else:
                        raise ValueError(f"unknown job kind: {job.kind!r}")
                    queue_mod.complete_job(db, job)
                    db.commit()
                except fulltext_mod.UnfetchableDocument as exc:
                    # `exc.status` distinguishes PERMANENT per-document
                    # outcomes (robots disallow, empty URL, malformed URL,
                    # unsupported redirect scheme, missing row) -- retrying
                    # won't change the answer, so dead-letter immediately --
                    # from `too_many_redirects`, which IS transient (the
                    # target site's redirect chain today doesn't guarantee
                    # its redirect chain tomorrow) and must get the normal
                    # fail/backoff/retry treatment instead of being
                    # permanently dead-lettered on one bad hop.
                    #
                    # db.rollback() below undoes the bill_documents status
                    # write (license_note='fulltext_status=...') that
                    # process_fetch_text_job already flushed before raising
                    # -- rollback discards uncommitted work in this
                    # transaction, status write included. Left undone, the
                    # document would keep looking "never attempted" and
                    # enqueue_fulltext_jobs would re-enqueue it forever even
                    # though the job itself is dead-lettered/failed. So
                    # re-apply the SAME status in the fresh session used for
                    # dead-lettering/failing, durably, before that commit.
                    db.rollback()
                    record_job_failure(
                        job.id,
                        type(job),
                        claimed_attempts=claimed_attempts,
                        error=str(exc),
                        terminal=exc.status in fulltext_mod.TERMINAL_STATUSES,
                        document_id=exc.document_id,
                        document_status=exc.status,
                        # Document-specific by construction. Matters for the
                        # NON-terminal member of this family,
                        # too_many_redirects: without a budget it is its own
                        # poison loop, retried for ever on a site whose
                        # redirect chain is permanently circular.
                        count_attempt=True,
                    )
                except fulltext_mod.DocumentFetchError as exc:
                    # The document's own host/bytes failed. Retryable, but it
                    # spends one of the document's MAX_FETCH_ATTEMPTS so a
                    # permanently-broken URL stops being re-fetched forever.
                    db.rollback()
                    record_job_failure(
                        job.id,
                        type(job),
                        claimed_attempts=claimed_attempts,
                        error=str(exc),
                        document_id=exc.document_id or claimed_document_id,
                        document_status=exc.status,
                        count_attempt=True,
                    )
                except Exception as exc:  # noqa: BLE001 - job-level failure isolation
                    # Unclassified. It may be the document's fault -- a data
                    # error on ITS row (SQLSTATE 22/23/54, e.g. the
                    # tsvector-too-long write that cost 309 documents their
                    # text) -- or it may be ours: a DB blip, an expired
                    # object-store credential, a bug on this line. Only the
                    # first kind may spend the document's budget. Charging the
                    # second kind would let a one-hour outage mark every
                    # in-flight document permanently_failed, which nothing in
                    # the system would ever retry (see `reset-fetch-attempts`
                    # for the recovery path that must never be needed).
                    db.rollback()
                    document_status, count_attempt = classify_job_failure(
                        claimed_document_id, exc
                    )
                    if claimed_document_id and not count_attempt:
                        print(
                            f"worker {worker_id}: fetch_text {claimed_document_id} failed with an "
                            f"UNCLASSIFIED error -- recording {document_status} WITHOUT charging "
                            f"the document a fetch attempt: {exc!r}",
                            flush=True,
                        )
                    record_job_failure(
                        job.id,
                        type(job),
                        claimed_attempts=claimed_attempts,
                        error=str(exc),
                        document_id=claimed_document_id,
                        document_status=document_status,
                        count_attempt=count_attempt,
                    )
            finally:
                db.close()
    except KeyboardInterrupt:
        print(f"worker {worker_id}: stopping")
        return 0


def classify_job_failure(
    document_id: str | None, exc: BaseException
) -> tuple[str | None, bool]:
    """What does this failed fetch_text job cost the document?

    Returns the `fulltext_status` to record and whether it may spend one of the
    document's MAX_FETCH_ATTEMPTS. Extracted from cmd_worker's generic handler
    so the decision is unit-testable rather than a conditional buried in an
    except block -- getting it wrong is not a cosmetic bug: charging an
    infrastructure outage to the documents that happened to be in flight walks
    them to `permanently_failed`, which is terminal, so nothing in the system
    would ever offer them again.
    """
    if not document_id:
        return (None, False)
    if fulltext_mod.is_document_specific_failure(exc):
        # DocumentFetchError/UnfetchableDocument carry the specific condition;
        # a data error on this row (SQLSTATE 22/23/54) does not, and is a
        # fetch_error for reporting purposes.
        status = getattr(exc, "status", None) or fulltext_mod.STATUS_FETCH_ERROR
        return (status, True)
    return (fulltext_mod.STATUS_WORKER_ERROR, False)


def _fetch_text_document_id(job) -> str | None:
    """The document a claimed job is about, or None -- never raises.

    `job.payload` is written by enqueue_fulltext_jobs and is always a dict
    today, but this runs OUTSIDE the worker's per-job try/except: a NULL or
    non-dict payload on one hand-inserted row would raise AttributeError with
    no handler, escape the worker loop, and leave that job `running` forever,
    holding a queue slot above the top-up floor so no new work could be
    enqueued either. Cheaper to be total than to re-live that.
    """
    if job.kind != fulltext_mod.FETCH_TEXT_KIND:
        return None
    payload = job.payload
    if not isinstance(payload, dict):
        return None
    document_id = payload.get("document_id")
    return str(document_id) if document_id else None


def record_job_failure(
    job_id,
    job_cls,
    *,
    claimed_attempts: int,
    error: str,
    terminal: bool = False,
    document_id: str | None = None,
    document_status: str | None = None,
    count_attempt: bool = False,
    session_factory=get_session,
) -> None:
    """Record a claimed job's failure in a FRESH session, after the claiming
    transaction has already been rolled back.

    `claimed_attempts` MUST be the count read off the job after `claim_job`
    incremented it and before that rollback. The rollback both discards the
    uncommitted increment and expires the instance, so re-reading `attempts`
    here would return the pre-claim value -- the job would then requeue with an
    attempt count that never grows and could never reach `fail_job`'s
    dead-letter cap.

    That is precisely what deadlocked the crawl on 2026-07-25: 1,215
    permanently-failing jobs all pinned at attempts=0, retried every 30
    seconds forever, which also held `queued` above the top-up floor so no new
    work could be enqueued either. Throughput was exactly zero for an hour
    while the service reported healthy and the logs showed it busily claiming
    jobs. Carrying the count across the session boundary is what makes
    permanent failures terminal.

    For an `UnfetchableDocument`, the caller also passes the document's status
    so the `license_note` write that the rollback discarded is re-applied
    durably here -- otherwise the document keeps looking never-attempted and
    `enqueue_fulltext_jobs` re-enqueues it forever despite the dead-letter.

    `count_attempt` decides whether this failure spends one of the document's
    MAX_FETCH_ATTEMPTS. It defaults to FALSE so that a future caller has to opt
    in deliberately: the budget is what makes a document permanently_failed
    (terminal, never re-enqueued), so charging the wrong failure to it -- an
    outage, a bug in this worker -- silently drops documents from the corpus.
    Only failures the fetcher itself classified as the document's own (see
    fulltext.is_document_specific_failure) pass True.
    """
    db = session_factory()
    try:
        if document_id and document_status:
            # FOR UPDATE: two workers can be recording failures for the same
            # document at the same moment, and a plain read-modify-write loses
            # one of the increments. That can only DELAY the cap, never break
            # it, but the lock is one row held for microseconds -- cheaper than
            # reasoning about it again later.
            document = db.get(BillDocument, document_id, with_for_update=True)
            if document is not None:
                status = document_status
                created_at = document.created_at
                if created_at is not None and created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if (
                    count_attempt
                    and status not in fulltext_mod.TERMINAL_STATUSES
                    and (
                        status not in fulltext_mod.NO_FETCH_ATTEMPT_CHARGE_STATUSES
                        # The 180-day created_at anchor is an accepted approximation for this grace period.
                        # A missing anchor cannot establish that the document is
                        # still inside grace, so conservatively charge it.
                        or created_at is None
                        or created_at
                        <= datetime.now(timezone.utc)
                        - timedelta(days=fulltext_mod.MA_DOCKET_NO_BILL_NUMBER_GRACE_DAYS)
                    )
                ):
                    document.fetch_attempts = (document.fetch_attempts or 0) + 1
                    if document.fetch_attempts >= fulltext_mod.MAX_FETCH_ATTEMPTS:
                        status = fulltext_mod.STATUS_PERMANENTLY_FAILED
                        terminal = True
                document.license_note = f"fulltext_status={status}"
        job = db.get(job_cls, job_id)
        if job is None:
            # Commit anyway: the document write above is the durable record of
            # this failure and must not be discarded just because the job row
            # vanished (the 84k dead fetch_text rows are slated for a purge, so
            # a cleanup racing a failure record is a real sequence). Without
            # this the increment is rolled back on close() and the document
            # keeps its poison-loop eligibility forever.
            db.commit()
            return
        job.attempts = claimed_attempts
        if terminal:
            queue_mod.dead_letter_job(db, job, error)
        else:
            queue_mod.fail_job(db, job, error)
        db.commit()
    finally:
        db.close()


def _next_utc_midnight_with_jitter(*, now: datetime | None = None) -> datetime:
    """Next UTC midnight plus up to 5 minutes of jitter, so a whole cycle's
    worth of budget-deferred jobs don't all wake and re-claim in the same
    instant."""
    now = now or datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(seconds=random.randint(0, 300))


def defer_job_for_budget(
    job_id,
    job_cls,
    *,
    claimed_attempts: int,
    run_after: datetime,
    session_factory=get_session,
) -> bool:
    """Requeue a claimed job that hit OpenStatesDailyBudgetExceeded without
    burning an attempt.

    Budget exhaustion is an outage of our own making, not the job's fault --
    the invariant is that it must never contribute to dead-lettering. Runs
    in a FRESH session for the same reason `record_job_failure` does: the
    claiming transaction already rolled back and expired that instance.
    `claimed_attempts` is `claim_job`'s post-increment count; the row's
    rollback already undid that increment, so `claimed_attempts - 1` is
    what `attempts` should read on the still-unclaimed row.

    That rollback also means there is a gap, between this job's claiming
    transaction rolling back the row to `queued` and this function's fresh
    transaction locking it, during which a second concurrent sync-worker can
    claim (or even complete) the same row. Writing unconditionally here
    would clobber whatever that worker did -- reset its `running`/`done`
    status back to `queued`, erase its lock fields, restore a stale
    `attempts`. So the row is fetched WITH a row lock and the deferral is
    only applied if the row still looks exactly like the unclaimed,
    just-rolled-back state this job left it in; otherwise it is left
    completely untouched.

    Returns True if the row was deferred, False if it was left alone
    because it no longer matched that expected state (already re-claimed,
    or gone).
    """
    db = session_factory()
    try:
        job = db.get(job_cls, job_id, with_for_update=True)
        if job is None:
            db.commit()
            return False
        expected_attempts = max(0, claimed_attempts - 1)
        if job.status != "queued" or job.attempts != expected_attempts or job.locked_by is not None:
            # Someone else already claimed (or completed) this row in the
            # gap -- leave it alone.
            db.rollback()
            return False
        job.run_after = run_after
        db.commit()
        return True
    finally:
        db.close()


def recompute_status_for_bills(
    db, bill_ids: list, counts: dict[str, int] | None = None, *, stamp: bool = True
) -> tuple[int, int]:
    """Re-derive `bills.status` for a specific set of bills. Caller commits.

    Returns (changed, cleared). Shared by the full `recompute-status` backfill
    and the sync worker's per-cycle refresh so the two can never drift: a
    status written by one and a status written by the other have to mean the
    same thing.

    `stamp` controls whether `updated_at` moves, and the two callers genuinely
    want opposite answers:

    * The sync worker stamps. A bill whose status moved because new actions
      landed HAS changed, and consumers must see it in /changes.
    * The full backfill does not. It re-derives all 209k bills, so any change
      to the derivation logic itself would otherwise stamp tens of thousands
      of rows at once and publish them as a wave of "changes" that no consumer
      can tell apart from real legislative movement -- one maintenance re-run
      would drown every watchlist on the platform. The bills did not move; our
      reading of them improved.

    That distinction is exactly what a real change-log table would carry as an
    event kind. This flag is the honest stand-in until there is one.
    """
    if not bill_ids:
        return (0, 0)
    # The session's end date is part of the derivation, not decoration: a bill
    # short of the governor's desk in an adjourned session is dead however
    # alive its own actions look. Joined here so it costs one query for the
    # whole chunk. See status.apply_session_outcome.
    current = {}
    session_end = {}
    session_active = {}
    bill_session = {}
    bill_jurisdiction: dict = {}
    bill_identifier_norm: dict = {}
    for r in db.execute(
        select(
            Bill.id,
            Bill.status,
            Bill.session_id,
            Bill.jurisdiction_id,
            Bill.identifier_norm,
            SessionModel.end_date,
            SessionModel.active,
        )
        .join(SessionModel, SessionModel.id == Bill.session_id)
        .where(Bill.id.in_(bill_ids))
    ).all():
        current[r.id] = r.status
        session_end[r.id] = r.end_date
        session_active[r.id] = bool(r.active)
        bill_jurisdiction[r.id] = r.jurisdiction_id
        bill_identifier_norm[r.id] = r.identifier_norm
        bill_session[r.id] = r.session_id
    # Session-level recent activity, for the case where the source calls a
    # session active but its predicted adjournment has passed. Deliberately
    # SESSION level, not bill level: a single bill can sit untouched for months
    # inside a chamber that is filing paper daily, so per-bill silence says
    # nothing about whether the legislature is sitting.
    #
    # Future-dated actions are excluded. South Carolina carries an action dated
    # 2026-09-01 -- a month ahead of today -- and letting a scheduled or
    # mis-keyed future date stand in as proof of current activity would make
    # the corroboration meaningless.
    recent_cutoff = datetime.now(timezone.utc).date() - timedelta(
        days=SESSION_ACTIVITY_WINDOW_DAYS
    )
    today_date = datetime.now(timezone.utc).date()
    session_ids = {sid for sid in bill_session.values() if sid is not None}
    sessions_with_recent_activity: set = set()
    if session_ids:
        sessions_with_recent_activity = {
            row[0]
            for row in db.execute(
                select(Bill.session_id)
                .join(BillAction, BillAction.bill_id == Bill.id)
                .where(
                    Bill.session_id.in_(session_ids),
                    BillAction.action_date >= recent_cutoff,
                    BillAction.action_date <= today_date,
                    or_(
                        *[
                            BillAction.classification.ilike(pat)
                            for pat in CHAMBER_ACTIVITY_PATTERNS
                        ]
                    ),
                )
                .distinct()
            ).all()
        }

    actions_by_bill: dict[object, list[status_mod.ActionRow]] = {bid: [] for bid in bill_ids}
    for a in db.execute(
        select(
            BillAction.bill_id,
            BillAction.action_date,
            BillAction.classification,
            BillAction.description,
            BillAction.organization_id,
        ).where(BillAction.bill_id.in_(bill_ids))
    ).all():
        actions_by_bill[a.bill_id].append(
            status_mod.ActionRow(
                action_date=a.action_date,
                classification=a.classification,
                description=a.description,
                organization_id=a.organization_id,
            )
        )

    # Pass 1: derive every bill in the chunk before propagating anything, so
    # a substituted bill resolved below sees its survivor's FRESH status
    # rather than whatever was stored before this run.
    derived_status: dict = {}
    for bid in bill_ids:
        derived_status[bid] = status_mod.apply_session_outcome(
            status_mod.derive_status(actions_by_bill[bid]),
            session_end.get(bid),
            session_active=session_active.get(bid, False),
            session_has_recent_activity=(
                bill_session.get(bid) in sessions_with_recent_activity
            ),
        )

    # Pass 2 -- substitution propagation (Jaya gap #1, R3). A substituted
    # print (e.g. an NY bill carrying "SUBSTITUTED BY A10008C") is not itself
    # the bill that moves; the identified survivor is. If the survivor
    # reached an OUTCOME, the substituted print should read as that outcome
    # too, not sit at IN_COMMITTEE/SUBSTITUTED forever while its survivor is
    # chaptered. related_bills is consulted the same way: a relation whose
    # type names a substitution, pointing away from this bill, is the same
    # signal as the text form.
    survivor_bill_id: dict = {}
    survivor_identifier: dict = {}
    for bid in bill_ids:
        for action in actions_by_bill[bid]:
            target = status_mod.substitution_target(action.description)
            if target:
                survivor_identifier[bid] = target  # last one wins; actions
                # are not guaranteed date-ordered from the query, but a bill
                # is only ever substituted once in practice.
    related_rows = db.execute(
        select(
            RelatedBill.bill_id,
            RelatedBill.related_bill_id,
            RelatedBill.related_identifier,
        ).where(
            RelatedBill.bill_id.in_(bill_ids),
            RelatedBill.relation_type.ilike("%substitut%"),
        )
    ).all()
    for row in related_rows:
        if row.related_bill_id is not None:
            survivor_bill_id[row.bill_id] = row.related_bill_id
        elif row.related_identifier:
            try:
                survivor_identifier[row.bill_id] = normalize_bill_number(
                    row.related_identifier
                )
            except ValueError:
                pass

    substitution_candidates = set(survivor_identifier) | set(survivor_bill_id)
    if substitution_candidates:
        # Resolve identifier-only survivors to a bill id within the same
        # jurisdiction + session. Whitespace/hyphens/case are already folded
        # by normalize_bill_number on both sides, since identifier_norm is
        # produced by the same function at ingest time.
        needs_lookup = {
            bid: ident
            for bid, ident in survivor_identifier.items()
            if bid not in survivor_bill_id
        }
        if needs_lookup:
            # NY-only: the print-suffix stripped candidate only ever applies
            # there (FL "HB 1A" / CA "AB 1X" use the same trailing-letter
            # shape as part of identity). One cheap query for the whole
            # chunk's jurisdictions, not per bill.
            jurisdiction_ids = {
                bill_jurisdiction.get(bid) for bid in needs_lookup if bill_jurisdiction.get(bid)
            }
            ny_jurisdiction_ids = {
                row.id
                for row in db.execute(
                    select(Jurisdiction.id).where(
                        Jurisdiction.id.in_(jurisdiction_ids),
                        func.upper(Jurisdiction.abbreviation) == "NY",
                    )
                ).all()
            }
            candidates_by_bid = {
                bid: status_mod.substitution_lookup_candidates(
                    ident, print_suffix=bill_jurisdiction.get(bid) in ny_jurisdiction_ids
                )
                for bid, ident in needs_lookup.items()
            }

            in_chunk_by_key = {
                (bill_jurisdiction.get(other), bill_session.get(other), bill_identifier_norm.get(other)): other
                for other in bill_ids
            }

            # Resolve rank-by-rank (exact identifier before the NY stripped
            # fallback), each rank checked against the chunk THEN the DB, so
            # an exact-but-out-of-chunk survivor always outranks a stripped
            # in-chunk one -- the winner must not depend on chunk boundaries.
            remaining = dict(needs_lookup)
            max_rank = max((len(c) for c in candidates_by_bid.values()), default=0)
            for rank in range(max_rank):
                rank_bids = [bid for bid in remaining if rank < len(candidates_by_bid[bid])]
                if not rank_bids:
                    continue
                still_needed = []
                for bid in rank_bids:
                    candidate = candidates_by_bid[bid][rank]
                    key = (bill_jurisdiction.get(bid), bill_session.get(bid), candidate)
                    match = in_chunk_by_key.get(key)
                    if match is not None and match != bid:
                        survivor_bill_id[bid] = match
                        remaining.pop(bid, None)
                    else:
                        still_needed.append(bid)
                if not still_needed:
                    continue
                # One batched query for every bill still unresolved at this
                # rank, then match rows back per bill by (jurisdiction,
                # session, identifier).
                rank_candidates = {candidates_by_bid[bid][rank] for bid in still_needed}
                rows = db.execute(
                    select(
                        Bill.id,
                        Bill.jurisdiction_id,
                        Bill.session_id,
                        Bill.identifier_norm,
                    ).where(Bill.identifier_norm.in_(rank_candidates))
                ).all()
                rows_by_key: dict = {}
                for row in rows:
                    rows_by_key.setdefault(
                        (row.jurisdiction_id, row.session_id, row.identifier_norm), []
                    ).append(row)
                for bid in still_needed:
                    candidate = candidates_by_bid[bid][rank]
                    key = (bill_jurisdiction.get(bid), bill_session.get(bid), candidate)
                    match = next(
                        (row for row in rows_by_key.get(key, ()) if row.id != bid), None
                    )
                    if match is not None:
                        survivor_bill_id[bid] = match.id
                        remaining.pop(bid, None)

        for bid, sid in survivor_bill_id.items():
            if bid not in bill_ids:
                continue
            if sid in derived_status:
                survivor_status = derived_status[sid]
            else:
                row = db.execute(select(Bill.status).where(Bill.id == sid)).first()
                survivor_status = row.status if row else None
            if survivor_status is not None and survivor_status in status_mod.TERMINAL_STATUSES:
                # No status_note/similar column exists on `bills` (checked
                # models.py) -- inherit the status only, per spec R3.
                derived_status[bid] = survivor_status
            else:
                derived_status[bid] = status_mod.SUBSTITUTED

    updates = []
    cleared = 0
    for bid in bill_ids:
        derived = derived_status[bid]
        if counts is not None and derived is not None:
            counts[derived] = counts.get(derived, 0) + 1
        if derived != current.get(bid):
            updates.append({"b_id": bid, "b_status": derived})
            if derived is None:
                cleared += 1
            if stamp:
                # Carries the transition itself, so a consumer can act on
                # "in_committee -> enacted" without having cached the old
                # value. Suppressed for wholesale backfills for the same
                # reason the timestamp is: see this function's docstring.
                events_mod.record_event(
                    db,
                    bid,
                    events_mod.STATUS,
                    f"{current.get(bid) or 'unknown'} -> {derived or 'unknown'}",
                )
    if updates:
        # Bump updated_at on exactly the rows whose status MOVED. This is a raw
        # UPDATE, so SQLAlchemy's client-side onupdate never fires -- without
        # setting it here a bill could go introduced -> enacted while its
        # updated_at stayed at the bulk-load timestamp, and /changes (which is
        # a range scan over updated_at) would never report the single event
        # consumers most want to hear about. Only changed rows are written, so
        # this still does not churn the column on a no-op re-run.
        db.execute(
            text(
                "UPDATE bills SET status = :b_status, updated_at = now() WHERE id = :b_id"
                if stamp
                else "UPDATE bills SET status = :b_status WHERE id = :b_id"
            ),
            updates,
        )
    return (len(updates), cleared)


def cmd_backfill_session_dates(args: argparse.Namespace) -> int:
    """Fill `sessions.start_date`/`end_date` from the Open States v3 API.

    The end date decides whether a bill still has a chance: anything short of
    the governor's desk in an adjourned session is dead no matter how alive its
    own action record looks (see status.apply_session_outcome). 18 of 77
    sessions had no end date at all, which left every bill in them
    unresolvable -- Georgia's biennium had adjourned on 2026-04-02 and 4,502
    Georgia bills were still being reported as live.

    Also corrects `active`, which is set once at seed time and never revisited:
    six sessions were flagged active with an end date already in the past.

    Read from upstream rather than curated by hand because sine die dates move,
    special sessions appear mid-year, and a hardcoded table would be wrong
    within a month and wrong silently.
    """
    db = get_session()
    try:
        jurisdictions = db.execute(
            select(Jurisdiction).order_by(Jurisdiction.abbreviation)
        ).scalars().all()
        stats = refresh_session_dates(db, jurisdictions, delay=args.delay, verbose=True)
        if args.dry_run:
            db.rollback()
            print(
                f"backfill-session-dates: DRY RUN -- {stats['checked']} session(s) checked, "
                f"{stats['filled']} would gain an end_date, {stats['corrected']} corrected, "
                f"{stats['deactivated']} would be deactivated, {stats['failed']} failed"
            )
            return 0
        db.commit()
        print(
            f"backfill-session-dates: {stats['checked']} session(s) checked, "
            f"{stats['filled']} end_date(s) filled, {stats['corrected']} corrected, "
            f"{stats['deactivated']} deactivated, {stats['failed']} failed"
        )
        # A run where nothing succeeded is a FAILED run, not a quiet no-op. The
        # first paced attempt failed all 35 calls against an exhausted daily
        # quota, changed nothing, and still looked like it had completed --
        # which is the shape of bug that gets mistaken for "already done".
        if stats["failed"] and not (stats["filled"] or stats["corrected"]):
            print(
                "backfill-session-dates: FAILED -- every upstream call errored and "
                "nothing was written. The v3 free tier is 250 requests/day AND "
                "~6/minute, shared with the bill sync; a same-day sync run can "
                "exhaust it. Check for 429s above and retry after the quota resets.",
                flush=True,
            )
            return 1
        return 0
    except Exception:
        db.rollback()
        traceback.print_exc()
        return 1
    finally:
        db.close()


def refresh_session_dates(
    db, jurisdictions, *, delay: float = 11.0, verbose: bool = False
) -> dict:
    """Pull authoritative session start/end dates for `jurisdictions`.

    Caller commits. Shared by the one-shot backfill command and the sync
    worker's per-cycle top-up so the two cannot drift.
    """
    from billcommons_ingest.openstates_api import OpenStatesClient

    client = OpenStatesClient()
    today = datetime.now(timezone.utc).date()
    checked = filled = corrected = deactivated = failed = 0

    for jurisdiction in jurisdictions:
        ocd_id = jurisdiction.openstates_id or _ocd_jurisdiction_id(jurisdiction)
        if not ocd_id:
            if verbose:
                print(f"  {jurisdiction.abbreviation}: no resolvable id, skipped", flush=True)
            continue
        # Paced deliberately. The v3 free tier is ~6 req/min and 250/day, and
        # this makes one call per jurisdiction, so firing all 51 back to back
        # exhausts the client's 429 backoff and the sweep dies partway --
        # leaving exactly the half-corrected session table it exists to
        # prevent. Slower and complete beats faster and partial.
        if checked or failed:
            time.sleep(delay)
        try:
            upstream = client.get_legislative_sessions(ocd_id)
        except Exception as exc:  # noqa: BLE001 - one bad state must not stop the sweep
            failed += 1
            print(f"  {jurisdiction.abbreviation}: FAILED {exc}", flush=True)
            continue

        by_identifier = {s.get("identifier"): s for s in upstream if s.get("identifier")}
        rows = db.execute(
            select(SessionModel).where(SessionModel.jurisdiction_id == jurisdiction.id)
        ).scalars().all()
        for row in rows:
            checked += 1
            match = by_identifier.get(row.identifier)
            if match is None:
                continue
            end_date = _parse_iso_date(match.get("end_date"))
            start_date = _parse_iso_date(match.get("start_date"))
            if end_date is not None and row.end_date != end_date:
                if row.end_date is None:
                    filled += 1
                else:
                    corrected += 1
                if verbose:
                    print(
                        f"  {jurisdiction.abbreviation} {row.identifier!r}: "
                        f"end_date {row.end_date} -> {end_date}",
                        flush=True,
                    )
                row.end_date = end_date
            if start_date is not None and row.start_date != start_date:
                row.start_date = start_date
            # `active` is a derived fact, not an independent one. Left stale it
            # silently contradicts the dates sitting beside it -- six sessions
            # were flagged active with an end date already in the past.
            should_be_active = row.end_date is None or row.end_date >= today
            if row.active != should_be_active:
                row.active = should_be_active
                if not should_be_active:
                    deactivated += 1

    return {
        "checked": checked,
        "filled": filled,
        "corrected": corrected,
        "deactivated": deactivated,
        "failed": failed,
    }


def _ocd_jurisdiction_id(jurisdiction) -> str | None:
    """Build the OCD jurisdiction id Open States addresses by.

    `jurisdictions.openstates_id` is null for all 51 rows (the bulk-CSV
    bootstrap never populated it), so deriving it from the abbreviation is what
    makes this command work at all. DC is a district, not a state -- the one
    case where the pattern differs.
    """
    abbreviation = (jurisdiction.abbreviation or "").lower()
    if not abbreviation:
        return None
    kind = "district" if abbreviation == "dc" else "state"
    return f"ocd-jurisdiction/country:us/{kind}:{abbreviation}/government"


def _parse_iso_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def cmd_recompute_status(args: argparse.Namespace) -> int:
    """Backfill/refresh `bills.status` from each bill's action record.

    Set-based SQL cannot do this: 42% of actions carry no classification, so
    the derivation needs status.py's text fallback, in Python. Bills are walked
    in id-ordered chunks and each chunk's actions are fetched in ONE query --
    209k bills against 1.6M actions is fine that way and pathological
    per-bill.

    Only rows whose status actually CHANGES are written, so a re-run over an
    already-correct corpus costs reads and no writes (and does not churn
    `updated_at`, which consumers use to detect real movement).

    `--jurisdiction` scopes the scan to one state (by abbreviation, e.g. NY)
    -- for running a fix like the R1-R3 status-carryover logic against the
    jurisdiction it targets first, before the full 209k-bill backfill.
    """
    db = get_session()
    processed = 0
    changed = 0
    cleared = 0
    counts: dict[str, int] = {}
    jurisdiction_id = None
    if getattr(args, "jurisdiction", None):
        jurisdiction_row = db.execute(
            select(Jurisdiction.id).where(
                Jurisdiction.abbreviation.ilike(args.jurisdiction)
            )
        ).first()
        if jurisdiction_row is None:
            print(f"recompute-status: unknown jurisdiction {args.jurisdiction!r}")
            return 1
        jurisdiction_id = jurisdiction_row.id
    try:
        last_id = None
        while True:
            if args.limit and processed >= args.limit:
                break
            chunk = args.chunk
            if args.limit:
                chunk = min(chunk, args.limit - processed)
            q = select(Bill.id, Bill.status).order_by(Bill.id).limit(chunk)
            if jurisdiction_id is not None:
                q = q.where(Bill.jurisdiction_id == jurisdiction_id)
            if last_id is not None:
                q = q.where(Bill.id > last_id)
            rows = db.execute(q).all()
            if not rows:
                break
            last_id = rows[-1].id
            bill_ids = [r.id for r in rows]

            # stamp=False: see recompute_status_for_bills. A wholesale
            # re-derivation is a maintenance pass, not 209k bills moving.
            chunk_changed, chunk_cleared = recompute_status_for_bills(
                db, bill_ids, counts, stamp=False
            )
            changed += chunk_changed
            cleared += chunk_cleared
            db.commit()
            processed += len(rows)
            print(
                f"recompute-status: {processed:,} bills scanned, {changed:,} updated",
                flush=True,
            )

        print(f"recompute-status: DONE scanned={processed:,} updated={changed:,} cleared={cleared:,}")
        for name in status_mod.ALL_STATUSES:
            if counts.get(name):
                print(f"  {name:20} {counts[name]:>8,}")
        undetermined = processed - sum(counts.values())
        print(f"  {'(undetermined)':20} {undetermined:>8,}")
        return 0
    except Exception:
        db.rollback()
        traceback.print_exc()
        return 1
    finally:
        db.close()


def cmd_sync_worker(args: argparse.Namespace) -> int:
    """Dedicated, long-running METADATA-refresh loop -- the only thing in this
    system that claims `api_sync` jobs.

    Why it exists: the crawl worker refuses api_sync outright
    (CRAWL_WORKER_EXCLUDED_KINDS) because a single api_sync holds a DB session
    open across minutes of rate-limited HTTP, which starved fetch_text and
    froze the crawl twice. With no other consumer, api_sync jobs simply piled
    up -- 75 sat `queued` and unclaimable while EVERY ONE of the 209,612 bills
    kept `updated_at` from the original bulk load. The corpus was a frozen
    snapshot: bill text kept arriving, but no bill's STATUS, actions or votes
    ever changed again. A consumer polling us for "what moved today" would have
    been told "nothing" forever, which is the worst failure a monitoring source
    can have -- silence that looks like an answer.

    Each cycle:
      1. `scheduler.run_schedule_pass` -- enqueue api_sync for jurisdictions
         whose cadence tier says they are due (it holds its own advisory lock,
         so running this alongside anything else is safe).
      2. Drain queued api_sync jobs, one short-lived session each, up to
         `--max-jobs` so one pathological state cannot spin a cycle forever.
      3. Re-derive `bills.status` for every bill the cycle touched, so the
         status field tracks the actions the sync just landed instead of
         freezing at whatever the initial backfill computed.
      4. Recompute coverage, because api_sync creates bills and the counts
         behind every coverage row would otherwise lag a full crawl cadence.
      5. Sleep `--interval` (default 24h -- nightly is enough for consumers
         doing a daily sync, and keeps us well inside the upstream API's
         rate limits).

    Failure isolation matches the crawl worker exactly: `claimed_attempts` is
    read while the claim's increment is still live, and failures are recorded
    in a FRESH session via `record_job_failure`, so a permanently-failing state
    actually reaches the dead-letter cap instead of retrying forever.
    """
    worker_id = args.worker_id or f"{socket.gethostname()}-sync-worker"
    interval = args.interval
    max_jobs = args.max_jobs
    print(
        f"sync-worker {worker_id}: interval={interval}s max_jobs_per_cycle={max_jobs} "
        f"once={args.once} (api_sync ONLY; never claims fetch_text/validate)",
        flush=True,
    )

    # Bills whose status still needs re-deriving, carried across cycles so a
    # failed recompute is retried instead of being lost with the cycle.
    pending_status_bills: set = set()

    # Jurisdictions the session-date top-up has already tried this round.
    # Some sessions have no upstream end date and never will -- a two-year
    # biennium mid-term (IL, WI, DC) genuinely has not ended. Those never
    # leave the needy set, so an unordered LIMIT would hand them the same
    # slots every cycle and starve the ones that CAN be filled. Excluding
    # what was already tried turns the cap into a round-robin; the round
    # resets when everyone has had a turn.
    session_date_checked: set = set()

    try:
        while True:
            touched_this_cycle: set = set()

            # Step 1: enqueue whatever is due.
            db = get_session()
            try:
                enqueued = scheduler_mod.run_schedule_pass(db)
                db.commit()
                print(
                    f"sync-worker {worker_id}: schedule pass enqueued "
                    f"{len(enqueued)} jurisdiction(s): {sorted(enqueued)}",
                    flush=True,
                )
            except Exception:
                db.rollback()
                traceback.print_exc()
            finally:
                db.close()

            # Step 2: drain. One session per job, released before the next --
            # never one long transaction spanning every state's HTTP.
            processed = 0
            failed = 0
            while processed + failed < max_jobs:
                db = get_session()
                try:
                    job = queue_mod.claim_job(
                        db, worker_id, kind=scheduler_mod.API_SYNC_KIND
                    )
                    if job is None:
                        db.commit()
                        break
                    # Read the post-claim count while this session's
                    # uncommitted increment is still live; rollback below
                    # would otherwise expire it and hand back the pre-claim
                    # value, so the job could never reach the dead-letter cap.
                    claimed_attempts = job.attempts
                    job_id = job.id
                    state = job.payload.get("state")
                    try:
                        result = api_sync_mod.run_api_sync_job(db, state)
                        queue_mod.complete_job(db, job)
                        db.commit()
                        # Collected only after the commit succeeds: a rolled-back
                        # sync wrote nothing, so its bills need no recompute.
                        touched_this_cycle |= result.touched_bill_ids
                        processed += 1
                        print(
                            f"sync-worker {worker_id}: api_sync {result.state} "
                            f"created={result.bills_created} updated={result.bills_updated} "
                            f"unchanged={result.bills_unchanged} actions={result.actions} "
                            f"versions={result.versions} documents={result.documents} "
                            f"next_page={result.next_page}",
                            flush=True,
                        )
                        for warning in result.warnings:
                            print(f"api-sync WARNING: {warning}", flush=True)
                    except OpenStatesDailyBudgetExceeded:
                        # Not a job failure -- our own daily brake tripped.
                        # Requeue for tomorrow without touching `attempts`,
                        # so budget exhaustion can never dead-letter a job.
                        db.rollback()
                        run_after = _next_utc_midnight_with_jitter()
                        deferred = defer_job_for_budget(
                            job_id,
                            IngestJob,
                            claimed_attempts=claimed_attempts,
                            run_after=run_after,
                        )
                        if deferred:
                            print(
                                f"api-sync BUDGET: {state} deferred to {run_after}",
                                flush=True,
                            )
                        else:
                            print(
                                f"api-sync BUDGET: {state} defer skipped (row re-claimed concurrently)",
                                flush=True,
                            )
                    except Exception as exc:  # noqa: BLE001 - per-job isolation
                        db.rollback()
                        failed += 1
                        print(
                            f"sync-worker {worker_id}: api_sync {state} FAILED: {exc}",
                            flush=True,
                        )
                        record_job_failure(
                            job_id,
                            IngestJob,
                            claimed_attempts=claimed_attempts,
                            error=str(exc),
                        )
                finally:
                    db.close()

            print(
                f"sync-worker {worker_id}: cycle done -- {processed} synced, {failed} failed",
                flush=True,
            )

            # Step 3: re-derive status for every bill this cycle touched.
            #
            # Without this, `bills.status` is frozen at whatever the one-off
            # backfill computed: the sync happily lands the new actions that
            # move a bill from in_committee to enacted, and the status field
            # -- the thing consumers filter and alert on -- keeps reporting
            # the old value indefinitely. A stale status is worse than a null
            # one, because it reads as a current answer.
            #
            # Works off the bill ids the sync REPORTED touching, never a
            # `updated_at >= cycle_started` window: that window compares a
            # DB-written column to this host's clock, and skew, a run whose
            # retrieved_at predates the cycle stamp, or a forward-only stamp
            # that kept an older value all silently drop bills out of it.
            #
            # Ids that fail to recompute are carried into the next cycle
            # rather than dropped. A window-based scheme cannot do this: once
            # the window moves past a failed bill, nothing ever revisits it,
            # and the bill serves a stale status until some unrelated edit
            # happens to touch it again.
            pending_status_bills |= touched_this_cycle
            if pending_status_bills:
                db = get_session()
                try:
                    batch = sorted(pending_status_bills)
                    changed, cleared = recompute_status_for_bills(db, batch)
                    db.commit()
                    pending_status_bills = set()
                    print(
                        f"sync-worker {worker_id}: status recomputed for "
                        f"{len(batch)} touched bill(s) -- {changed} changed, "
                        f"{cleared} cleared",
                        flush=True,
                    )
                except Exception:
                    db.rollback()
                    traceback.print_exc()
                    print(
                        f"sync-worker {worker_id}: status recompute FAILED for "
                        f"{len(pending_status_bills)} bill(s); retrying next cycle",
                        flush=True,
                    )
                finally:
                    db.close()

            # Step 3a: top up missing session end dates.
            #
            # The adjournment rule below is only as good as this column, and a
            # session with no end date is invisible to it -- Georgia's biennium
            # had adjourned and 4,502 Georgia bills were still being reported
            # as live purely because nobody had recorded the date.
            #
            # Self-terminating: only jurisdictions that still have a session
            # with a NULL end date are queried, so the set shrinks to zero and
            # this becomes a single cheap SELECT. Capped per cycle because the
            # upstream free tier is 250 requests/day shared with the actual
            # sync, and starving that to fill a date would be a bad trade.
            db = get_session()
            try:
                def _needy(exclude: set) -> list:
                    stmt = (
                        select(Jurisdiction)
                        .join(
                            SessionModel,
                            SessionModel.jurisdiction_id == Jurisdiction.id,
                        )
                        .where(SessionModel.end_date.is_(None))
                    )
                    if exclude:
                        stmt = stmt.where(Jurisdiction.id.not_in(exclude))
                    return db.execute(
                        stmt.distinct()
                        .order_by(Jurisdiction.abbreviation)
                        .limit(SESSION_DATE_TOPUP_PER_CYCLE)
                    ).scalars().all()

                needy = _needy(session_date_checked)
                if not needy and session_date_checked:
                    # Everyone has had a turn; start the next round.
                    session_date_checked = set()
                    needy = _needy(session_date_checked)
                session_date_checked |= {j.id for j in needy}
                if needy:
                    stats = refresh_session_dates(db, needy, delay=11.0)
                    db.commit()
                    print(
                        f"sync-worker {worker_id}: session-date top-up -- "
                        f"{stats['filled']} filled, {stats['corrected']} corrected, "
                        f"{stats['failed']} failed across {len(needy)} jurisdiction(s)",
                        flush=True,
                    )
                else:
                    db.commit()
            except Exception:
                db.rollback()
                traceback.print_exc()
            finally:
                db.close()

            # Step 3b: bills that died because their session ran out of clock.
            #
            # Unlike every other status transition, this one is triggered by
            # the CALENDAR, not by an action. Nothing is filed when a session
            # adjourns -- the bill just stops -- so no sync touches it, no
            # event fires, and it would keep reporting `in_committee` forever.
            # Without this sweep the adjournment rule would only ever reach
            # bills that happened to be modified for some other reason.
            #
            # Self-limiting: the set is whatever has fallen off the calendar
            # since the last pass, which is zero on most nights and a few
            # thousand the day after a sine die. Batched so one adjournment
            # cannot produce a single enormous IN list.
            db = get_session()
            try:
                newly_dead = list(
                    db.execute(
                        select(Bill.id)
                        .join(SessionModel, SessionModel.id == Bill.session_id)
                        .where(
                            SessionModel.end_date.is_not(None),
                            SessionModel.end_date < datetime.now(timezone.utc).date(),
                            # end_date is `expected_adjournment` -- an estimate.
                            # A session the source still calls active has not
                            # adjourned, whatever our estimate says.
                            SessionModel.active.is_(False),
                            or_(
                                Bill.status.is_(None),
                                Bill.status.in_(sorted(status_mod.LIVE_STATUSES)),
                            ),
                        )
                        .limit(ADJOURNMENT_SWEEP_BATCH)
                    ).scalars()
                )
                if newly_dead:
                    for i in range(0, len(newly_dead), 2000):
                        recompute_status_for_bills(db, newly_dead[i : i + 2000])
                    db.commit()
                    print(
                        f"sync-worker {worker_id}: adjournment sweep closed "
                        f"{len(newly_dead)} bill(s) whose session has ended",
                        flush=True,
                    )
                else:
                    db.commit()
            except Exception:
                db.rollback()
                traceback.print_exc()
            finally:
                db.close()

            # Step 4: api_sync creates bills; refresh the counts every coverage
            # row is derived from rather than waiting on the crawl worker.
            if processed:
                db = get_session()
                try:
                    coverage_mod.recompute_all_coverage(db)
                    db.commit()
                    print(f"sync-worker {worker_id}: coverage recomputed", flush=True)
                except Exception:
                    db.rollback()
                    traceback.print_exc()
                finally:
                    db.close()

            if args.once:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"sync-worker {worker_id}: stopping")
        return 0


def cmd_validate_worker(args: argparse.Namespace) -> int:
    """Dedicated, long-running validation-ONLY loop -- a separate process
    from `cmd_worker` (the crawl worker) so validation's minutes-long
    external HTTP (production search API + each bill's official state site)
    can never hold open a DB session that starves the crawl worker's
    fetch_text connections (the `idle in transaction` root cause this whole
    subcommand exists to fix -- see module docstring + validation.py's
    docstring).

    This loop NEVER claims/touches `ingest_jobs` rows of kind fetch_text or
    api_sync -- it doesn't go through the `ingest_jobs` queue at all for
    validation work. Instead each cycle:

      1. (optional) recompute coverage counts once, so full_text_count is
         fresh before selecting FULL_TEXT_SEARCHABLE candidates (a
         jurisdiction whose crawl just finished shouldn't wait up to
         `--coverage-recompute-interval` for the OTHER worker's periodic
         recompute pass to notice).
      2. Acquire a dedicated pg advisory lock (a DIFFERENT key from every
         other advisory lock in this codebase -- see
         scheduler.VALIDATE_WORKER_CYCLE_ADVISORY_LOCK_KEY) for the
         selection pass, so 2+ validate-worker instances never double-select
         (and never double-validate) the same jurisdiction in the same
         cycle.
      3. Select up to `--batch` candidates via `scheduler.plan_validation`
         (the SAME priority SQL the in-worker validate-scheduler already
         used: FULL_TEXT_SEARCHABLE-not-yet-GREEN first, then stale
         DEGRADED, then oldest-checked-other; GREEN and not-yet-searchable
         rows are excluded by that query already).
      4. For each candidate, call `validation.validate_and_record_txnfree`
         (transaction-free: no session held during the external HTTP,
         short write txn to persist). One try/except per jurisdiction so a
         single bad jurisdiction (a hung site, a DB hiccup) never kills the
         loop.
      5. Sleep `--interval` seconds; repeat. Empty selection also sleeps and
         retries -- never busy-loops.

    A shared RobotsCache + rate-limited httpx clients persist across the
    whole loop lifetime (mirrors `cmd_worker`'s `fulltext_fetcher` reuse
    pattern), so per-host robots.txt caching and politeness pacing survive
    across cycles instead of resetting on every jurisdiction.
    """
    worker_id = args.worker_id or f"{socket.gethostname()}-validate-worker"
    batch = args.batch
    interval = args.interval
    sample_size = args.sample
    jurisdiction_timeout = args.jurisdiction_timeout
    degraded_recheck_age_hours = args.degraded_recheck_age_hours
    print(
        f"validate-worker {worker_id}: batch={batch} interval={interval}s "
        f"sample={sample_size} jurisdiction_timeout={jurisdiction_timeout}s "
        "(validation-only; never touches fetch_text/api_sync jobs)"
    )

    # Shared across the whole loop lifetime, same reuse pattern as
    # cmd_worker's fulltext_fetcher -- per-host robots.txt caching and the
    # search/source clients' connection pools persist across jurisdictions.
    source_client = validation_mod.new_client(timeout=validation_mod.DEFAULT_SOURCE_TIMEOUT)
    search_client = validation_mod.new_client(
        base_url=validation_mod.DEFAULT_SEARCH_API_BASE, timeout=validation_mod.DEFAULT_SEARCH_TIMEOUT
    )
    robots_cache = fulltext_mod.RobotsCache(client=source_client)

    try:
        while True:
            db = get_session()
            try:
                # Step 1: keep counts fresh before selecting, so a
                # jurisdiction whose crawl just finished is immediately
                # visible as FULL_TEXT_SEARCHABLE rather than waiting on the
                # crawl worker's own (much longer) recompute cadence.
                coverage_mod.recompute_all_coverage(db)
                db.commit()
            except Exception:
                db.rollback()
                traceback.print_exc()
            finally:
                db.close()

            db = get_session()
            try:
                # Step 2+3: advisory-locked selection pass. A caller that
                # can't acquire the lock immediately (another validate-worker
                # instance's cycle is mid-selection) skips this cycle
                # entirely -- safe, the next cycle will pick up whatever is
                # still due.
                acquired = db.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": scheduler_mod.VALIDATE_WORKER_CYCLE_ADVISORY_LOCK_KEY},
                ).scalar_one()
                if not acquired:
                    db.commit()
                    time.sleep(interval)
                    continue
                try:
                    candidates = scheduler_mod.plan_validation(
                        db,
                        batch=batch,
                        degraded_recheck_age_hours=degraded_recheck_age_hours,
                    )
                finally:
                    db.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": scheduler_mod.VALIDATE_WORKER_CYCLE_ADVISORY_LOCK_KEY},
                    )
                db.commit()
            except Exception:
                db.rollback()
                traceback.print_exc()
                candidates = []
            finally:
                db.close()

            if not candidates:
                time.sleep(interval)
                continue

            # Step 4: validate each candidate txn-free, one at a time
            # (politely paced -- the shared clients above carry robots.txt
            # caching, and DEFAULT_JURISDICTION_TIMEOUT bounds each one's
            # external-HTTP wall clock so a single hung site can't stall the
            # whole cycle).
            for candidate in candidates:
                try:
                    summary = validation_mod.validate_and_record_txnfree(
                        candidate.jurisdiction_id,
                        sample_size=sample_size,
                        search_client=search_client,
                        source_client=source_client,
                        robots_cache=robots_cache,
                        jurisdiction_timeout=jurisdiction_timeout,
                    )
                    rate = f"{summary.pass_rate:.0%}" if summary.pass_rate is not None else "n/a"
                    leg_rates: dict[str, list[str]] = {}
                    for bill in summary.bills:
                        for leg in bill.legs:
                            leg_rates.setdefault(leg.leg, []).append(leg.status)
                    leg_summary = ", ".join(
                        f"{leg}={'/'.join(statuses)}" for leg, statuses in sorted(leg_rates.items())
                    )
                    print(
                        f"validate-worker {worker_id}: {candidate.jurisdiction_abbr} "
                        f"(was {candidate.status}) sampled={len(summary.bills)} "
                        f"pass_rate={rate} legs=[{leg_summary}]"
                    )
                except Exception as exc:  # noqa: BLE001 - one bad jurisdiction must never kill the loop
                    print(
                        f"validate-worker {worker_id}: ERROR validating {candidate.jurisdiction_abbr}: {exc}"
                    )
                    traceback.print_exc()

            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"validate-worker {worker_id}: stopping")
        return 0
    finally:
        search_client.close()
        source_client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m billcommons_ingest")
    sub = parser.add_subparsers(dest="command", required=True)

    p_seed = sub.add_parser("seed-registry", help="seed jurisdictions/sessions/coverage")
    p_seed.add_argument("--registry", default=None, help=f"path to registry JSON (default {DEFAULT_REGISTRY_PATH})")
    p_seed.set_defaults(func=cmd_seed_registry)

    p_bootstrap = sub.add_parser("bootstrap", help="ingest a session bulk-CSV zip")
    p_bootstrap.add_argument("--state", required=True, help="two-letter state code, e.g. NC")
    p_bootstrap.add_argument("--zip", required=True, help="path to the session CSV zip")
    p_bootstrap.add_argument("--session", default=None, help="session identifier (default: active session)")
    p_bootstrap.add_argument(
        "--source-url",
        default=None,
        help="zip download URL, stored as source_url on a newly-created session row (path c only)",
    )
    p_bootstrap.set_defaults(func=cmd_bootstrap)

    p_api = sub.add_parser("api-sync", help="incremental sync via the v3 API")
    p_api.add_argument("--state", required=True)
    p_api.set_defaults(func=cmd_api_sync)

    p_backfill_versions = sub.add_parser(
        "backfill-api-versions",
        help=(
            "page-resumable one-shot v3 API replay for ONE state, from an explicit --since, "
            "to backfill bill_versions/bill_documents an ordinary api_sync run missed -- "
            "never reads/advances the normal incremental-sync watermark"
        ),
    )
    p_backfill_versions.add_argument("--state", required=True, help="two-letter state code, e.g. CA")
    p_backfill_versions.add_argument(
        "--since", required=True, help="ISO-8601 updated_since, used verbatim for every chunk"
    )
    p_backfill_versions.add_argument("--start-page", type=int, default=1, help="first page to fetch (default 1)")
    p_backfill_versions.add_argument(
        "--page-budget",
        type=int,
        default=10,
        help="max API pages this invocation may consume (default 10; a quota budget, not proof of completion)",
    )
    p_backfill_versions.add_argument(
        "--commit-pages",
        type=int,
        default=5,
        help="pages per transaction (default 5, never greater than --page-budget)",
    )
    p_backfill_versions.set_defaults(func=cmd_backfill_api_versions)

    p_cov = sub.add_parser("recompute-coverage", help="recompute coverage + write report JSON")
    p_cov.add_argument("--output", default=None, help=f"output path (default {DEFAULT_COVERAGE_OUTPUT})")
    p_cov.set_defaults(func=cmd_recompute_coverage)

    p_enqueue_fulltext = sub.add_parser(
        "enqueue-fulltext", help="enqueue fetch_text jobs for bill_documents missing extracted_text"
    )
    p_enqueue_fulltext.add_argument(
        "--limit", type=int, default=None, help="max number of documents to enqueue (default: no limit)"
    )
    p_enqueue_fulltext.set_defaults(func=cmd_enqueue_fulltext)

    p_browser_fetch = sub.add_parser(
        "browser-fetch",
        help="fetch selected robots-dark public documents through attended Chrome/CDP",
    )
    browser_hosts = p_browser_fetch.add_mutually_exclusive_group(required=True)
    browser_hosts.add_argument("--host", choices=browser_fetch_mod.ALLOWLIST)
    browser_hosts.add_argument("--all-hosts", action="store_true", help="round-robin all approved hosts")
    p_browser_fetch.add_argument(
        "--limit", type=_positive_int, default=300, help="max documents to fetch (default: 300)"
    )
    p_browser_fetch.add_argument(
        "--pace",
        type=_non_negative_finite_float,
        default=3.5,
        help="base seconds between documents (default: 3.5)",
    )
    p_browser_fetch.add_argument(
        "--max-seconds",
        type=_positive_float,
        default=1500.0,
        help="wall-clock cap for this run (default: 1500)",
    )
    p_browser_fetch.add_argument("--dry-run", action="store_true", help="show matching documents without fetching")
    p_browser_fetch.set_defaults(func=cmd_browser_fetch)

    p_reset_fetch = sub.add_parser(
        "reset-fetch-attempts",
        help=(
            "clear bill_documents.fetch_attempts (and the permanently_failed/"
            "worker_error notes) so documents excluded by the retry cap get a "
            "fresh budget -- the recovery path after an outage or a fixed source"
        ),
    )
    p_reset_fetch.add_argument(
        "--document-id", action="append", default=None, help="repeatable; a specific bill_documents.id"
    )
    p_reset_fetch.add_argument(
        "--url-like",
        default=None,
        help="SQL LIKE pattern against bill_documents.url, e.g. 'https://leg.state.xx.us/%%'",
    )
    reset_status_group = p_reset_fetch.add_mutually_exclusive_group()
    reset_status_group.add_argument(
        "--status",
        action="append",
        default=None,
        help=(
            "repeatable fulltext_status value whose license_note is cleared "
            f"(default: {' + '.join(RESETTABLE_DEFAULT_STATUSES)})"
        ),
    )
    p_reset_fetch.add_argument(
        "--jurisdiction",
        action="append",
        default=None,
        help="repeatable; two-letter jurisdiction abbreviation, e.g. --jurisdiction MA",
    )
    reset_status_group.add_argument(
        "--only-permanently-failed",
        action="store_true",
        help=(
            "narrow --status to JUST permanently_failed (excludes worker_error) -- the "
            "requeue path after a url_resolvers.py fix, where the fix is document-specific"
        ),
    )
    p_reset_fetch.add_argument(
        "--all", action="store_true", help="every document (required if no other filter is given)"
    )
    p_reset_fetch.add_argument("--limit", type=int, default=None, help="cap the number of rows written")
    p_reset_fetch.add_argument(
        "--dry-run", action="store_true", help="report the matching documents without writing"
    )
    p_reset_fetch.set_defaults(func=cmd_reset_fetch_attempts)

    p_validate = sub.add_parser("validate", help="QA-sample bills against source-of-truth and record validation_runs")
    p_validate.add_argument("--state", default=None, help="two-letter state code, e.g. AK")
    p_validate.add_argument("--all", action="store_true", help="validate every jurisdiction in the DB")
    p_validate.add_argument(
        "--sample", type=int, default=validation_mod.DEFAULT_SAMPLE_SIZE, help="bills to sample (default 5)"
    )
    p_validate.set_defaults(func=cmd_validate)

    p_schedule = sub.add_parser(
        "schedule-refresh", help="enqueue api_sync jobs for jurisdictions due per SPEC refresh cadence"
    )
    p_schedule.set_defaults(func=cmd_schedule_refresh)

    p_worker = sub.add_parser("worker", help="run the long-lived job-queue worker loop")
    p_worker.add_argument("--worker-id", default=None)
    p_worker.add_argument("--poll-interval", type=float, default=5.0)
    p_worker.add_argument(
        "--reschedule-interval",
        type=float,
        default=float(os.environ.get("RESCHEDULE_INTERVAL", "0")),
        help=(
            "seconds between schedule-refresh (api_sync) passes inside the worker "
            "loop (default 0 = DISABLED, env RESCHEDULE_INTERVAL to enable). "
            "Disabled by default: an api_sync job makes many rate-limited v3-API "
            "calls while holding a DB txn open, starving the single worker's "
            "fetch_text crawl (same reason as validate-schedule). Incremental "
            "api_sync runs as a separate paced process once the crawl is caught up."
        ),
    )
    p_worker.add_argument(
        "--fulltext-topup-interval",
        type=float,
        default=10 * 60.0,
        help="seconds between full-text queue top-up checks (default 600s/10min; 0 disables)",
    )
    p_worker.add_argument(
        "--fulltext-topup-batch",
        type=int,
        default=5000,
        help="documents to enqueue per full-text top-up when the queue is low (default 5000)",
    )
    p_worker.add_argument(
        "--fulltext-topup-floor",
        type=int,
        default=1000,
        help="top up the fetch_text queue when it falls below this many jobs (default 1000)",
    )
    p_worker.add_argument(
        "--coverage-recompute-interval",
        type=float,
        default=1200.0,
        help="seconds between coverage-recompute passes inside the worker loop (default 1200s/20min; 0 disables)",
    )
    p_worker.add_argument(
        "--validate-schedule-interval",
        type=float,
        default=float(os.environ.get("VALIDATE_SCHEDULE_INTERVAL", "0")),
        help=(
            "seconds between validation-scheduler enqueue passes "
            "(default 0 = DISABLED, env VALIDATE_SCHEDULE_INTERVAL to enable). "
            "Disabled by default because a slow external validation job on the "
            "single crawl worker starves the fetch_text queue; validation runs "
            "as a separate paced process (see docs/operations)."
        ),
    )
    p_worker.add_argument(
        "--validate-batch",
        type=int,
        default=scheduler_mod.DEFAULT_VALIDATE_BATCH,
        help="jurisdictions to enqueue a validate job for per scheduler pass (default 3)",
    )
    p_worker.set_defaults(func=cmd_worker)

    p_validate_worker = sub.add_parser(
        "validate-worker",
        help=(
            "dedicated, long-running validation-ONLY loop (never touches "
            "fetch_text/api_sync jobs) -- runs validate_and_record_txnfree "
            "on a priority-selected batch each cycle"
        ),
    )
    p_validate_worker.add_argument("--worker-id", default=None)
    p_validate_worker.add_argument(
        "--batch",
        type=int,
        default=int(os.environ.get("VALIDATE_WORKER_BATCH", str(scheduler_mod.DEFAULT_VALIDATE_BATCH))),
        help="jurisdictions to validate per cycle (default 3, env VALIDATE_WORKER_BATCH)",
    )
    p_validate_worker.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("VALIDATE_WORKER_INTERVAL", "300")),
        help="seconds to sleep between cycles (default 300s/5min, env VALIDATE_WORKER_INTERVAL)",
    )
    p_validate_worker.add_argument(
        "--sample",
        type=int,
        default=validation_mod.DEFAULT_SAMPLE_SIZE,
        help="bills to sample per jurisdiction (default 5)",
    )
    p_validate_worker.add_argument(
        "--jurisdiction-timeout",
        type=float,
        default=validation_mod.DEFAULT_JURISDICTION_TIMEOUT,
        help=(
            "per-jurisdiction wall-clock cap in seconds for the external-HTTP "
            "phase (default 180s, env VALIDATION_JURISDICTION_TIMEOUT); on cap, "
            "remaining legs are marked unverifiable rather than raising"
        ),
    )
    p_validate_worker.add_argument(
        "--degraded-recheck-age-hours",
        type=int,
        default=scheduler_mod.DEFAULT_DEGRADED_RECHECK_AGE_HOURS,
        help="re-check a DEGRADED jurisdiction after this many hours (default 6)",
    )
    p_validate_worker.set_defaults(func=cmd_validate_worker)

    p_status = sub.add_parser(
        "recompute-status",
        help="derive bills.status from each bill's action record (see status.py)",
    )
    p_status.add_argument("--limit", type=int, default=0, help="stop after N bills (0 = all)")
    p_status.add_argument("--chunk", type=int, default=2000, help="bills per batch (default 2000)")
    p_status.add_argument(
        "--jurisdiction",
        default=None,
        help="scope to one jurisdiction by abbreviation (e.g. NY); default all",
    )
    p_status.set_defaults(func=cmd_recompute_status)

    p_session_dates = sub.add_parser(
        "backfill-session-dates",
        help="pull authoritative session start/end dates from the Open States v3 API",
    )
    p_session_dates.add_argument(
        "--dry-run", action="store_true", help="report what would change without writing"
    )
    p_session_dates.add_argument(
        "--delay",
        type=float,
        default=11.0,
        help="seconds between jurisdictions (default 11 -- the v3 free tier is ~6 req/min)",
    )
    p_session_dates.set_defaults(func=cmd_backfill_session_dates)

    p_sync_worker = sub.add_parser(
        "sync-worker",
        help=(
            "dedicated metadata-refresh loop -- the ONLY consumer of api_sync "
            "jobs (the crawl worker refuses them); nightly by default"
        ),
    )
    p_sync_worker.add_argument("--worker-id", default=None)
    p_sync_worker.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("SYNC_WORKER_INTERVAL", "86400")),
        help="seconds between cycles (default 86400s/24h, env SYNC_WORKER_INTERVAL)",
    )
    p_sync_worker.add_argument(
        "--max-jobs",
        type=int,
        default=int(os.environ.get("SYNC_WORKER_MAX_JOBS", "200")),
        help=(
            "cap on api_sync jobs drained per cycle so one pathological state "
            "cannot spin a cycle forever (default 200, env SYNC_WORKER_MAX_JOBS)"
        ),
    )
    p_sync_worker.add_argument(
        "--once",
        action="store_true",
        help=(
            "run a single cycle and exit instead of looping -- lets the whole "
            "path be exercised and verified, and makes the command usable from "
            "an external scheduler"
        ),
    )
    p_sync_worker.set_defaults(func=cmd_sync_worker)

    p_ca = sub.add_parser(
        "ca-fulltext",
        help="backfill California full text from the official leginfo pubinfo "
        "bulk (Tier-1 source; leginfo.ca.gov robots-blocks the website, the "
        "downloads host does not)",
    )
    p_ca.add_argument("--zip-url", default=None, help="override pubinfo zip URL")
    p_ca.add_argument("--zip-path", default=None, help="use a local pubinfo zip instead of downloading")
    p_ca.add_argument("--limit", type=int, default=None, help="cap number of CA docs to backfill")
    p_ca.add_argument("--dry-run", action="store_true", help="report match counts without writing")
    p_ca.set_defaults(func=cmd_ca_fulltext)

    return parser


def cmd_ca_fulltext(args: argparse.Namespace) -> int:
    from billcommons_ingest import ca_bulk_fulltext as ca_mod

    result = ca_mod.run_ca_fulltext(
        zip_url=args.zip_url,
        zip_path=args.zip_path,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print(f"ca-fulltext: {result}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
