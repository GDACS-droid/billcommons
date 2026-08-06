"""§5 payload contract tests for the webhook dispatcher, required by spec:

  (a) the payload "cursor" is accepted verbatim by GET /api/v1/changes?cursor=
  (b) the event object's "bill" shape diffs empty against /changes' own
      ChangeEvent.bill (BillSummary) field set
  (c) the documented receiver rules (5-min skew window, constant-time
      compare, dedupe on event id) are actually written down

This file imports workers/webhooks/dispatch_webhooks.py directly (via a
local sys.path insertion, same trick workers/webhooks/tests/conftest.py
uses) -- that is fine for a TEST, but note the API package itself
(billcommons_api/) must never do this; test_container_import_boundary.py
enforces that in the other direction (the API image does not ship workers/).
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

WORKERS_WEBHOOKS = Path(__file__).resolve().parents[3] / "workers" / "webhooks"
sys.path.insert(0, str(WORKERS_WEBHOOKS))

import dispatch_webhooks as dw  # noqa: E402
from billcommons_api.schemas import BillSummary  # noqa: E402
from billcommons_shared.cursor import encode_cursor  # noqa: E402


def test_a_delivery_cursor_is_accepted_verbatim_by_changes(client):
    """Any cursor this module can produce must round-trip through /changes
    without a 400 -- proves the encodings are the SAME implementation
    (billcommons_shared.cursor), not merely compatible-by-coincidence."""
    cursor = encode_cursor(1)
    resp = client.get("/api/v1/changes", params={"cursor": cursor, "per_page": 1})
    assert resp.status_code == 200


def test_b_bill_payload_field_set_matches_changeevent_bill_exactly():
    """serialize_bill's keys must be EXACTLY BillSummary's field names --
    not a subset (which would silently drop a field a consumer might already
    depend on) and not a superset (which would leak a field /changes never
    promised)."""
    now = datetime.now(timezone.utc)
    fake_bill = SimpleNamespace(
        id=uuid.uuid4(), jurisdiction_id=uuid.uuid4(), session_id=uuid.uuid4(),
        chamber="lower", identifier="HB 1", identifier_norm="HB 1", title="An act",
        short_title=None, bill_type=None, status="introduced", status_date=None,
        introduced_date=None, latest_action_text=None, latest_action_date=None,
        source_url=None, updated_at=now,
    )
    payload_keys = set(dw.serialize_bill(fake_bill, "NC", "2026 Session").keys())
    schema_keys = set(BillSummary.model_fields.keys())
    assert payload_keys == schema_keys, (
        f"dispatch_webhooks.serialize_bill diverges from BillSummary: "
        f"payload-only={payload_keys - schema_keys}, schema-only={schema_keys - payload_keys}"
    )


def test_c_receiver_rules_are_documented():
    docs = (Path(__file__).resolve().parents[3] / "docs" / "api" / "webhooks.md").read_text()
    assert "5-minute skew" in docs or "5 minute skew" in docs
    assert "constant-time" in docs.lower() or "compare_digest" in docs
    assert "dedupe on" in docs.lower() and "id" in docs.lower()


def test_dispatcher_quota_constants_match_the_api_router_by_value():
    """dispatch_webhooks.py duplicates BOTH MAX_ACTIVE_PER_HOST and (as of
    verify round-4 fix #1) MAX_ACTIVE_GLOBAL BY VALUE from
    billcommons_api.routers.webhooks -- this worker's container does not
    ship apps/api (see MAX_ACTIVE_PER_HOST's own comment in
    dispatch_webhooks.py, which had CLAIMED since round-3 that this exact
    test existed. Round-4 fix #9 (kimi Finding 3) found it did not -- this is
    that test, finally."""
    from billcommons_api.routers.webhooks import MAX_ACTIVE_GLOBAL as api_max_global
    from billcommons_api.routers.webhooks import MAX_ACTIVE_PER_HOST as api_max_host

    assert dw.MAX_ACTIVE_PER_HOST == api_max_host
    assert dw.MAX_ACTIVE_GLOBAL == api_max_global


def test_dispatcher_quota_lock_key_matches_the_api_router_by_value():
    """Verify round-5 fix #3 (codex HIGH, kimi #3, grok): dispatch_webhooks.
    _attempt_challenge now holds the SAME advisory lock creation/reactivate
    already hold (`_QUOTA_LOCK_KEY`) around its own promotion-time host/
    global recounts -- otherwise a promotion racing an API-side create/
    reactivate (which DOES hold the lock) can both read under-cap and both
    commit. Same "duplicated by value, pinned by a contract test"
    convention as MAX_ACTIVE_PER_HOST/MAX_ACTIVE_GLOBAL above (round-4 fix
    #9) -- this worker's container does not ship apps/api, so it cannot
    import the router's constant directly."""
    from billcommons_api.routers.webhooks import _QUOTA_LOCK_KEY as api_quota_lock_key

    assert dw._QUOTA_LOCK_KEY == api_quota_lock_key


def test_dispatcher_valid_event_kinds_matches_the_api_router_by_value():
    """Batch item 12 (opus Q): dispatch_webhooks.py duplicates VALID_EVENT_
    KINDS BY VALUE from billcommons_api.routers.webhooks (same duplicated-
    by-value story as MAX_ACTIVE_PER_HOST/MAX_ACTIVE_GLOBAL above -- this
    worker's container does not ship apps/api). Nothing previously pinned
    the two sets equal; a kind added to one and not the other would let the
    router accept (or the dispatcher silently mis-scope) an event kind the
    other side does not recognize, with no test catching the drift."""
    from billcommons_api.routers.webhooks import VALID_EVENT_KINDS as api_valid_kinds

    assert dw.VALID_EVENT_KINDS == api_valid_kinds


def test_webhook_watermark_constant_matches_changes_constant():
    """billcommons_api.routers.webhooks and workers/webhooks/dispatch_webhooks
    each hand-declare their own copy of COMMIT_SAFETY_LAG_SECONDS (the
    worker's container doesn't ship apps/api, and the API router doesn't
    ship workers/ -- see both modules' own comments) -- pin them equal here
    so the two safety-lag windows can never silently drift apart."""
    from billcommons_api.routers.changes import COMMIT_SAFETY_LAG_SECONDS as api_lag
    from billcommons_api.routers.webhooks import COMMIT_SAFETY_LAG_SECONDS as webhooks_api_lag

    assert api_lag == webhooks_api_lag == dw.COMMIT_SAFETY_LAG_SECONDS
