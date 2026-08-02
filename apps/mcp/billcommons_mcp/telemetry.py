"""Aggregate usage telemetry for the MCP surface.

Answers one question we could not answer: is anyone actually USING this? The
logs showed 139 successful POSTs against exactly one tool call over the same
window -- connect, list tools, disconnect, which is a directory health prober,
not a researcher. Nothing distinguished the two without recording it.

Three rules, all load-bearing:

1. **Aggregate only.** Tool name, outcome, error CLASS, duration, client
   family. No IP, no query text, no bill ids, no error messages (messages can
   quote user input).
2. **No auth gate.** Adding an API key to measure adoption suppresses the
   adoption it measures. Free and keyless is the distribution strategy.
3. **Never breaks a tool call.** Recording is best-effort and swallows
   everything. A telemetry failure that 500s a legitimate research query would
   be a far worse bug than the blindness it fixes.
"""
from __future__ import annotations

import contextvars
import os
import sys
import threading

from billcommons_schema.models import ToolInvocation
from billcommons_shared.db import get_session
from billcommons_shared.telemetry_constants import PROBE_FAMILY, PROBE_HEADER as _PROBE_HEADER_STR

# Set BILLCOMMONS_TELEMETRY=0 to disable (tests, local dev).
_ENABLED = os.environ.get("BILLCOMMONS_TELEMETRY", "1") not in ("0", "false", "no")

# Reserved family for our OWN read-path monitor. It calls a real tool on a
# 2-minute cadence deliberately (a handshake cannot see a dead database), which
# means it manufactures ~720 "tool calls" a day. Within hours of shipping
# telemetry it was 61 of 63 recorded calls -- the monitor built to prove the
# service was alive was drowning the metric built to prove anyone was using it.
# Marked at the edge, excluded at the read side, never silently deleted.

# Header the monitor sets to identify itself. A header rather than a User-Agent
# match: UA strings are guessable, and anything a stranger can spoof to make
# their calls vanish from a public usage figure is a bad idea.
PROBE_HEADER = _PROBE_HEADER_STR.encode("ascii")

# The client family for the request being served. A ContextVar, not a global:
# the streamable-HTTP session manager serves concurrent requests in one
# process, so a module-level global would attribute rows to whichever client
# connected most recently.
_client_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "billcommons_client_family", default=None
)

# Fallback for the family reported at the MCP initialize handshake, when the
# transport does not carry the request context through to the tool call.
_client_family: str | None = None
_lock = threading.Lock()


def set_client_family(name: str | None) -> None:
    """Record the client family reported at initialize (e.g. "claude-code").

    Truncated and lowercased: we want a family, not a fingerprint.
    """
    global _client_family
    with _lock:
        _client_family = name.strip().lower()[:40] if name else None


def current_client_family() -> str | None:
    """Per-request family if the transport propagated it, else the handshake
    value. Never raises."""
    try:
        scoped = _client_ctx.get()
    except LookupError:  # pragma: no cover - default makes this unreachable
        scoped = None
    return scoped or _client_family


class ClientFamilyMiddleware:
    """ASGI middleware that tags the request with a client family.

    Sits outside FastMCP because FastMCP (1.28) exposes no hook to read HTTP
    headers from inside a tool -- there is no `get_http_headers` in this
    release, and reaching into the session lifecycle to add one would be a lot
    of fragile machinery for a label.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        family = PROBE_FAMILY if headers.get(PROBE_HEADER) else None
        token = _client_ctx.set(family)
        try:
            await self.app(scope, receive, send)
        finally:
            _client_ctx.reset(token)


def record_invocation(
    *,
    tool: str,
    outcome: str,
    error_code: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Best-effort. Swallows every exception by design -- see rule 3."""
    if not _ENABLED:
        return
    try:
        db = get_session()
        try:
            db.add(
                ToolInvocation(
                    tool=tool[:80],
                    outcome=outcome,
                    error_code=error_code[:80] if error_code else None,
                    duration_ms=duration_ms,
                    client_family=current_client_family(),
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        # Loud enough to find in logs, quiet enough not to affect the caller.
        print(f"telemetry: failed to record {tool}: {exc}", file=sys.stderr)
