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
import socket
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, text

from billcommons_ingest import api_sync as api_sync_mod
from billcommons_ingest import coverage as coverage_mod
from billcommons_ingest import fulltext as fulltext_mod
from billcommons_ingest import queue as queue_mod
from billcommons_ingest import registry as registry_mod
from billcommons_ingest import scheduler as scheduler_mod
from billcommons_ingest import validation as validation_mod
from billcommons_ingest.openstates_bulk import ingest_session_csv_zip, peek_session_slug
from billcommons_ingest.session_match import (
    MatchPath,
    SessionCandidate,
    infer_classification_for_new_session,
    resolve_session,
)
from billcommons_schema.models import (
    BillDocument,
    IngestionRun,
    Jurisdiction,
    JurisdictionCoverage,
    Session as SessionModel,
)
from billcommons_shared.db import get_session
from billcommons_shared.rawstore import FilesystemRawStore

DEFAULT_REGISTRY_PATH = "data/registry/sessions-2026.json"
DEFAULT_COVERAGE_OUTPUT = "docs/state-coverage/coverage-latest.json"


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
            f"sponsorships={result.sponsorships}"
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
                    queued = queue_mod.count_queued(db_ft, fulltext_mod.FETCH_TEXT_KIND)
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
                job = queue_mod.claim_job(db, worker_id)
                if job is None:
                    db.commit()
                    time.sleep(poll_interval)
                    continue
                print(f"worker {worker_id}: claimed job {job.id} kind={job.kind}")
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
                    is_terminal = exc.status in fulltext_mod.TERMINAL_STATUSES
                    db2 = get_session()
                    try:
                        if exc.document_id and exc.status:
                            document2 = db2.get(BillDocument, exc.document_id)
                            if document2 is not None:
                                document2.license_note = f"fulltext_status={exc.status}"
                        job2 = db2.get(type(job), job.id)
                        if is_terminal:
                            queue_mod.dead_letter_job(db2, job2, str(exc))
                        else:
                            queue_mod.fail_job(db2, job2, str(exc))
                        db2.commit()
                    finally:
                        db2.close()
                except Exception as exc:  # noqa: BLE001 - job-level failure isolation
                    db.rollback()
                    db2 = get_session()
                    try:
                        job2 = db2.get(type(job), job.id)
                        queue_mod.fail_job(db2, job2, str(exc))
                        db2.commit()
                    finally:
                        db2.close()
            finally:
                db.close()
    except KeyboardInterrupt:
        print(f"worker {worker_id}: stopping")
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
