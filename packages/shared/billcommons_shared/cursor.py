"""Opaque change-feed cursor encoding, shared by `/changes`
(billcommons_api.routers.changes) and workers/webhooks/dispatch_webhooks.py.

Moved here (2026-08) so the webhook payload's "cursor" field is GUARANTEED to
decode identically to `/changes`'s own `next_cursor` -- the payload contract
requires "the payload cursor is accepted verbatim by GET
/api/v1/changes?cursor=", and the dispatcher's container does not ship
apps/api (see infra/docker/Dockerfile.webhooks-worker / the API<->workers
import boundary enforced by apps/api/tests/test_container_import_boundary.py
in the other direction), so a single shared implementation is the only way
to make that a structural guarantee instead of two hand-synced copies.

Deliberately not the raw `seq`: consumers persist cursors for months, so
whatever shape is published here is frozen. Wrapping it means the ordering
key can later change -- to a composite, a different column, a sharded scheme
-- without breaking a single stored cursor, and it stops clients from doing
arithmetic on a value whose gaps are meaningless (sequences skip on
rollback).

This module raises plain `InvalidCursor` (a ValueError), not an HTTP
exception -- it has no FastAPI dependency, so `billcommons_api.routers.changes`
wraps it into the API's own 400 error shape at the one call site that faces
an HTTP client.
"""
from __future__ import annotations

import base64
import binascii
import json

CURSOR_VERSION = 1


class InvalidCursor(ValueError):
    """Raised by `decode_cursor` on anything that isn't a cursor this module
    itself produced."""


def encode_cursor(seq: int) -> str:
    raw = json.dumps({"v": CURSOR_VERSION, "seq": seq}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> int:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        # Round-2 fix #10: base64("[]"), base64("123"), base64("null") all
        # decode to valid JSON that is NOT a dict -- `.get` on a list/int/
        # None raises AttributeError, which this function did not catch,
        # turning a garbage-but-decodable cursor into a 500 instead of the
        # intended 400 `InvalidCursor`.
        if not isinstance(payload, dict):
            raise ValueError(f"cursor payload is not an object: {payload!r}")
        if payload.get("v") != CURSOR_VERSION:
            raise ValueError(f"unsupported cursor version {payload.get('v')!r}")
        seq = payload["seq"]
        # `bool` is a subclass of `int` in Python -- reject `true`/`false`
        # explicitly, and reject any non-integral value (e.g. `1.9`), both
        # of which `int(...)` would otherwise silently coerce.
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise ValueError(f"cursor seq is not an integer: {seq!r}")
        # Verify round-3 fix #7: a `seq` outside Postgres' signed-bigint
        # range (-2**63 .. 2**63-1) is a fabricated/corrupted cursor, not a
        # real one this module ever produced -- passed through unchecked, it
        # reaches a `WHERE seq > :cursor_seq` bind parameter on
        # /api/v1/changes and Postgres raises a bigint-overflow error at the
        # SQL layer, a 500 instead of the 400 InvalidCursor every other
        # malformed cursor already gets.
        if seq < 0 or seq > 2**63 - 1:
            raise ValueError(f"cursor seq out of bigint range: {seq!r}")
        return seq
    except (ValueError, KeyError, TypeError, AttributeError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidCursor(str(exc)) from exc
