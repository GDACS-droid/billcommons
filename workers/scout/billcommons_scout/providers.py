"""Isolated research-browser providers for the Scout worker."""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from billcommons_shared.scout import BrowserCapture, BrowserRequest, ScoutPolicyError, canonicalize_url, is_official_url


LOCAL_ENV_PATH = Path.home() / ".config" / "billcommons" / ".env"
_KNOWN_SOLARI_CODES = frozenset({
    "FeatureRequiresPlan",
    "ConcurrencyLimitExceeded",
    "PlanLimitExceeded",
    "BrowserUnhealthy",
})
_SAFE_HTTP_STATUSES = frozenset({400, 401, 403, 404, 408, 409, 413, 422, 429, 500, 502, 503, 504})
_KNOWN_PHASES = frozenset({"create", "connect", "navigate", "extract"})
_SAFE_EXCEPTION_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SAFE_ERROR_REASONS = (
    ("ERR_ABORTED", "net_aborted"),
    ("ERR_CERT_", "tls"),
    ("ERR_CONNECTION_RESET", "connection_reset"),
    ("ERR_FAILED", "net_failed"),
    ("ERR_NAME_NOT_RESOLVED", "dns"),
    ("Timeout", "timeout"),
    ("Target page, context or browser has been closed", "browser_closed"),
)


class SolariProviderError(RuntimeError):
    """A deliberately non-sensitive Solari failure classification.

    SDK error messages can include response bodies and connection endpoints.  The
    operator CLI needs actionable diagnostics, but must never surface those
    values, so it only receives this fixed-shape classification.
    """

    def __init__(self, phase: str, exc: BaseException) -> None:
        super().__init__("solari_provider_failure")
        self.phase = phase if phase in _KNOWN_PHASES else "unknown"
        name = exc.__class__.__name__
        self.exception_class = name if _SAFE_EXCEPTION_NAME.fullmatch(name) else "UnknownError"
        status = getattr(exc, "status", None)
        self.status = status if isinstance(status, int) and status in _SAFE_HTTP_STATUSES else None
        code = getattr(exc, "code", None)
        self.code = code if code in _KNOWN_SOLARI_CODES else None
        # Patchright's exception type is often only ``Error``. Convert a tiny
        # fixed set of transport markers into our own enum, never retaining or
        # returning the raw message (which can contain URLs and endpoints).
        message = str(exc)
        self.reason = next((reason for marker, reason in _SAFE_ERROR_REASONS if marker in message), None)

    def diagnostic_fields(self) -> tuple[str, str, int | None, str | None, str | None]:
        return self.phase, self.exception_class, self.status, self.code, self.reason


class ProviderSessionPersistenceError(RuntimeError):
    """A created provider session could not be durably recorded by Scout.

    The opaque provider ID is retained solely for the runner's recovery path;
    it never appears in the exception message or an operator diagnostic.
    """

    def __init__(self, provider_session_id: str) -> None:
        super().__init__("browser_session_persistence_failed")
        self.provider_session_id = provider_session_id


def resolve_solari_api_key(api_key: str | None = None, *, env_path: Path = LOCAL_ENV_PATH) -> str | None:
    """Resolve the secret without logging it or executing the local env file.

    Local setup follows the same ``~/.config/billcommons/.env`` convention as
    the database helper.  Parsing it as data, instead of shell-sourcing it,
    keeps the live check safe even if another value contains shell syntax.
    """
    if api_key:
        return api_key
    configured = os.environ.get("SOLARI_API_KEY")
    if configured:
        return configured
    if not env_path.is_file():
        return None
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "SOLARI_API_KEY":
            return value.strip().strip('"').strip("'") or None
    return None


@dataclass
class MockResearchBrowserProvider:
    """Deterministic fixture provider; it never opens a network connection."""

    captures: dict[str, BrowserCapture | Exception] = field(default_factory=dict)
    released: list[str] = field(default_factory=list)

    def capture(self, request: BrowserRequest, *, on_started) -> BrowserCapture:
        result = self.captures.get(request.url)
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise RuntimeError("mock_browser_unconfigured")
        on_started(result.provider_session_id)
        return result

    def release(self, provider_session_id: str) -> str | None:
        self.released.append(provider_session_id)
        return None

    def probe_replay(self, provider_session_id: str) -> str | None:
        return None


