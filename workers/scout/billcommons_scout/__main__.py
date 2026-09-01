"""Operator entrypoints for the dedicated Scout worker (never the API process)."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import time

from billcommons_shared.db import get_sessionmaker
from billcommons_shared.rawstore import FilesystemRawStore
from billcommons_shared.scout import BrowserRequest, ScoutSettings

from billcommons_scout.providers import SolariProviderError, SolariResearchBrowserProvider, resolve_solari_api_key
from billcommons_scout.runner import ScoutRunner


_SAFE_EXCEPTION_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SAFE_HTTP_STATUSES = {400, 401, 403, 404, 408, 409, 413, 422, 429, 500, 502, 503, 504}
_SOLARI_SMOKE_URL = "https://www.leg.state.fl.us/robots.txt"
_SOLARI_SMOKE_MARKER = b"User-agent"


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


def _runner() -> ScoutRunner:
    return ScoutRunner(get_sessionmaker(), FilesystemRawStore(), SolariResearchBrowserProvider())


def main() -> int:
    parser = argparse.ArgumentParser(prog="billcommons-scout")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="print non-secret Scout readiness information")
    worker = sub.add_parser("worker", help="claim and process Scout jobs")
    worker.add_argument("--once", action="store_true")
    sub.add_parser("reap", help="retry durable cleanup_failed browser sessions")
    sub.add_parser("solari-check", help="EXPLICIT opt-in one-session Solari smoke check")
    args = parser.parse_args()
    settings = ScoutSettings.from_env()
    if args.command == "check":
        print(f"enabled={settings.enabled} rawstore_configured={bool(os.environ.get('RAWSTORE_ROOT'))} solari_configured={bool(resolve_solari_api_key())}")
        return 0 if settings.enabled and os.environ.get("RAWSTORE_ROOT") else 2
    # Cleanup is deliberately available while the feature flag is off. A
    # rollback disables new jobs/claims first, then must still be able to reap
    # sessions that were already created by the previous revision.
    if args.command == "reap":
        print(f"reap_candidates={_runner().reap_sessions()}")
        return 0
    if not settings.enabled:
        # Dark launch blocks both API creation and worker claims.
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
            # Capture already succeeded. This is the redundant, durable-reaper
            # confirmation failing—not evidence that navigation failed.
            print(
                f"solari_check=partial capture=ok cleanup=release_unconfirmed "
                f"{_safe_solari_diagnostic(exc, fallback_phase='release')}"
            )
            return 1
        if _SOLARI_SMOKE_MARKER not in capture.body:
            # A successful navigation to an interstitial or provider error
            # page is not a successful government-source smoke test. Cleanup
            # has already been independently confirmed above.
            print("solari_check=failed phase=verify exception=UnexpectedContent cleanup=confirmed")
            return 1
        print(
            f"solari_check=ok session_ref={hashlib.sha256(capture.provider_session_id.encode()).hexdigest()[:12]} "
            f"actions={capture.actions} runtime_ms={int((time.monotonic() - started) * 1000)} "
            f"element=robots_user_agent replay={'available' if replay else 'unavailable'} cleanup=confirmed"
        )
        return 0
    runner = _runner()
    if args.once:
        return 0 if runner.run_once(f"scout-{os.getpid()}") else 1
    next_reap = time.monotonic()
    while True:
        if time.monotonic() >= next_reap:
            runner.reap_sessions()
            next_reap = time.monotonic() + 60
        if not runner.run_once(f"scout-{os.getpid()}"):
            time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
