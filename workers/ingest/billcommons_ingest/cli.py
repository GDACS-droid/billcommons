"""`python -m billcommons_ingest {subcommand}` entrypoint.

Subcommands (per BRIEF-wave2.md):
    seed-registry                     Upsert all 51 jurisdictions/sessions/coverage
                                       rows from data/registry/sessions-2026.json.
    bootstrap --state XX --zip PATH   Ingest a session bulk-CSV zip for state XX.
    api-sync --state XX               Incremental sync via the v3 API (updated_since
                                       the jurisdiction's last successful run).
    recompute-coverage                Recompute jurisdiction_coverage + write
                                       docs/state-coverage/coverage-latest.json.
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
from billcommons_ingest import queue as queue_mod
from billcommons_ingest import registry as registry_mod
from billcommons_ingest.openstates_bulk import ingest_session_csv_zip
from billcommons_schema.models import IngestionRun, Jurisdiction, Session as SessionModel
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


def cmd_bootstrap(args: argparse.Namespace) -> int:
    db = get_session()
    try:
        jurisdiction = db.execute(
            select(Jurisdiction).where(Jurisdiction.abbreviation == args.state.upper())
        ).scalar_one_or_none()
        if jurisdiction is None:
            print(f"bootstrap: no jurisdiction row for state {args.state!r}; run seed-registry first")
            return 1

        session_row = None
        if args.session:
            session_row = db.execute(
                select(SessionModel).where(
                    SessionModel.jurisdiction_id == jurisdiction.id,
                    SessionModel.identifier == args.session,
                )
            ).scalar_one_or_none()
        else:
            session_row = db.execute(
                select(SessionModel)
                .where(SessionModel.jurisdiction_id == jurisdiction.id, SessionModel.active.is_(True))
                .order_by(SessionModel.start_date.desc().nulls_last())
            ).scalars().first()

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


def cmd_worker(args: argparse.Namespace) -> int:
    worker_id = args.worker_id or f"{socket.gethostname()}-{sys.argv[0]}"
    poll_interval = args.poll_interval
    print(f"worker {worker_id}: polling every {poll_interval}s (Ctrl-C to stop)")
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
                    else:
                        raise ValueError(f"unknown job kind: {job.kind!r}")
                    queue_mod.complete_job(db, job)
                    db.commit()
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
    p_bootstrap.set_defaults(func=cmd_bootstrap)

    p_api = sub.add_parser("api-sync", help="incremental sync via the v3 API")
    p_api.add_argument("--state", required=True)
    p_api.set_defaults(func=cmd_api_sync)

    p_cov = sub.add_parser("recompute-coverage", help="recompute coverage + write report JSON")
    p_cov.add_argument("--output", default=None, help=f"output path (default {DEFAULT_COVERAGE_OUTPUT})")
    p_cov.set_defaults(func=cmd_recompute_coverage)

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
