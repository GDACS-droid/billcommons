"""Operator entrypoints for the dedicated Scout worker (never the API process)."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import re
import signal
import time
import uuid

from billcommons_shared.db import get_sessionmaker
from billcommons_shared.rawstore import FilesystemRawStore
from billcommons_shared.scout import BrowserRequest, ScoutSettings
from sqlalchemy import text

from billcommons_scout.providers import SolariProviderError, SolariResearchBrowserProvider, resolve_solari_api_key
from billcommons_scout.rawstore import PostgresScoutRawStore
from billcommons_scout.runner import ScoutRunner


_SAFE_EXCEPTION_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SAFE_HTTP_STATUSES = {400, 401, 403, 404, 408, 409, 413, 422, 429, 500, 502, 503, 504}
# This is deliberately the final statute URL rather than a chapter-index click
# path.  The infrastructure smoke needs one bounded official capture, not a
# browser-interaction claim; following a link would spend an unnecessary action
# and would add a second page/navigation surface to this check.
_SOLARI_SMOKE_URL = (
    "https://www.leg.state.fl.us/statutes/index.cfm?App_mode=Display_Statute&"
    "Search_String=&URL=0000-0099/0043/Sections/0043.16.html"
)
_SOLARI_SMOKE_MARKERS = (b"43.16", b"Justice Administrative Commission")


def _safe_solari_diagnostic(exc: BaseException, *, fallback_phase: str) -> str:
    """Return operator-safe failure metadata without SDK text or response bodies."""
    if isinstance(exc, SolariProviderError):
        phase, exception_class, status, code, reason = exc.diagnostic_fields()
    else:
        phase = fallback_phase
        exception_class = exc.__class__.__name__
        status = getattr(exc, "status", None)
        code = getattr(exc, "code", None)
        if not isinstance(status, int) or status not in _SAFE_HTTP_STATUSES:
            status = None
        if code not in {"FeatureRequiresPlan", "ConcurrencyLimitExceeded", "PlanLimitExceeded", "BrowserUnhealthy"}:
            code = None
        reason = None
    if not _SAFE_EXCEPTION_NAME.fullmatch(exception_class):
        exception_class = "UnknownError"
    fields = [f"phase={phase}", f"exception={exception_class}"]
    if status is not None:
        fields.append(f"status={status}")
    if code is not None:
        fields.append(f"code={code}")
    if reason is not None:
        fields.append(f"reason={reason}")
    return " ".join(fields)


def _rawstore():
    """Return Scout's service-independent store, with an explicit local escape hatch."""
    backend = os.environ.get("BILLCOMMONS_SCOUT_RAWSTORE_BACKEND", "postgres").strip().casefold()
    if backend == "postgres":
        return PostgresScoutRawStore(get_sessionmaker())
    if backend == "filesystem" and os.environ.get("BILLCOMMONS_SCOUT_ALLOW_FILESYSTEM_RAWSTORE") == "1":
        # Compatibility only for local tests/dev. Production must not silently
        # couple Scout to an ingest-service mounted volume.
        return FilesystemRawStore()
    raise RuntimeError("invalid_scout_rawstore_backend")


def _runner() -> ScoutRunner:
    return ScoutRunner(get_sessionmaker(), _rawstore(), SolariResearchBrowserProvider())


def _check_readiness() -> int:
    """Exercise required local dependencies without exposing their configuration."""
    database_ok = rawstore_ok = False
    try:
        with get_sessionmaker()() as db:
            db.execute(text("SELECT 1"))
            db.execute(text("SELECT 1 FROM scout_research_jobs LIMIT 1"))
        database_ok = True
    except Exception:
        pass
    try:
        store = _rawstore()
        healthcheck = getattr(store, "healthcheck", None)
        if callable(healthcheck):
            rawstore_ok = bool(healthcheck())
        else:
            # The explicitly opted-in filesystem compatibility backend has no
            # read-only health API. Its probe is deleted immediately.
            payload = f"scout-readiness-{uuid.uuid4().hex}".encode()
            key = store.put(payload, {"kind": "scout_readiness"})
            rawstore_ok = store.get(key) == payload
            data_path, meta_path = store._paths(key)
            data_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
    except Exception:
        rawstore_ok = False
    configured = bool(resolve_solari_api_key())
    provider_sdk = importlib.util.find_spec("solari_browser") is not None
    print(
        f"database={'ok' if database_ok else 'failed'} "
        f"scout_tables={'ok' if database_ok else 'failed'} "
        f"rawstore={'ok' if rawstore_ok else 'failed'} "
        f"solari_configured={configured} "
        f"solari_sdk={'available' if provider_sdk else 'missing'}"
    )
    return 0 if database_ok and rawstore_ok and (not configured or provider_sdk) else 2


def _run_worker_loop(runner: ScoutRunner, *, once: bool, worker_id: str) -> int:
    """Drain on TERM/INT: no new claims after signal, current run cleans up."""
    draining = False

    def request_drain(_signum, _frame) -> None:
        nonlocal draining
        draining = True

    old_term = signal.signal(signal.SIGTERM, request_drain)
    old_int = signal.signal(signal.SIGINT, request_drain)
    try:
        if once:
            return 0 if runner.run_once(worker_id) else 1
        next_reap = time.monotonic()
        while not draining:
            if time.monotonic() >= next_reap:
                runner.reap_sessions()
                runner.reap_staging_blobs()
                next_reap = time.monotonic() + 60
            # run_once is the drain boundary: a signal received during a run
            # waits for process/finally cleanup, then stops before next claim.
            if not runner.run_once(worker_id) and not draining:
                time.sleep(1)
        return 0
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