class SolariResearchBrowserProvider:
    """Thin adapter around the official async ``solari-browser`` Python SDK.

    It is intentionally the only module that imports the SDK.  Scout uses the
    public ``sessions.create()`` endpoint followed by Patchright's documented
    Playwright-wire ``chromium.connect()`` flow instead of ``launch()``.  That
    lets it persist the provider session ID *before* connection can fail.
    """

    def __init__(self, api_key: str | None = None, *, cleanup_seconds: int = 10) -> None:
        self._api_key = resolve_solari_api_key(api_key)
        self._closed_replays: dict[str, str | None] = {}
        self._cleanup_seconds = cleanup_seconds

    def capture(self, request: BrowserRequest, *, on_started) -> BrowserCapture:
        if not self._api_key:
            raise RuntimeError("solari_not_configured")
        return asyncio.run(self._capture_with_cleanup(request, on_started))

    async def _capture_with_cleanup(self, request: BrowserRequest, on_started) -> BrowserCapture:
        """Drive work under the job timeout, then clean up outside that timeout."""
        state: dict[str, Any] = {
            "phase": "create", "session_id": None, "browser": None,
            "context": None, "playwright": None, "solari": None,
        }
        try:
            # This intentionally bounds connect/navigation/extraction together,
            # but not cleanup: cancellation of the drive must not skip release.
            return await asyncio.wait_for(self._capture(request, on_started, state), timeout=request.wall_seconds)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            raise SolariProviderError(str(state["phase"]), exc) from exc
        except ProviderSessionPersistenceError:
            # Cleanup still runs below; the runner alone receives the opaque
            # ID so it can recover a truthful durable ledger if DB recovers.
            raise
        except SolariProviderError:
            raise
        except Exception as exc:
            raise SolariProviderError(str(state["phase"]), exc) from exc
        finally:
            await self._cleanup_capture(state)

    async def _capture(self, request: BrowserRequest, on_started, state: dict[str, Any]) -> BrowserCapture:
        canonical = canonicalize_url(request.url)
        try:
            from solari_browser import Solari
        except ImportError as exc:  # pragma: no cover - package is optional in local tests
            raise RuntimeError("solari_sdk_unavailable") from exc
        from patchright.async_api import async_playwright

        solari = Solari(api_key=self._api_key, timeout_ms=request.wall_seconds * 1000)
        state["solari"] = solari
        state["phase"] = "create"
        session = await solari.sessions.create(recording=True)
        session_id = str(getattr(session, "id", ""))
        if not session_id:
            raise RuntimeError("solari_missing_session_id")
        state["session_id"] = session_id
        # This callback is a durable database update in ScoutRunner. It must
        # precede Patchright startup/connection so any known session is reaped.
        try:
            on_started(session_id)
        except Exception as exc:
            # The provider will self-clean in the enclosing finally block.
            raise ProviderSessionPersistenceError(session_id) from exc

        state["phase"] = "connect"
        playwright = await async_playwright().start()
        state["playwright"] = playwright
        browser = await playwright.chromium.connect(session.ws_endpoint)
        state["browser"] = browser
        state["phase"] = "navigate"
        # A fresh context prevents inherited state from silently broadening a
        # capture.  Service workers are blocked because their requests can
        # outlive or bypass an ordinary page-routing assumption.
        context = await browser.new_context(service_workers="block")
        state["context"] = context

        async def admit_route(route) -> None:
            # Fetch with redirects disabled, then admit the Location before
            # Chromium sees it. ``continue_`` delegates redirect handling to
            # the browser and is therefore insufficient for this boundary.
            request_url = route.request.url
            if not is_official_url(request_url):
                await route.abort()
                return
            try:
                response = await route.fetch(max_redirects=0, max_retries=0)
                if 300 <= response.status < 400:
                    headers = await response.all_headers()
                    location = next(
                        (value for name, value in headers.items() if name.casefold() == "location"),
                        None,
                    )
                    if not location:
                        await route.abort()
                        return
                    try:
                        target = canonicalize_url(urljoin(request_url, location))
                    except ScoutPolicyError:
                        await route.abort()
                        return
                    if not is_official_url(target):
                        await route.abort()
                        return
                await route.fulfill(response=response)
            except Exception:
                # A network/malformed-header failure remains a failed source,
                # not permission to bypass routing policy.
                await route.abort()

        async def block_web_socket(route) -> None:
            await route.close()

        await context.route("**/*", admit_route)
        await context.route_web_socket("**/*", block_web_socket)
        page = await context.new_page()

        async def close_unexpected_popup(popup) -> None:
            if popup is not page:
                await popup.close()

        # Popup pages are a second navigation surface. Close them rather than
        # treating them as free extra pages outside the job's page budget.
        context.on("page", close_unexpected_popup)

        # Off-domain fonts/analytics are deliberately blocked by the route
        # policy.  DOM readiness is sufficient for evidence extraction and
        # avoids spending the whole job budget waiting for irrelevant load
        # events from resources Scout will never admit.
        await page.goto(
            canonical,
            timeout=request.wall_seconds * 1000,
            wait_until="domcontentloaded",
        )
        state["phase"] = "extract"
        content = (await page.content()).encode("utf-8")
        if len(content) > request.max_bytes:
            raise RuntimeError("browser_body_too_large")
        return BrowserCapture(
            provider_session_id=session_id,
            # Provenance records the browser's final admitted location, not
            # merely the URL the worker requested.
            url=canonicalize_url(page.url),
            mime_type="text/html",
            body=content,
            pages=1,
            actions=1,
        )

    async def _cleanup_capture(self, state: dict[str, Any]) -> None:
        """Bound each cleanup action; never make replay availability fatal."""
        context = state.get("context")
        if context is not None:
            try:
                await asyncio.wait_for(context.close(), timeout=self._cleanup_seconds)
            except Exception:
                pass
        browser = state.get("browser")
        if browser is not None:
            try:
                await asyncio.wait_for(browser.close(), timeout=self._cleanup_seconds)
            except Exception:
                pass
        playwright = state.get("playwright")
        if playwright is not None:
            try:
                await asyncio.wait_for(playwright.stop(), timeout=self._cleanup_seconds)
            except Exception:
                pass
        solari = state.get("solari")
        session_id = state.get("session_id")
        if solari is not None and session_id:
            try:
                # Release even when Patchright did not connect.  404 is defined
                # by the SDK as success, which also makes the runner's later
                # durable reaper release idempotent.
                await asyncio.wait_for(solari.sessions.release_and_wait(session_id), timeout=self._cleanup_seconds)
            except Exception:
                pass
            replay_url = None
            try:
                replay = await asyncio.wait_for(solari.sessions.get_replay_url(session_id), timeout=min(1.0, self._cleanup_seconds))
                replay_url = str(getattr(replay, "url", replay))
            except Exception:
                pass
            self._closed_replays[session_id] = replay_url
        if solari is not None:
            try:
                await asyncio.wait_for(solari.close(), timeout=self._cleanup_seconds)
            except Exception:
                pass

    def release(self, provider_session_id: str) -> str | None:
        """Release an orphan from any worker process, then probe replay.

        A local replay cache is advisory only: reapers instantiate their own
        provider, so every call confirms the remote idempotent release first.
        """
        if not self._api_key:
            raise RuntimeError("solari_not_configured")
        return asyncio.run(asyncio.wait_for(self._release(provider_session_id), timeout=self._cleanup_seconds))

    def probe_replay(self, provider_session_id: str) -> str | None:
        """Best-effort replay probe; unlike release, this never re-closes remotely."""
        if not self._api_key:
            raise RuntimeError("solari_not_configured")
        return asyncio.run(asyncio.wait_for(self._probe_replay(provider_session_id), timeout=self._cleanup_seconds))

    async def _release(self, provider_session_id: str) -> str | None:
        try:
            from solari_browser import Solari
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("solari_sdk_unavailable") from exc
        async with Solari(api_key=self._api_key, timeout_ms=self._cleanup_seconds * 1000) as solari:
            await solari.sessions.release_and_wait(provider_session_id)
            replay_url = None
            deadline = time.monotonic() + max(1.0, self._cleanup_seconds - 1.0)
            while time.monotonic() < deadline:
                try:
                    replay = await asyncio.wait_for(solari.sessions.get_replay_url(provider_session_id), timeout=1.0)
                    replay_url = str(getattr(replay, "url", replay))
                    break
                except Exception:
                    await asyncio.sleep(0.5)
            self._closed_replays[provider_session_id] = replay_url
            return replay_url

    async def _probe_replay(self, provider_session_id: str) -> str | None:
        try:
            from solari_browser import Solari
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("solari_sdk_unavailable") from exc
        async with Solari(api_key=self._api_key, timeout_ms=self._cleanup_seconds * 1000) as solari:
            replay = await asyncio.wait_for(solari.sessions.get_replay_url(provider_session_id), timeout=self._cleanup_seconds)
            replay_url = str(getattr(replay, "url", replay))
            self._closed_replays[provider_session_id] = replay_url
            return replay_url
