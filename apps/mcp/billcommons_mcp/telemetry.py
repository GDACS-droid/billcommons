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

import os
import sys
import threading

from billcommons_schema.models import ToolInvocation
from billcommons_shared.db import get_session

# Set BILLCOMMONS_TELEMETRY=0 to disable (tests, local dev).
_ENABLED = os.environ.get("BILLCOMMONS_TELEMETRY", "1") not in ("0", "false", "no")

# The client family from the MCP initialize handshake, if the client
# volunteered one. Process-global because MCP holds one client per process
# connection; a wrong value here is a mislabelled row, never a leak.
_client_family: str | None = None
_lock = threading.Lock()


def set_client_family(name: str | None) -> None:
    """Record the client family reported at initialize (e.g. "claude-code").

    Truncated and lowercased: we want a family, not a fingerprint.
    """
    global _client_family
    with _lock:
        _client_family = name.strip().lower()[:40] if name else None


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
                    client_family=_client_family,
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        # Loud enough to find in logs, quiet enough not to affect the caller.
        print(f"telemetry: failed to record {tool}: {exc}", file=sys.stderr)