def _idle_while_disabled() -> int:
    """Keep a dark-deployed worker observable without opening a claim path.

    The runner is deliberately never constructed here.  That makes the feature
    flag a hard no-claim boundary while still keeping the service process alive
    for Railway health/log observation.  TERM/INT use the same cooperative
    drain contract as the active worker: there is no in-flight work, so exit is
    immediate after the current sleep boundary.
    """
    draining = False

    def request_drain(_signum, _frame) -> None:
        nonlocal draining
        draining = True

    old_term = signal.signal(signal.SIGTERM, request_drain)
    old_int = signal.signal(signal.SIGINT, request_drain)
    try:
        print("Scout is disabled; idling without job claims.", flush=True)
        while not draining:
            time.sleep(1)
        print("Scout disabled worker drained.", flush=True)
        return 0
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


def main() -> int:
    parser = argparse.ArgumentParser(prog="billcommons-scout")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="print non-secret Scout readiness information")
    worker = sub.add_parser("worker", help="claim and process Scout jobs")
    worker.add_argument("--once", action="store_true")
    sub.add_parser("reap", help="retry durable cleanup_failed browser sessions")
    sub.add_parser("rollback", help="reap eligible sessions and terminalize safely abandoned jobs")
    sub.add_parser("solari-check", help="EXPLICIT opt-in one-session Solari smoke check")
    args = parser.parse_args()
    settings = ScoutSettings.from_env()
    if args.command == "check":
        return _check_readiness()
    # Cleanup is deliberately available while the feature flag is off. A
    # rollback disables new jobs/claims first, then must still be able to reap
    # sessions that were already created by the previous revision.
    if args.command == "reap":
        runner = _runner()
        staging = runner.reap_staging_blobs()
        print(
            f"reap_candidates={runner.reap_sessions()} "
            f"staged_sources={staging['staged_sources']} raw_blobs={staging['raw_blobs']}"
        )
        return 0
    if args.command == "rollback":
        result = _runner().rollback_reconcile()
        print(f"reaped={result['reaped']} terminalized={result['terminalized']}")
        return 0
    if not settings.enabled:
        # Dark launch blocks both API creation and worker claims.
        if args.command == "worker" and not args.once:
            return _idle_while_disabled()
        print("Scout is disabled; no jobs claimed.")
        return 2
    if args.command == "solari-check":
        if os.environ.get("BILLCOMMONS_SCOUT_SOLARI_CHECK") != "1":
            print("Refusing live Solari check; set BILLCOMMONS_SCOUT_SOLARI_CHECK=1.")
            return 2
        started = time.monotonic()
        provider = SolariResearchBrowserProvider()
        started_sessions: list[str] = []
        try:
            capture = provider.capture(BrowserRequest(
                url=_SOLARI_SMOKE_URL, max_pages=1, max_actions=1,
                wall_seconds=settings.browser_wall_seconds, max_bytes=settings.max_direct_bytes,
            ), on_started=started_sessions.append)
        except Exception as exc:
            # Do not print exception text: SDK/provider responses can contain
            # third-party details, endpoints, or response bodies.
            cleanup = "not_created"
            if started_sessions:
                try:
                    provider.release(started_sessions[-1])
                    cleanup = "confirmed"
                except Exception:
                    cleanup = "unconfirmed"
            print(
                f"solari_check=failed {_safe_solari_diagnostic(exc, fallback_phase='capture')} "
                f"cleanup={cleanup}"
            )
            return 1
        try:
            replay = provider.release(capture.provider_session_id)
        except Exception as exc:
            # Capture already succeeded. The one lifecycle release failed;
            # this is not evidence that navigation itself failed.
            print(
                f"solari_check=partial capture=ok cleanup=release_unconfirmed "
                f"{_safe_solari_diagnostic(exc, fallback_phase='release')}"
            )
            return 1
        if not all(marker in capture.body for marker in _SOLARI_SMOKE_MARKERS):
            # A successful navigation to an interstitial or provider error
            # page is not a successful government-source smoke test. Cleanup
            # has already been independently confirmed above.
            print("solari_check=failed phase=verify exception=UnexpectedContent cleanup=confirmed")
            return 1
        print(
            f"solari_check=ok session_ref={hashlib.sha256(capture.provider_session_id.encode()).hexdigest()[:12]} "
            f"actions={capture.actions} runtime_ms={int((time.monotonic() - started) * 1000)} "
            f"capture=official_fl_statute_43_16 navigation=direct_no_click "
            f"markers=43_16,justice_administrative_commission "
            f"replay={'available' if replay else 'unavailable'} cleanup=confirmed"
        )
        return 0
    return _run_worker_loop(_runner(), once=args.once, worker_id=f"scout-{os.getpid()}")


if __name__ == "__main__":
    raise SystemExit(main())
