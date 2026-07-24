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
    worker                            Long-running loop: claim + process ingest_jobs.
"""
from __future__ import annotations

import argparse
import socket
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from billcommons_ingest import coverage as coverage_mod
from billcommons_ingest import fulltext as fulltext_mod
from billcommons_ingest import queue as queue_mod
from billcommons_ingest import registry as registry_mod
from billcommons_ingest.openstates_bulk import ingest_session_csv_zip, peek_session_slug
from billcommons_ingest.session_match import (
    MatchPath,
    SessionCandidate,
    infer_classification_for_new_session,
    resolve_session,
)
from billcommons_schema.models import (
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
                db, args.zip, session_row=session_row, rawstore=rawstore
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
    print(
        "api-sync: not yet wired to a live OPENSTATES_API_KEY-driven incremental "
        f"pass for {args.state!r} in this environment; openstates_api.OpenStatesClient "
        "is available for scripting incremental syncs once a key is provisioned."
    )
    return 0


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
    print(f"worker {worker_id}: polling every {poll_interval}s (Ctrl-C to stop)")

    # Shared across the whole worker-loop lifetime so per-host robots.txt
    # caching and the politeness rate-limit bucket persist across jobs
    # instead of resetting on every claim.
    fulltext_fetcher = fulltext_mod.FullTextFetcher()
    rawstore = FilesystemRawStore()

    try:
        while True:
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
                    else:
                        raise ValueError(f"unknown job kind: {job.kind!r}")
                    queue_mod.complete_job(db, job)
                    db.commit()
                except fulltext_mod.UnfetchableDocument as exc:
                    # Permanent per-document outcome (robots disallow, empty
                    # URL, missing row) -- retrying won't change the answer,
                    # so dead-letter immediately instead of consuming the
                    # normal backoff/retry budget.
                    db.rollback()
                    db2 = get_session()
                    try:
                        job2 = db2.get(type(job), job.id)
                        queue_mod.dead_letter_job(db2, job2, str(exc))
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

    p_worker = sub.add_parser("worker", help="run the long-lived job-queue worker loop")
    p_worker.add_argument("--worker-id", default=None)
    p_worker.add_argument("--poll-interval", type=float, default=5.0)
    p_worker.set_defaults(func=cmd_worker)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
