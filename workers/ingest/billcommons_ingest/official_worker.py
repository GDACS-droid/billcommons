"""Recurring official-source observations with a durable per-target schedule.

Run separately from the metadata and document workers. Each target is one
bounded transaction; shutdown stops new claims and lets the current observation
finish. A process crash rolls back its row lock and leaves the target due.
"""
from __future__ import annotations

import argparse
import json
import signal
import threading
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from billcommons_schema.models import Jurisdiction, OfficialSourceTarget
from billcommons_shared.db import get_session
from billcommons_ingest.official_ca_actions import ca_delta_url


DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DEFAULT_CADENCE_SECONDS = 86400


def seed_ca_targets(db, *, enable: bool = False) -> int:
    """Idempotently register the seven reviewed, current-session CA deltas.

    This does not change any bill or action. Existing target configuration is
    validated, never silently replaced. Enabling requires the caller's explicit
    switch, and the existing next-check/backoff state survives another seed.
    """
    jurisdiction = db.execute(select(Jurisdiction).where(
        Jurisdiction.abbreviation == "CA")).scalar_one_or_none()
    if jurisdiction is None:
        raise ValueError("California jurisdiction must exist before target registration")
    for day in DAYS:
        scope = {"day": day, "sessions": ["20252026 regular", "special1"]}
        url = ca_delta_url(day)
        db.execute(insert(OfficialSourceTarget).values(
            jurisdiction_id=jurisdiction.id, adapter_name="ca_official_actions",
            source_url=url, scope=scope, enabled=enable,
            cadence_seconds=DEFAULT_CADENCE_SECONDS,
        ).on_conflict_do_nothing(constraint="uq_official_target_adapter_url"))
        target = db.execute(select(OfficialSourceTarget).where(
            OfficialSourceTarget.adapter_name == "ca_official_actions",
            OfficialSourceTarget.source_url == url,
        ).with_for_update()).scalar_one()
        if target.jurisdiction_id != jurisdiction.id or target.scope != scope:
            raise ValueError("existing California target differs from reviewed scope")
        if enable:
            target.enabled = True
    db.flush()
    return len(DAYS)


def run_cycle(*, stop: threading.Event, max_observations: int,
              session_factory: Callable = get_session, observer: Callable | None = None,
              emit: Callable[[dict], None] | None = None) -> int:
    """Observe at most the requested count, committing each result separately."""
    if not 1 <= max_observations <= 100:
        raise ValueError("max_observations must be between 1 and 100")
    if observer is None:
        from billcommons_ingest.official_observer import observe_due_target
        observer = observe_due_target
    if emit is None:
        emit = _emit
    processed = 0
    for _ in range(max_observations):
        if stop.is_set():
            break
        db = session_factory()
        try:
            result = observer(db)
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
        if result is None:
            break
        processed += 1
        emit({"event": "official_observation", "target_id": str(result.target_id),
              "status": result.status, "record_count": result.record_count,
              "reconciliation_count": result.reconciliation_count})
    return processed


def _emit(record: dict) -> None:
    print(json.dumps({"time": datetime.now(timezone.utc).isoformat(), **record},
                     sort_keys=True), flush=True)


def _bounded_int(minimum: int, maximum: int):
    def parse(value: str) -> int:
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"must be between {minimum} and {maximum}")
        return number
    return parse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-ca", action="store_true",
                        help="register seven reviewed CA daily delta targets")
    parser.add_argument("--enable-seeded", action="store_true",
                        help="explicitly enable the targets registered by --seed-ca")
    parser.add_argument("--once", action="store_true", help="run one bounded observation cycle")
    parser.add_argument("--max-observations", type=_bounded_int(1, 100), default=7)
    parser.add_argument("--interval", type=_bounded_int(30, 3600), default=60,
                        help="seconds between scans; each target retains its own cadence")
    args = parser.parse_args(argv)
    if args.enable_seeded and not args.seed_ca:
        parser.error("--enable-seeded requires --seed-ca")
    stop = threading.Event()
    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, lambda *_: stop.set())
    try:
        if args.seed_ca:
            db = get_session()
            try:
                count = seed_ca_targets(db, enable=args.enable_seeded)
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
            _emit({"event": "official_targets_registered", "count": count,
                   "enabled": args.enable_seeded})
        failures = 0
        while not stop.is_set():
            try:
                processed = run_cycle(stop=stop, max_observations=args.max_observations)
                failures = 0
                _emit({"event": "official_cycle_complete", "observations": processed})
            except Exception as exc:
                failures += 1
                # Exception messages can contain source bodies or DSNs.
                _emit({"event": "official_cycle_failed", "error_class": type(exc).__name__,
                       "consecutive_failures": failures})
                if args.once or failures >= 5:
                    return 1
            if args.once:
                break
            stop.wait(args.interval)
        _emit({"event": "official_worker_stopped"})
        return 0
    except Exception as exc:
        _emit({"event": "official_worker_failed", "error_class": type(exc).__name__})
        return 1
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
