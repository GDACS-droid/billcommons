"""/api/v1/webhooks -- creation, quotas, validation, manage-token auth,
reactivate modes, and the router's own no-outbound-HTTP invariant.

Live-DB conventions per apps/api/tests/test_feeds_atom.py's docstring: this
suite creates its own throwaway rows (never reads/asserts on pre-existing
corpus rows) and tears them down via an autouse fixture keyed on a
test-only email domain, same pattern as test_alerts.py's
`_cleanup_test_subscriptions`. Hostnames are randomized per test (a random
2-label domain, e.g. "wh-<hex>.com") rather than a fixed one like
"example.com", because `_registrable_domain` reduces any subdomain of a
FIXED 2-label domain to that same domain -- a fixed host would let two
concurrent test runs (this suite does run concurrently with itself; see
test_feeds_atom.py's own note) collide on the per-domain quota test.
(Round-7 fix #3: was "wh-<hex>.test" -- see `_fresh_host`'s own docstring
for why the real public-suffix-list adoption forced the TLD to change.)

Verify round-7 fix #5(b): reserved X-Forwarded-For IP ranges per test group,
so no two tests in this file (or a re-run of this same file, same day --
the per-IP creation quota is a 24h Postgres COUNT, not reset per test
session) can collide on the SAME address and push each other over/under a
quota threshold neither test itself intended to hit:
  - 198.51.100.1-198.51.100.9, 198.51.100.200:
    test_stored_host_is_lowercased_and_trailing_dot_stripped
  - 198.51.100.1-198.51.100.10, 198.51.100.200:
    test_domain_quota_is_enforced_per_host
  - 198.51.101.1-198.51.101.10, 198.51.101.200:
    test_unverified_subs_on_a_domain_do_not_block_a_verified_creation
  - 198.51.102.1-198.51.102.10, 198.51.102.200:
    test_reactivate_409s_when_the_host_quota_is_full
  - 203.0.113.50-203.0.113.61: test_global_quota_counts_only_verified_subs
  - 192.0.2.1-192.0.2.250 (its OWN /24, previously the colliding
    203.0.113.1-250 random range): test_creation_quota_is_enforced_per_ip
Everything above stays under 5 hits per address per day (the per-IP daily
cap), and no address is reused across groups.
"""
from __future__ import annotations

import socket
import uuid

import pytest
from sqlalchemy import text

from billcommons_shared.db import get_session

_EMAIL_DOMAIN = "webhooks-contract-test.example.com"


def _fresh_email() -> str:
    return f"test-{uuid.uuid4().hex[:10]}@{_EMAIL_DOMAIN}"


def _fresh_host() -> str:
    """A 2-label host, so _registrable_domain(host) == host -- unique per
    call, never shared with a concurrent test run.

    Verify round-7 fix #3 DEVIATION: the ".test" TLD (RFC 2606 reserved for
    exactly this "fake hostname in a test" purpose) is deliberately NOT a
    real public suffix -- `publicsuffix2.get_sld` cannot compute an eTLD+1
    for a TLD it has no PSL entry for and falls back to the bare TLD
    itself, so EVERY "wh-<hex>.test" host this fixture ever generated
    collapsed to the single shared registrable domain "test" the moment
    fix #3 replaced the curated fixed-depth logic (which had no such
    fallback and simply returned the last two labels unconditionally).
    ".com" is a real, one-level PSL entry, so a 2-label "wh-<hex>.com" host
    reduces to itself via `get_sld` exactly as this fixture always assumed
    -- switched here, not because "wh-<hex>.test" was wrong before, but
    because fix #3 changed what "an unregistered TLD" means for this
    computation."""
    return f"wh-{uuid.uuid4().hex[:12]}.com"


def _body(**overrides) -> dict:
    body = {
        "url": f"https://{_fresh_host()}/hook",
        "email": _fresh_email(),
        "kind": "topic",
        "target": "artificial-intelligence",
    }
    body.update(overrides)
    return body


@pytest.fixture()
def webhook_bill_fixture():
    """One throwaway jurisdiction/session/bill -- verify round-6 fix #5's
    "bills-kind targets must exist at creation" tests need at least one
    GENUINE bill id to accept alongside a fabricated one. ZZ-prefixed
    create/teardown, same convention as test_feeds_atom.py's own
    `feed_fixture`."""
    from billcommons_schema.models import Bill, Jurisdiction
    from billcommons_schema.models import Session as SessionModel

    db = get_session()
    abbr = f"ZZ{uuid.uuid4().hex[:6].upper()}"
    jurisdiction = Jurisdiction(name=f"Test State {abbr}", abbreviation=abbr, classification="state")
    db.add(jurisdiction)
    db.flush()
    session_row = SessionModel(jurisdiction_id=jurisdiction.id, identifier="2026 Test Session", active=True)
    db.add(session_row)
    db.flush()
    bill = Bill(
        jurisdiction_id=jurisdiction.id, session_id=session_row.id,
        identifier="HB 1", identifier_norm="HB 1", title="A webhook-target-validation test bill",
    )
    db.add(bill)
    db.flush()
    db.commit()
    try:
        yield bill
    finally:
        db.close()
        cleanup = get_session()
        try:
            cleanup.execute(text("DELETE FROM bill_events WHERE bill_id = :b"), {"b": bill.id})
            cleanup.execute(text("DELETE FROM bills WHERE id = :b"), {"b": bill.id})
            cleanup.execute(text("DELETE FROM sessions WHERE id = :s"), {"s": session_row.id})
            cleanup.execute(text("DELETE FROM jurisdictions WHERE id = :j"), {"j": jurisdiction.id})
            cleanup.commit()
        finally:
            cleanup.close()


# Verify round-7 fix #5(a) (opus MED #4): every reserved IP range/pattern
# this file's tests create webhook_creation_events rows under -- the three
# named /24s (module docstring) PLUS "testclient" (starlette TestClient's
# own default `request.client.host` for every one of this file's ~30 calls
# that post without an X-Forwarded-For header at all).
_CREATION_EVENT_IP_PATTERNS = (
    "198.51.100.%", "198.51.101.%", "198.51.102.%", "198.51.103.%", "203.0.113.%",
    "192.0.2.%", "testclient",
    # r12 fix #4: the /64-bucketed creator_ip values
    # test_unverified_per_host_quota_is_scoped_per_creator_ip_ipv6_64_bucket
    # persists (the raw addresses it sends collapse to these two /64
    # network addresses before storage -- see `quota_bucket`).
    "2001:db8:bc12:34::%", "2001:db8:bc12:9999::%",
)


@pytest.fixture(autouse=True)
def _cleanup_test_webhooks():
    yield
    db = get_session()
    try:
        db.execute(
            text("DELETE FROM webhook_subscriptions WHERE email LIKE :pat"),
            {"pat": f"%@{_EMAIL_DOMAIN}"},
        )
        # Verify round-7 fix #5(a): the creation-events ledger (migration
        # 0014, no FK to webhook_subscriptions) is NOT covered by the
        # email-keyed DELETE above at all -- left alone, it accumulates one
        # row per POST across every run of this suite and, since the per-IP
        # daily quota counts FROM this ledger once it exists (see
        # `_creation_quota_stmt`), the reserved test IPs/host above hit
        # their real 5/day cap by the suite's third run of the same day,
        # turning genuinely-under-quota test creations into unexplained
        # 429s. Probe-guarded exactly like `_creation_events_table_exists`
        # itself -- a clean no-op until migration 0014 is applied to prod.
        table_exists = db.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'webhook_creation_events' "
                "AND table_schema = current_schema()"
            )
        ).scalar_one_or_none() is not None
        if table_exists:
            for pattern in _CREATION_EVENT_IP_PATTERNS:
                db.execute(
                    text("DELETE FROM webhook_creation_events WHERE creator_ip LIKE :pat"),
                    {"pat": pattern},
                )
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Creation + response shape
# ---------------------------------------------------------------------------


def test_create_returns_201_with_manage_token_and_secret_once(client):
    resp = client.post("/api/v1/webhooks", json=_body())
    assert resp.status_code == 201
    payload = resp.json()
    assert payload["verified"] is False
    assert len(payload["manage_token"]) > 20
    assert len(payload["signing_secret"]) > 20
    assert payload["manage_token"] != payload["signing_secret"]


def test_create_rejects_http_scheme(client):
    resp = client.post("/api/v1/webhooks", json=_body(url=f"http://{_fresh_host()}/hook"))
    assert resp.status_code == 422 or resp.status_code == 400
    assert resp.json()["error"]["code"] in ("invalid_webhook_url",)


def test_create_rejects_non_default_port(client):
    resp = client.post("/api/v1/webhooks", json=_body(url=f"https://{_fresh_host()}:8443/hook"))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_webhook_url"


def test_create_rejects_userinfo_in_url(client):
    resp = client.post(
        "/api/v1/webhooks", json=_body(url=f"https://user:pw@{_fresh_host()}/hook")
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_webhook_url"


def test_create_rejects_malformed_bracket_url_as_400_not_500(client):
    """Round-3 fix #5: a malformed bracketed-IPv6 url ("https://[::1/hook")
    used to raise a raw ValueError past `admit_url`, which this router only
    catches as `SsrfRejected` -- an uncaught ValueError is a 500."""
    resp = client.post(
        "/api/v1/webhooks", json=_body(url=f"https://[::1/hook")
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_webhook_url"


def test_create_rejects_lone_surrogate_in_url_path_as_a_clean_4xx_not_500(client):
    """Round-3 fix #8: a lone UTF-16 surrogate reaching `admit_url`'s
    `_normalize_path_and_query` used to raise a raw UnicodeEncodeError -- a
    500, never surfaced to the caller as any kind of clean 4xx.

    DEVIATION from the fix's literal wording ("Test via router: 400
    invalid_webhook_url, not 500"): reaching create_webhook's OWN
    `admit_url` call with a raw lone surrogate turns out to be unreachable
    through this router's actual request path -- Pydantic's `body: str`
    field validation rejects a lone surrogate with its own 422
    ('string_unicode', "unable to parse raw data as a unicode string")
    BEFORE `create_webhook`'s body ever runs, so `admit_url` never sees it
    here at all. That 422 is itself proof the raw exception never escapes
    as a 500 -- this test pins THAT. `admit_url`'s own fix is still real
    and necessary: it is ALSO the dispatcher's re-admission of the STORED
    url on every delivery/challenge attempt (see `admit_url`'s own
    docstring), a caller this router-level guard does not cover -- see
    packages/shared/tests/test_safe_http.py's direct unit test of
    `admit_url` itself for that path.

    httpx's own JSON encoder (`json.dumps(..., ensure_ascii=False).encode
    ("utf-8")`) cannot even SERIALIZE a lone surrogate client-side -- same
    problem test_feeds_atom.py's own lone-surrogate test documents for
    Postgres -- so the wire bytes here are constructed by hand instead: a
    `\\ud800` JSON *escape sequence* is pure ASCII on the wire and
    `json.loads` on the SERVER side decodes that escape into a real
    lone-surrogate Python str, which is what reaches Pydantic's validator.
    """
    import json as json_module

    body = _body(url=f"https://{_fresh_host()}/PLACEHOLDER")
    raw = json_module.dumps(body).replace("PLACEHOLDER", "\\ud800")
    resp = client.post(
        "/api/v1/webhooks",
        content=raw.encode("ascii"),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code < 500, resp.text
    assert resp.status_code == 422, resp.text


def test_create_rejects_garbage_email(client):
    resp = client.post("/api/v1/webhooks", json=_body(email="not-an-email"))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_email"


def test_create_rejects_unknown_topic(client):
    resp = client.post("/api/v1/webhooks", json=_body(target="nonexistent-topic"))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unknown_topic"


def test_create_rejects_unknown_jurisdiction_kind(client):
    resp = client.post(
        "/api/v1/webhooks",
        json=_body(kind="jurisdiction", target="ZZ"),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unknown_jurisdiction"


def test_create_bills_kind_rejects_unparseable_uuid(client):
    resp = client.post(
        "/api/v1/webhooks", json=_body(kind="bills", target="not-a-uuid")
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_bill_id"


def test_create_bills_kind_rejects_more_than_the_max_ids(client):
    """r12 fix #1: MAX_BILL_IDS lowered 200 -> 64 (see that constant's own
    comment for the btree-overflow probe this bound is sized from)."""
    import billcommons_api.routers.webhooks as webhooks_module

    ids = ",".join(str(uuid.uuid4()) for _ in range(webhooks_module.MAX_BILL_IDS + 1))
    resp = client.post("/api/v1/webhooks", json=_body(kind="bills", target=ids))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "too_many_bill_ids"


def test_create_bills_kind_accepts_at_the_new_max(client):
    """r12 fix #1's own boundary: exactly MAX_BILL_IDS (64) real bill ids
    must still create cleanly and commit -- the whole point of lowering
    the bound was to stay UNDER the btree tuple ceiling, not to make the
    max itself unreachable."""
    import uuid as uuid_module

    from billcommons_schema.models import Bill, Jurisdiction
    from billcommons_schema.models import Session as SessionModel

    import billcommons_api.routers.webhooks as webhooks_module

    db = get_session()
    abbr = f"ZZ{uuid_module.uuid4().hex[:6].upper()}"
    jurisdiction = Jurisdiction(name=f"Test State {abbr}", abbreviation=abbr, classification="state")
    db.add(jurisdiction)
    db.flush()
    session_row = SessionModel(jurisdiction_id=jurisdiction.id, identifier="2026 Test Session", active=True)
    db.add(session_row)
    db.flush()
    bills = [
        Bill(
            jurisdiction_id=jurisdiction.id, session_id=session_row.id,
            identifier=f"HB {i}", identifier_norm=f"HB {i}", title="a max-bill-ids boundary test bill",
        )
        for i in range(webhooks_module.MAX_BILL_IDS)
    ]
    db.add_all(bills)
    db.flush()
    db.commit()
    bill_ids = [b.id for b in bills]
    try:
        ids = ",".join(str(b) for b in bill_ids)
        resp = client.post("/api/v1/webhooks", json=_body(kind="bills", target=ids))
        assert resp.status_code == 201, resp.text
    finally:
        cleanup = get_session()
        try:
            cleanup.execute(text("DELETE FROM webhook_subscriptions WHERE kind = 'bills' AND target = :t"), {"t": ids})
            cleanup.execute(text("DELETE FROM bill_events WHERE bill_id = ANY(:ids)"), {"ids": bill_ids})
            cleanup.execute(text("DELETE FROM bills WHERE id = ANY(:ids)"), {"ids": bill_ids})
            cleanup.execute(text("DELETE FROM sessions WHERE id = :s"), {"s": session_row.id})
            cleanup.execute(text("DELETE FROM jurisdictions WHERE id = :j"), {"j": jurisdiction.id})
            cleanup.commit()
        finally:
            cleanup.close()
        db.close()


# ---------------------------------------------------------------------------
# r13 fix #1: the r12 fix (MAX_BILL_IDS -> 64) only ever bounded `target`'s
# own byte length -- the btree ceiling `uq_webhook_url_kind_target_event_
# kinds` actually hits is on the COMBINED (url, kind, target, event_kinds)
# tuple, and `url` has no length cap of its own below the schema's 2000-CHAR
# limit. `test_create_bills_kind_accepts_at_the_new_max` above already
# covers "64 ids + a short (default-fixture) url still creates" -- these two
# cover the url dimension specifically, at both ends of MAX_BILL_IDS.
# ---------------------------------------------------------------------------


def test_create_rejects_bills_kind_when_url_alone_busts_the_combined_budget(client):
    """A single real bill id (well under MAX_BILL_IDS) plus a schema-valid
    but long, percent-encoding-expanded url busts MAX_SUBSCRIPTION_KEY_BYTES
    on its own -- 400 subscription_key_too_large, never the uncaught 500 the
    bare btree ceiling would otherwise raise (SQLSTATE 54000 escapes the
    narrowed 23505 handler, correctly)."""
    import uuid as uuid_module

    from billcommons_schema.models import Bill, Jurisdiction
    from billcommons_schema.models import Session as SessionModel

    db = get_session()
    abbr = f"ZZ{uuid_module.uuid4().hex[:6].upper()}"
    jurisdiction = Jurisdiction(name=f"Test State {abbr}", abbreviation=abbr, classification="state")
    db.add(jurisdiction)
    db.flush()
    session_row = SessionModel(jurisdiction_id=jurisdiction.id, identifier="2026 Test Session", active=True)
    db.add(session_row)
    db.flush()
    bill = Bill(
        jurisdiction_id=jurisdiction.id, session_id=session_row.id,
        identifier="HB 1", identifier_norm="HB 1", title="a byte-budget boundary test bill",
    )
    db.add(bill)
    db.flush()
    db.commit()
    bill_id = bill.id
    try:
        # 1900 two-byte-UTF-8 characters in the path -- each percent-encodes
        # to 6 ASCII bytes ("%XX%XX"), well past MAX_SUBSCRIPTION_KEY_BYTES
        # while the raw url string itself (~1930 chars) stays comfortably
        # under the schema's own 2000-CHAR cap.
        long_path = "é" * 1900
        resp = client.post(
            "/api/v1/webhooks",
            json=_body(
                url=f"https://{_fresh_host()}/{long_path}",
                kind="bills",
                target=str(bill_id),
            ),
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "subscription_key_too_large"
    finally:
        cleanup = get_session()
        try:
            cleanup.execute(text("DELETE FROM bill_events WHERE bill_id = :i"), {"i": bill_id})
            cleanup.execute(text("DELETE FROM bills WHERE id = :i"), {"i": bill_id})
            cleanup.execute(text("DELETE FROM sessions WHERE id = :s"), {"s": session_row.id})
            cleanup.execute(text("DELETE FROM jurisdictions WHERE id = :j"), {"j": jurisdiction.id})
            cleanup.commit()
        finally:
            cleanup.close()
        db.close()


def test_create_rejects_topic_kind_with_a_long_percent_encoded_url(client):
    """opus's kind='topic' case: the combined-bytes budget applies to EVERY
    kind, not just 'bills' -- a topic subscription's target is a short
    slug, but a long, percent-encoding-expanded url busts the SAME shared
    budget just as effectively. 400, not 500."""
    long_path = "é" * 1900
    resp = client.post(
        "/api/v1/webhooks",
        json=_body(url=f"https://{_fresh_host()}/{long_path}", kind="topic", target="artificial-intelligence"),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "subscription_key_too_large"


def test_create_bills_kind_dedupes_and_accepts(client, webhook_bill_fixture):
    # Verify round-6 fix #5: a fabricated (never-persisted) bill id no
    # longer creates cleanly -- this test now needs a GENUINE bill id, from
    # `webhook_bill_fixture`, to still exercise the dedupe behavior.
    one = str(webhook_bill_fixture.id)
    ids = f"{one},{one}"
    resp = client.post("/api/v1/webhooks", json=_body(kind="bills", target=ids))
    assert resp.status_code == 201


def test_create_bills_kind_rejects_a_bill_id_that_does_not_exist(client):
    """Verify round-6 fix #5 (deepseek MED #5): `_validate_target` parses
    UUIDs but, before this fix, never checked the bills existed -- a
    typo'd/fabricated bill id created cleanly and then quiet-ran FOREVER
    (the dispatcher's `BillEvent.bill_id.in_(ids)` scope match is a
    perfectly healthy-looking empty result for a nonexistent id, exactly
    the "reported as nothing to report" failure mode this codebase's other
    scope-validity fixes exist to prevent). Now caught at creation time,
    400 invalid_webhook_target, naming the unknown id."""
    fabricated = str(uuid.uuid4())
    resp = client.post("/api/v1/webhooks", json=_body(kind="bills", target=fabricated))
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "invalid_webhook_target"
    assert fabricated in body["error"]["message"]


def test_create_bills_kind_rejects_one_fabricated_id_among_real_ones(client, webhook_bill_fixture):
    """One good id (from the fixture) plus one fabricated id -- the whole
    request 400s, naming ONLY the fabricated one, not the real one."""
    real = str(webhook_bill_fixture.id)
    fabricated = str(uuid.uuid4())
    resp = client.post(
        "/api/v1/webhooks", json=_body(kind="bills", target=f"{real},{fabricated}")
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "invalid_webhook_target"
    assert fabricated in body["error"]["message"]
    assert real not in body["error"]["message"]


def test_create_rejects_unknown_event_kind(client):
    resp = client.post("/api/v1/webhooks", json=_body(event_kinds="status,not-a-kind"))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unknown_event_kind"


def test_create_rejects_jurisdiction_field_with_bills_kind(client):
    resp = client.post(
        "/api/v1/webhooks",
        json=_body(kind="bills", target=str(uuid.uuid4()), jurisdiction="FL"),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "jurisdiction_field_not_used"


# ---------------------------------------------------------------------------
# URL normalization (fix #17) + event_kinds ""-vs-NULL (fix #18)
# ---------------------------------------------------------------------------


def _mark_verified(host: str) -> None:
    """Test-only helper: flips `verified` true for every row on `host` via a
    direct DB write, standing in for the dispatcher's real challenge/answer
    round trip (which this DB-only router never performs itself -- see the
    module docstring). Needed since round-2 fix #7 makes the per-domain
    quota count only verified subscriptions -- a quota test has to actually
    verify its throwaway rows to exercise that quota at all."""
    db = get_session()
    try:
        db.execute(
            text("UPDATE webhook_subscriptions SET verified = true WHERE host = :h"),
            {"h": host},
        )
        db.commit()
    finally:
        db.close()


def test_stored_host_is_lowercased_and_trailing_dot_stripped(client):
    """A mixed-case host with a trailing FQDN dot must normalize to the
    same registrable domain as its plain lowercase form -- proven
    end-to-end via the per-host quota, which reads straight off the stored
    `host` column: if normalization didn't happen, these two "different"
    hosts would each get their OWN 10-subscription quota bucket instead of
    sharing one. Rows are flipped `verified` (fix #7: the quota counts only
    verified subs) via `_mark_verified` between the 10th and 11th create."""
    host = _fresh_host()
    mixed_case_dotted = f"{host.upper()}."

    first = client.post("/api/v1/webhooks", json=_body(url=f"https://{mixed_case_dotted}/hook/a"))
    assert first.status_code == 201, first.text

    ips = [f"198.51.100.{i + 1}" for i in range(9)]
    for i, ip in enumerate(ips):
        resp = client.post(
            "/api/v1/webhooks",
            json=_body(url=f"https://{host}/hook/b{i}"),
            headers={"x-forwarded-for": ip},
        )
        assert resp.status_code == 201, resp.text

    _mark_verified(host)

    eleventh = client.post(
        "/api/v1/webhooks",
        json=_body(url=f"https://{host}/hook/eleventh"),
        headers={"x-forwarded-for": "198.51.100.200"},
    )
    assert eleventh.status_code == 403, (
        "the mixed-case/trailing-dot subscription and the 9 plain ones must "
        "share ONE per-host quota bucket (10 total), not two separate ones"
    )
    assert eleventh.json()["error"]["code"] == "webhook_domain_quota_exceeded"


def test_duplicate_url_differing_only_by_case_or_trailing_dot_is_a_409(client):
    """The uniqueness constraint binds on the STORED (normalized) url --
    two subscribe attempts differing only in host case/trailing dot must
    collide as the same subscription, per fix #17's "keep raw input
    nowhere" requirement."""
    host = _fresh_host()
    first = client.post("/api/v1/webhooks", json=_body(url=f"https://{host}/hook"))
    assert first.status_code == 201

    again = client.post(
        "/api/v1/webhooks",
        json=_body(url=f"https://{host.upper()}./hook", email=_fresh_email()),
    )
    assert again.status_code == 409


def test_comma_only_event_kinds_normalizes_to_null_not_empty_string(client):
    """`event_kinds=",,"` must store as NULL (fix #18), not `""` -- proven
    indirectly: a NULL-event_kinds subscription and an explicit-omitted-
    event_kinds subscription to the SAME url/kind/target must collide on
    the uniqueness constraint (nulls_not_distinct=True treats every NULL as
    equal), which only happens if the comma-only input was normalized to
    NULL rather than stored as a distinct "" string."""
    host = _fresh_host()
    body = _body(url=f"https://{host}/hook", event_kinds=",, ,")
    first = client.post("/api/v1/webhooks", json=body)
    assert first.status_code == 201

    no_event_kinds = {k: v for k, v in body.items() if k != "event_kinds"}
    no_event_kinds["email"] = _fresh_email()
    again = client.post("/api/v1/webhooks", json=no_event_kinds)
    assert again.status_code == 409, (
        "comma-only event_kinds must normalize to the same NULL as omitting "
        "it entirely, or these two would NOT collide"
    )


def test_ipv6_literal_url_round_trips_with_brackets(client):
    """Round-2 fix #4: `admit_url`'s `.hostname` strips the `[...]` around
    an IPv6 literal -- the router must re-bracket it when reconstructing
    the stored/normalized url, or the stored value is not a valid URL at
    all and can never be delivered to or re-admitted at dispatch time."""
    resp = client.post(
        "/api/v1/webhooks",
        json=_body(url="https://[2001:db8::1]/hook"),
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()

    db = get_session()
    try:
        stored_url = db.execute(
            text("SELECT url FROM webhook_subscriptions WHERE id = :id"),
            {"id": created["id"]},
        ).scalar_one()
    finally:
        db.close()
    assert stored_url == "https://[2001:db8::1]/hook"


def test_resubscribing_same_url_kind_target_event_kinds_is_a_409(client):
    host = _fresh_host()
    body = _body(url=f"https://{host}/hook")
    first = client.post("/api/v1/webhooks", json=body)
    assert first.status_code == 201
    # Same url/kind/target/event_kinds, different email -- the uniqueness
    # constraint does not key on email at all.
    again = client.post("/api/v1/webhooks", json={**body, "email": _fresh_email()})
    assert again.status_code == 409


# ---------------------------------------------------------------------------
# Verify round-5 fix #4: creation-ATTEMPT quota counting (webhook_creation_
# events, migration 0014). Migration 0014 is NOT applied to this live
# database yet (see this suite's own docstring on the repo-wide live-DB
# convention) -- the probe + query-builder logic is pure/unit-testable
# without the real table, per the fix's own instruction; the end-to-end
# behavior against a real table is a post-migration smoke item.
# ---------------------------------------------------------------------------


def test_creation_quota_stmt_targets_events_table_when_present_else_subscriptions():
    """Pure query-builder test, no DB at all: `_creation_quota_stmt` must
    target `webhook_creation_events` once the migration exists and fall
    back to `webhook_subscriptions` (the old, exploitable-by-delete count)
    only when it doesn't."""
    import billcommons_api.routers.webhooks as webhooks_module

    since = object()  # never actually compared/executed -- str(stmt) only
    stmt_present = webhooks_module._creation_quota_stmt(events_table_exists=True, ip="1.2.3.4", since=since)
    stmt_absent = webhooks_module._creation_quota_stmt(events_table_exists=False, ip="1.2.3.4", since=since)

    assert "webhook_creation_events" in str(stmt_present)
    assert "webhook_subscriptions" not in str(stmt_present)
    assert "webhook_subscriptions" in str(stmt_absent)
    assert "webhook_creation_events" not in str(stmt_absent)


def test_creation_events_table_exists_probe_reads_the_information_schema_result():
    """Fake-session test (no live table needed): `_creation_events_table_
    exists` just needs an object with `.execute(stmt).scalar_one_or_none()`
    -- proven against both outcomes without touching Postgres at all."""
    import billcommons_api.routers.webhooks as webhooks_module

    class _FakeProbeDb:
        def __init__(self, exists: bool):
            self._exists = exists

        def execute(self, stmt):
            from types import SimpleNamespace

            return SimpleNamespace(scalar_one_or_none=lambda: (1 if self._exists else None))

    assert webhooks_module._creation_events_table_exists(_FakeProbeDb(True)) is True
    assert webhooks_module._creation_events_table_exists(_FakeProbeDb(False)) is False


def test_creation_events_table_exists_probe_is_schema_qualified():
    """Round-7 fix #6 (opus LOW #5): without `table_schema =
    current_schema()`, a same-named table visible in ANOTHER schema on the
    search_path makes the underlying query return two rows --
    `scalar_one_or_none` would raise `MultipleResultsFound`, an uncaught
    500 on every `POST /webhooks`. Pure string-shape check, no DB
    needed -- the live-schema behavior itself is exercised for free by
    every OTHER test in this file that hits this probe against the real,
    single-schema prod database."""
    import billcommons_api.routers.webhooks as webhooks_module

    class _CapturingDb:
        def __init__(self):
            self.last_stmt = None

        def execute(self, stmt):
            from types import SimpleNamespace

            self.last_stmt = str(stmt)
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    db = _CapturingDb()
    webhooks_module._creation_events_table_exists(db)
    assert "table_schema = current_schema()" in db.last_stmt


# ---------------------------------------------------------------------------
# r12 fix #6 (grok 3): `_challenge_attempted_at_column_exists` -- migration
# 0015's own probe, duplicated by pattern from
# workers/webhooks/dispatch_webhooks.py's function of the same name --
# same "fake-session unit test + schema-qualification check" convention as
# `_creation_events_table_exists` just above.
# ---------------------------------------------------------------------------


def test_challenge_attempted_at_column_exists_probe_reads_the_information_schema_result():
    import billcommons_api.routers.webhooks as webhooks_module

    class _FakeProbeDb:
        def __init__(self, exists: bool):
            self._exists = exists

        def execute(self, stmt):
            from types import SimpleNamespace

            return SimpleNamespace(scalar_one_or_none=lambda: (1 if self._exists else None))

    assert webhooks_module._challenge_attempted_at_column_exists(_FakeProbeDb(True)) is True
    assert webhooks_module._challenge_attempted_at_column_exists(_FakeProbeDb(False)) is False


def test_challenge_attempted_at_column_exists_probe_is_schema_qualified():
    import billcommons_api.routers.webhooks as webhooks_module

    class _CapturingDb:
        def __init__(self):
            self.last_stmt = None

        def execute(self, stmt):
            from types import SimpleNamespace

            self.last_stmt = str(stmt)
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    db = _CapturingDb()
    webhooks_module._challenge_attempted_at_column_exists(db)
    assert "table_schema = current_schema()" in db.last_stmt


def _challenge_attempted_at_column_present() -> bool:
    db = get_session()
    try:
        return db.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'webhook_subscriptions' "
                "AND column_name = 'challenge_attempted_at'"
            )
        ).scalar_one_or_none() is not None
    finally:
        db.close()


requires_migration_0015 = pytest.mark.skipif(
    not _challenge_attempted_at_column_present(),
    reason=(
        "migration 0015 (webhook_subscriptions.challenge_attempted_at) has "
        "not been applied to this database. Applying it to prod is an "
        "orchestrator ship-gate, not something this test suite may do "
        "itself -- same convention as workers/webhooks/tests/"
        "test_dispatch_webhooks.py's requires_migration_0012."
    ),
)


@requires_migration_0015
def test_reactivate_unverified_nulls_the_challenge_attempted_at_rotation_clock(client):
    """r12 fix #6 (grok 3): reactivating an UNVERIFIED sub must also null
    out `challenge_attempted_at` -- `run_challenges` rotates least-
    recently-attempted FIRST, NULLS FIRST (migration 0015) -- so a
    reactivated row (a fresh token, a reset 24h GC clock -- round-4 fix #3,
    already covered by test_reactivate_regenerates_challenge_token_and_
    resets_gc_clock_when_unverified above) must ALSO rejoin the front of
    that rotation, not sit behind every other unverified sub with its
    stale attempted-at timestamp still in place."""
    from billcommons_schema.models import WebhookSubscription

    created = client.post("/api/v1/webhooks", json=_body()).json()
    db = get_session()
    try:
        db.execute(
            text(
                "UPDATE webhook_subscriptions SET active=false, verified=false, "
                "challenge_token=NULL, disabled_reason='domain_quota_exceeded', "
                "disabled_at=now(), challenge_attempted_at=now() WHERE id=:id"
            ),
            {"id": created["id"]},
        )
        db.commit()

        resp = client.post(
            f"/api/v1/webhooks/{created['id']}/reactivate?mode=resume",
            headers={"Authorization": f"Bearer {created['manage_token']}"},
        )
        assert resp.status_code == 200, resp.text

        attempted_at = db.execute(
            text("SELECT challenge_attempted_at FROM webhook_subscriptions WHERE id=:id"),
            {"id": created["id"]},
        ).scalar_one()
        assert attempted_at is None, (
            "a reactivated unverified sub's challenge_attempted_at must be "
            "reset to NULL, putting it back at the front of the rotation"
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Quotas
# ---------------------------------------------------------------------------


def test_creation_quota_is_enforced_per_ip(client):
    # Round-7 fix #5(b): this used to pick from 203.0.113.1-250, which
    # overlaps the file's own hard-coded 203.0.113.50/60/61 (and, formerly,
    # 70/71/80) -- a collision meant this test's 6 creations on a shared
    # address could tip ANOTHER test's fixed-IP assertion over the daily
    # cap, or vice versa. 192.0.2.0/24 is this test's own reserved /24 (see
    # module docstring), touched by nothing else in this file.
    ip = f"192.0.2.{uuid.uuid4().int % 250 + 1}"
    headers = {"x-forwarded-for": ip}
    for _ in range(5):
        resp = client.post("/api/v1/webhooks", json=_body(), headers=headers)
        assert resp.status_code == 201
    sixth = client.post("/api/v1/webhooks", json=_body(), headers=headers)
    assert sixth.status_code == 429
    assert sixth.json()["error"]["code"] == "webhook_creation_quota_exceeded"


def test_creations_never_leave_the_advisory_lock_held(client):
    """Regression companion to fix #16: the advisory lock is transaction-
    scoped (`pg_advisory_xact_lock`), released automatically at commit/
    rollback -- if it leaked, a SECOND creation on a totally unrelated
    IP/host would hang or fail. Proves the lock doesn't wedge future
    requests, which is the only externally-observable symptom of a leak
    from this test's vantage point (a live DB it can't introspect session
    locks on without a superuser probe)."""
    first = client.post("/api/v1/webhooks", json=_body())
    assert first.status_code == 201
    second = client.post("/api/v1/webhooks", json=_body())
    assert second.status_code == 201


def test_domain_quota_is_enforced_per_host(client):
    host = _fresh_host()
    for i in range(10):
        headers = {"x-forwarded-for": f"198.51.100.{i + 1}"}
        resp = client.post(
            "/api/v1/webhooks",
            json=_body(url=f"https://{host}/hook/{i}"),
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
    # Fix #7: the quota counts only VERIFIED subs -- flip these 10 verified
    # (standing in for the dispatcher's real challenge round trip) so the
    # 11th genuinely trips the quota.
    _mark_verified(host)
    headers = {"x-forwarded-for": "198.51.100.200"}
    eleventh = client.post(
        "/api/v1/webhooks", json=_body(url=f"https://{host}/hook/10"), headers=headers
    )
    assert eleventh.status_code == 403
    assert eleventh.json()["error"]["code"] == "webhook_domain_quota_exceeded"


def test_unverified_subs_on_a_domain_do_not_block_a_verified_creation(client, monkeypatch):
    """Round-2 fix #7: 10 UNVERIFIED subscriptions on a domain (never
    flipped `verified`, unlike the tests above) must not consume the
    per-domain VERIFIED-subscription quota -- that cap counts only verified
    subs. Round-7 fix #2 added a SEPARATE per-domain cap on the unverified
    pool itself (its own dedicated test,
    test_unverified_per_host_quota_is_enforced, covers that one) -- raised
    out of the way here so this test keeps isolating the property it always
    tested: an unverified sub occupies zero VERIFIED-quota headroom, no
    matter how many of them exist."""
    import billcommons_api.routers.webhooks as webhooks_module

    monkeypatch.setattr(webhooks_module, "MAX_UNVERIFIED_PER_HOST", 999)

    host = _fresh_host()
    for i in range(10):
        headers = {"x-forwarded-for": f"198.51.101.{i + 1}"}
        resp = client.post(
            "/api/v1/webhooks",
            json=_body(url=f"https://{host}/hook/unverified-{i}"),
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
    # An 11th creation on the SAME domain, from yet another IP, must still
    # succeed -- none of the 10 above ever verified.
    eleventh = client.post(
        "/api/v1/webhooks",
        json=_body(url=f"https://{host}/hook/eleventh"),
        headers={"x-forwarded-for": "198.51.101.200"},
    )
    assert eleventh.status_code == 201, (
        "10 unverified subs on a domain must not block an 11th creation "
        f"(fix #7): {eleventh.text}"
    )


def _current_global_counts() -> tuple[int, int]:
    """(verified active count, unverified active count) right now -- the
    live DB is not empty-guaranteed (other suites run concurrently, see this
    file's own docstring), so round-3 fix #1's global-quota tests below
    monkeypatch the caps RELATIVE to whatever is already there, exactly like
    every other quota test in this file uses randomized hosts/IPs rather
    than assuming a clean slate."""
    db = get_session()
    try:
        verified = db.execute(
            text("SELECT count(*) FROM webhook_subscriptions WHERE active AND verified")
        ).scalar_one()
        unverified = db.execute(
            text("SELECT count(*) FROM webhook_subscriptions WHERE active AND NOT verified")
        ).scalar_one()
        return verified, unverified
    finally:
        db.close()


def test_global_quota_counts_only_verified_subs(client, monkeypatch):
    """Round-3 fix #1: `verified.is_(True)` added to the MAX_ACTIVE_GLOBAL
    count -- an unverifiable sub (nobody controls the target url) must never
    occupy a global-quota seat forever. Proven by capping MAX_ACTIVE_GLOBAL
    down to "1 more than whatever is currently verified" and showing TWO
    unverified creations both still succeed (they don't count), then that
    the cap genuinely bites once one of them verifies."""
    import billcommons_api.routers.webhooks as webhooks_module

    verified_now, _ = _current_global_counts()
    monkeypatch.setattr(webhooks_module, "MAX_ACTIVE_GLOBAL", verified_now + 1)
    # Round-7 fix #2: MAX_UNVERIFIED_GLOBAL is gone -- the two unverified
    # creations below each use their OWN fresh host (`_fresh_host()`), so
    # they can never approach the (now per-host) MAX_UNVERIFIED_PER_HOST
    # cap of 10 either; nothing to monkeypatch for them any more.

    for i in range(2):
        resp = client.post(
            "/api/v1/webhooks",
            json=_body(url=f"https://{_fresh_host()}/hook"),
            headers={"x-forwarded-for": f"203.0.113.{50 + i}"},
        )
        assert resp.status_code == 201, (
            f"unverified creation must not count against the verified-only "
            f"global cap: {resp.text}"
        )

    host = _fresh_host()
    created = client.post(
        "/api/v1/webhooks",
        json=_body(url=f"https://{host}/hook"),
        headers={"x-forwarded-for": "203.0.113.60"},
    )
    assert created.status_code == 201
    _mark_verified(host)

    blocked = client.post(
        "/api/v1/webhooks",
        json=_body(url=f"https://{_fresh_host()}/hook"),
        headers={"x-forwarded-for": "203.0.113.61"},
    )
    assert blocked.status_code == 403, (
        f"the cap must bite once verified count reaches it: {blocked.text}"
    )
    assert blocked.json()["error"]["code"] == "webhook_global_quota_exceeded"


def test_unverified_per_host_quota_is_enforced(client, monkeypatch):
    """Round-7 fix #2: MAX_UNVERIFIED_GLOBAL (a ~50-IP global kill switch)
    is replaced by MAX_UNVERIFIED_PER_HOST -- bounds the pool of active-
    but-unverified rows PER REGISTRABLE DOMAIN instead, so one attacker's
    unverifiable subs on ONE domain can never lock out creations for every
    OTHER domain the way the global cap could."""
    import billcommons_api.routers.webhooks as webhooks_module

    monkeypatch.setattr(webhooks_module, "MAX_UNVERIFIED_PER_HOST", 2)

    host = _fresh_host()
    for i in range(2):
        resp = client.post(
            "/api/v1/webhooks",
            json=_body(url=f"https://{host}/hook/{i}"),
        )
        assert resp.status_code == 201, resp.text

    blocked = client.post(
        "/api/v1/webhooks",
        json=_body(url=f"https://{host}/hook/blocked"),
    )
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["error"]["code"] == "webhook_domain_quota_exceeded"


def test_unverified_per_host_quota_is_scoped_per_creator_ip(client, monkeypatch):
    """r11 fix #6 (opus D): MAX_UNVERIFIED_PER_HOST alone was a victim-
    lockout -- an attacker could fill a real domain owner's ENTIRE
    unverified-per-host pool with junk subscriptions from its own IP,
    403ing the owner's own legitimate attempt to subscribe a webhook for
    their own domain, with no remedy until the 24h challenge GC caught up.
    Scoping the cap to (host, creator_ip) means an attacker filling its own
    budget against a host must not consume any of a DIFFERENT IP's budget
    against that same host."""
    import billcommons_api.routers.webhooks as webhooks_module

    monkeypatch.setattr(webhooks_module, "MAX_UNVERIFIED_PER_HOST", 10)
    # The per-IP daily creation quota (5/day, unrelated to this test's
    # subject) would otherwise cap the attacker's own IP at 5 creations
    # before it could ever reach a 10-unverified-per-host scenario -- raised
    # out of the way, same pattern this file's other quota tests already use
    # to isolate the ONE cap each test actually exercises.
    monkeypatch.setattr(webhooks_module, "MAX_CREATIONS_PER_IP_PER_DAY", 999)

    host = _fresh_host()
    attacker_ip = "198.51.103.1"
    for i in range(10):
        resp = client.post(
            "/api/v1/webhooks",
            json=_body(url=f"https://{host}/hook/attacker-{i}"),
            headers={"x-forwarded-for": attacker_ip},
        )
        assert resp.status_code == 201, resp.text

    # The attacker's own 11th on the same host DOES trip its own cap.
    attacker_blocked = client.post(
        "/api/v1/webhooks",
        json=_body(url=f"https://{host}/hook/attacker-10"),
        headers={"x-forwarded-for": attacker_ip},
    )
    assert attacker_blocked.status_code == 403, attacker_blocked.text

    # A DIFFERENT caller IP -- the real domain owner -- naming the SAME
    # host must not be blocked by the attacker's saturated budget.
    owner = client.post(
        "/api/v1/webhooks",
        json=_body(url=f"https://{host}/hook/owner"),
        headers={"x-forwarded-for": "198.51.103.200"},
    )
    assert owner.status_code == 201, (
        "10 unverified subs from one IP must not block a different IP's "
        f"creation on the same host: {owner.text}"
    )

    # A DIFFERENT domain must be unaffected -- the whole point of the fix.
    unaffected = client.post(
        "/api/v1/webhooks",
        json=_body(url=f"https://{_fresh_host()}/hook"),
    )
    assert unaffected.status_code == 201, (
        f"a full unverified quota on one domain must not affect another: {unaffected.text}"
    )


def test_unverified_per_host_quota_is_scoped_per_creator_ip_ipv6_64_bucket(client, monkeypatch):
    """r12 fix #4 (opus 4, MED but gutting): the (host, creator_ip) cap
    proven above must bind on the /64-COLLAPSED creator_ip for an IPv6
    caller, not the raw /128 -- any client with a routed /64 can otherwise
    mint a fresh, never-reused address per creation and the cap above
    never actually bites."""
    import billcommons_api.routers.webhooks as webhooks_module

    monkeypatch.setattr(webhooks_module, "MAX_UNVERIFIED_PER_HOST", 3)
    monkeypatch.setattr(webhooks_module, "MAX_CREATIONS_PER_IP_PER_DAY", 999)

    host = _fresh_host()
    same_64 = "2001:db8:bc12:34"
    for i in range(3):
        resp = client.post(
            "/api/v1/webhooks",
            json=_body(url=f"https://{host}/hook/v6-{i}"),
            headers={"x-forwarded-for": f"{same_64}::{i + 1}"},
        )
        assert resp.status_code == 201, resp.text

    # A FOURTH address, still within the SAME /64 (only the low bits
    # differ), must collapse to the SAME creator_ip bucket and trip the
    # cap -- proving it binds on the /64, not the raw /128.
    blocked = client.post(
        "/api/v1/webhooks",
        json=_body(url=f"https://{host}/hook/v6-blocked"),
        headers={"x-forwarded-for": f"{same_64}::ffff"},
    )
    assert blocked.status_code == 403, (
        f"a fresh /128 within the SAME /64 must not evade the cap: {blocked.text}"
    )

    # A DIFFERENT /64 gets its own, independent budget against the same host.
    different_64 = client.post(
        "/api/v1/webhooks",
        json=_body(url=f"https://{host}/hook/v6-different-64"),
        headers={"x-forwarded-for": "2001:db8:bc12:9999::1"},
    )
    assert different_64.status_code == 201, (
        f"a DIFFERENT /64 must not share the exhausted bucket: {different_64.text}"
    )


# ---------------------------------------------------------------------------
# Round-7 fix #3: registrable-domain via the REAL public suffix list
# (publicsuffix2.get_sld), replacing the curated fixed-depth
# `_MULTI_PART_SUFFIXES` table. Every assertion below is pinned to the
# ACTUAL output observed from `publicsuffix2==2.20191221` on this box
# (checked interactively before writing these tests, per the fix's own
# instruction -- "don't assume, don't derive it from the old curated
# table's shape"), not to what a curated list would have produced.
# ---------------------------------------------------------------------------


def test_registrable_domain_co_uk_collapses_within_one_domain_not_across():
    """co.uk is a real two-level ccTLD entry in the bundled PSL: two
    unrelated hosts merely SHARING that suffix must not reduce to one
    shared "co.uk" bucket, but two subdomains of the SAME real domain
    still must."""
    import billcommons_api.routers.webhooks as webhooks_module

    victim = webhooks_module._registrable_domain(f"innocuous-{uuid.uuid4().hex[:8]}.co.uk")
    attacker = webhooks_module._registrable_domain(f"attacker-{uuid.uuid4().hex[:8]}.co.uk")
    assert victim != attacker
    assert victim.endswith(".co.uk")

    same_a = webhooks_module._registrable_domain("hooks.example.co.uk")
    same_b = webhooks_module._registrable_domain("api.example.co.uk")
    assert same_a == same_b == "example.co.uk"


def test_registrable_domain_github_io_gives_each_tenant_its_own_bucket():
    """github.io is listed in the PSL as a suffix in its own right (one
    level, not multi-part) -- unlike co.uk, two DIFFERENT github.io
    tenants must NOT collapse; each subdomain-per-customer gets its own
    registrable domain."""
    import billcommons_api.routers.webhooks as webhooks_module

    victim = webhooks_module._registrable_domain("victim.github.io")
    attacker = webhooks_module._registrable_domain("attacker.github.io")
    assert victim == "victim.github.io"
    assert attacker == "attacker.github.io"
    assert victim != attacker


def test_registrable_domain_s3_region_bucket_shape_matches_actual_psl_output():
    """s3.us-east-1.amazonaws.com (the AWS bucket-in-region hostname shape
    the fix's motivating example names): the VENDORED current PSL snapshot
    (apps/api/billcommons_api/data/public_suffix_list.dat, replacing the
    package's 2019 data precisely because of gaps like this) lists the
    per-region S3 endpoint itself as a public suffix -- so each bucket
    hostname is its OWN registrable domain and two different buckets get
    two different quota buckets. Asserted as observed against the vendored
    data, per this fix's own instruction."""
    import billcommons_api.routers.webhooks as webhooks_module

    bucket_a = webhooks_module._registrable_domain("bucket-a.s3.us-east-1.amazonaws.com")
    bucket_b = webhooks_module._registrable_domain("bucket-b.s3.us-east-1.amazonaws.com")
    assert bucket_a == "bucket-a.s3.us-east-1.amazonaws.com"
    assert bucket_b == "bucket-b.s3.us-east-1.amazonaws.com"
    assert bucket_a != bucket_b


def test_registrable_domain_azure_cloudapp_region_form_matches_actual_psl_output():
    """eastus.cloudapp.azure.com (the Azure PaaS region-hostname shape):
    the CURRENT PSL deliberately has no cloudapp.azure.com entry (checked
    in the vendored snapshot while vendoring it -- Microsoft lists
    azurecontainer.io/azureedge.net/etc., not cloudapp), so get_sld falls
    back to the generic two-label rule and returns "azure.com" for any
    host under cloudapp.azure.com. That collapse is therefore
    STANDARD-ALIGNED behavior (browsers draw the same cookie boundary),
    not a gap in our matching. Asserted as observed, not assumed."""
    import billcommons_api.routers.webhooks as webhooks_module

    assert webhooks_module._registrable_domain("myvm.eastus.cloudapp.azure.com") == "azure.com"
    assert webhooks_module._registrable_domain("otherhost.westus.cloudapp.azure.com") == "azure.com"


def test_registrable_domain_plain_com_uses_the_ordinary_two_label_rule():
    import billcommons_api.routers.webhooks as webhooks_module

    assert webhooks_module._registrable_domain("sub.example.com") == "example.com"
    assert webhooks_module._registrable_domain("a.b.c.example.com") == "example.com"


def test_ip_literal_hosts_get_their_own_bucket_not_collapsed_by_trailing_octets():
    """Round-4 fix #4 (agy HIGH, opus #6): `_registrable_domain` used to run
    every hostname through the dot-label logic, which reduces an IPv4
    literal to its trailing two octets ("198.51.100.42" -> "100.42") --
    two totally unrelated subscribers whose raw IPs merely happen to share
    trailing octets would collapse into ONE shared 10-subscription quota
    bucket (cross-tenant lockout). An IP literal must bucket on the FULL
    literal address instead, checked before the dot-label logic ever runs."""
    import billcommons_api.routers.webhooks as webhooks_module

    a = webhooks_module._registrable_domain("198.51.100.42")
    b = webhooks_module._registrable_domain("203.0.100.42")
    assert a == "198.51.100.42"
    assert b == "203.0.100.42"
    assert a != b, "two unrelated IPv4 literals sharing trailing octets must not collapse"

    # Same shape for IPv6.
    v6_a = webhooks_module._registrable_domain("2001:db8::1")
    v6_b = webhooks_module._registrable_domain("2001:db9::1")
    assert v6_a != v6_b
    assert v6_a == "2001:db8::1"

    # A real hostname must still go through the ordinary dot-label logic
    # (this fix must not accidentally start treating every host as an IP).
    assert webhooks_module._registrable_domain("hooks.example.com") == "example.com"


def test_ip_literal_bucket_key_is_canonicalized_across_equivalent_spellings():
    """Verify round-9 fix #2: `_registrable_domain` returned an IP literal
    VERBATIM, un-normalized -- the same address has multiple equally-valid
    textual forms (e.g. "::1" vs its fully-expanded "0:0:0:0:0:0:0:1", or a
    bracketed "[::1]"), and each distinct spelling landed in its own quota
    bucket for what is really one host. The bucket key must be the parsed
    address's canonical compressed form, so every spelling collapses to the
    same bucket."""
    import billcommons_api.routers.webhooks as webhooks_module

    expanded = webhooks_module._registrable_domain("0:0:0:0:0:0:0:1")
    compressed = webhooks_module._registrable_domain("::1")
    assert expanded == compressed == "::1"

    # Bracketed form (defensive -- this function's real caller already
    # unbrackets via admit_url's own `.hostname`, but the helper's contract
    # should not silently depend on that).
    bracketed = webhooks_module._registrable_domain("[::1]")
    assert bracketed == "::1"

    # A public IPv6 address in expanded vs. compressed form must also
    # collapse to one bucket.
    long_form = webhooks_module._registrable_domain("2001:0db8:0000:0000:0000:0000:0000:0001")
    short_form = webhooks_module._registrable_domain("2001:db8::1")
    assert long_form == short_form == "2001:db8::1"

    # Distinct addresses must still bucket separately.
    assert webhooks_module._registrable_domain(
        "2001:db8::1"
    ) != webhooks_module._registrable_domain("2001:db8::2")


# ---------------------------------------------------------------------------
# Manage-token auth
# ---------------------------------------------------------------------------


def test_get_requires_bearer_token(client):
    created = client.post("/api/v1/webhooks", json=_body()).json()
    resp = client.get(f"/api/v1/webhooks/{created['id']}")
    assert resp.status_code == 403


def test_get_rejects_wrong_token(client):
    created = client.post("/api/v1/webhooks", json=_body()).json()
    resp = client.get(
        f"/api/v1/webhooks/{created['id']}",
        headers={"Authorization": "Bearer not-the-real-token"},
    )
    assert resp.status_code == 403


def test_get_accepts_correct_token_and_never_returns_secrets(client):
    created = client.post("/api/v1/webhooks", json=_body()).json()
    resp = client.get(
        f"/api/v1/webhooks/{created['id']}",
        headers={"Authorization": f"Bearer {created['manage_token']}"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert "manage_token" not in payload
    assert "signing_secret" not in payload
    assert payload["verified"] is False
    assert payload["active"] is True


def test_unknown_id_is_404_even_with_no_auth(client):
    resp = client.get(f"/api/v1/webhooks/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_delete_requires_correct_token_then_removes_the_row(client):
    created = client.post("/api/v1/webhooks", json=_body()).json()
    wrong = client.delete(
        f"/api/v1/webhooks/{created['id']}", headers={"Authorization": "Bearer wrong"}
    )
    assert wrong.status_code == 403

    real = client.delete(
        f"/api/v1/webhooks/{created['id']}",
        headers={"Authorization": f"Bearer {created['manage_token']}"},
    )
    assert real.status_code == 204

    gone = client.get(
        f"/api/v1/webhooks/{created['id']}",
        headers={"Authorization": f"Bearer {created['manage_token']}"},
    )
    assert gone.status_code == 404


# ---------------------------------------------------------------------------
# Reactivate modes
# ---------------------------------------------------------------------------


def test_reactivate_requires_explicit_mode(client):
    db = get_session()
    try:
        created = client.post("/api/v1/webhooks", json=_body()).json()
        db.execute(
            text(
                "UPDATE webhook_subscriptions SET active=false, "
                "disabled_reason='too_many_failures', disabled_at=now() WHERE id=:id"
            ),
            {"id": created["id"]},
        )
        db.commit()
        resp = client.post(
            f"/api/v1/webhooks/{created['id']}/reactivate",
            headers={"Authorization": f"Bearer {created['manage_token']}"},
        )
        assert resp.status_code == 409
    finally:
        db.close()


def test_reactivate_rejects_a_subscription_that_is_not_disabled(client):
    created = client.post("/api/v1/webhooks", json=_body()).json()
    resp = client.post(
        f"/api/v1/webhooks/{created['id']}/reactivate?mode=resume",
        headers={"Authorization": f"Bearer {created['manage_token']}"},
    )
    assert resp.status_code == 409


def test_reactivate_rejects_active_false_disabled_reason_none(client):
    """Round-3 fix #14: the guard is `disabled_reason is None` ALONE, not
    `disabled_reason is None and row.active` -- an `active=false,
    disabled_reason=NULL` row (a state no current code path produces, since
    every disable branch sets `disabled_reason` in the same write that sets
    `active=False`, but the fix is cheap insurance against a future one that
    forgets to) must still 409, never silently reactivate."""
    db = get_session()
    try:
        created = client.post("/api/v1/webhooks", json=_body()).json()
        db.execute(
            text("UPDATE webhook_subscriptions SET active=false WHERE id=:id"),
            {"id": created["id"]},
        )
        db.commit()
        resp = client.post(
            f"/api/v1/webhooks/{created['id']}/reactivate?mode=resume",
            headers={"Authorization": f"Bearer {created['manage_token']}"},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "webhook_not_disabled"
    finally:
        db.close()


def test_reactivate_clears_a_stale_disabled_notify_pending_but_not_created(client):
    """Round-3 fix #12: reactivating clears `notify_pending = 'disabled'`
    (send_alerts.py would otherwise still mail an auto-disable notice for a
    subscription that is active again by its next nightly run) but must
    NEVER clobber a still-pending 'created' notice, which is unrelated."""
    db = get_session()
    try:
        created = client.post("/api/v1/webhooks", json=_body()).json()

        db.execute(
            text(
                "UPDATE webhook_subscriptions SET active=false, "
                "disabled_reason='too_many_failures', disabled_at=now(), "
                "notify_pending='disabled' WHERE id=:id"
            ),
            {"id": created["id"]},
        )
        db.commit()
        resp = client.post(
            f"/api/v1/webhooks/{created['id']}/reactivate?mode=skip",
            headers={"Authorization": f"Bearer {created['manage_token']}"},
        )
        assert resp.status_code == 200
        pending_after_disabled = db.execute(
            text("SELECT notify_pending FROM webhook_subscriptions WHERE id=:id"),
            {"id": created["id"]},
        ).scalar_one()
        assert pending_after_disabled is None

        db.execute(
            text(
                "UPDATE webhook_subscriptions SET active=false, "
                "disabled_reason='too_many_failures', disabled_at=now(), "
                "notify_pending='created' WHERE id=:id"
            ),
            {"id": created["id"]},
        )
        db.commit()
        resp2 = client.post(
            f"/api/v1/webhooks/{created['id']}/reactivate?mode=skip",
            headers={"Authorization": f"Bearer {created['manage_token']}"},
        )
        assert resp2.status_code == 200
        pending_after_created = db.execute(
            text("SELECT notify_pending FROM webhook_subscriptions WHERE id=:id"),
            {"id": created["id"]},
        ).scalar_one()
        assert pending_after_created == "created", (
            "reactivate must never clobber an unrelated, still-pending "
            "'created' notice"
        )
    finally:
        db.close()


def test_reactivate_mode_resume_keeps_last_seq_mode_skip_advances_it(client):
    db = get_session()
    try:
        created = client.post("/api/v1/webhooks", json=_body()).json()
        db.execute(
            text(
                "UPDATE webhook_subscriptions SET active=false, last_seq=0, "
                "disabled_reason='too_many_failures', disabled_at=now() WHERE id=:id"
            ),
            {"id": created["id"]},
        )
        db.commit()

        headers = {"Authorization": f"Bearer {created['manage_token']}"}
        resp = client.post(
            f"/api/v1/webhooks/{created['id']}/reactivate?mode=resume", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["mode"] == "resume"
        last_seq_after_resume = db.execute(
            text("SELECT last_seq FROM webhook_subscriptions WHERE id=:id"),
            {"id": created["id"]},
        ).scalar_one()
        assert last_seq_after_resume == 0

        db.execute(
            text(
                "UPDATE webhook_subscriptions SET active=false, "
                "disabled_reason='too_many_failures', disabled_at=now() WHERE id=:id"
            ),
            {"id": created["id"]},
        )
        db.commit()
        resp2 = client.post(
            f"/api/v1/webhooks/{created['id']}/reactivate?mode=skip", headers=headers
        )
        assert resp2.status_code == 200
        assert resp2.json()["mode"] == "skip"
        last_seq_after_skip = db.execute(
            text("SELECT last_seq FROM webhook_subscriptions WHERE id=:id"),
            {"id": created["id"]},
        ).scalar_one()
        assert last_seq_after_skip >= 0
    finally:
        db.close()


def test_reactivate_409s_when_the_host_quota_is_full(client, monkeypatch):
    """Round-4 fix #2 (opus #5, grok #2): `reactivate_webhook` used to write
    `active=True` unconditionally, with NO quota check at all -- a
    subscription that had been auto-disabled for ordinary delivery failures
    (not a quota reason) could reactivate straight back into a host that had
    since filled up to its 10-verified-subscription cap in the meantime.
    10 verified-and-active subs on one host, plus an 11th that is ALSO
    verified but currently auto-disabled: reactivating the 11th must 409,
    not silently retake a seat that isn't there."""
    import billcommons_api.routers.webhooks as webhooks_module

    # Round-7 fix #2: this test's own 11 creations are exercising the
    # VERIFIED per-host cap (still 10, unchanged), not the new unverified
    # one -- but all 11 are created UNVERIFIED first (see below), and the
    # new MAX_UNVERIFIED_PER_HOST default (10) would otherwise block the
    # 11th of THOSE before this test ever gets to flip anything `verified`.
    # Raised out of the way here; this test's own subject is the verified
    # cap, not the unverified one (that has its own dedicated test above).
    monkeypatch.setattr(webhooks_module, "MAX_UNVERIFIED_PER_HOST", 999)

    # All 11 created FIRST, all still unverified -- the per-domain quota
    # (round-2 fix #7) counts only VERIFIED subs, so none of these creations
    # trip it yet (same ordering test_domain_quota_is_enforced_per_host
    # already relies on).
    host = _fresh_host()
    for i in range(10):
        headers = {"x-forwarded-for": f"198.51.102.{i + 1}"}
        resp = client.post(
            "/api/v1/webhooks",
            json=_body(url=f"https://{host}/hook/{i}"),
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
    eleventh = client.post(
        "/api/v1/webhooks",
        json=_body(url=f"https://{host}/hook/10"),
        headers={"x-forwarded-for": "198.51.102.200"},
    ).json()

    db = get_session()
    try:
        # The first 10 verify and stay active -- they now hold the host's
        # entire quota. The 11th ALSO verifies (it did, once, before it was
        # auto-disabled for an ordinary delivery failure -- NOT a quota
        # reason) but is disabled and inactive right now, so it does not
        # itself count against the 10 above.
        db.execute(
            text(
                "UPDATE webhook_subscriptions SET verified=true "
                "WHERE host = :h AND id != :eleventh_id"
            ),
            {"h": host, "eleventh_id": eleventh["id"]},
        )
        db.execute(
            text(
                "UPDATE webhook_subscriptions SET verified=true, active=false, "
                "disabled_reason='too_many_failures', disabled_at=now() WHERE id=:id"
            ),
            {"id": eleventh["id"]},
        )
        db.commit()

        resp = client.post(
            f"/api/v1/webhooks/{eleventh['id']}/reactivate?mode=resume",
            headers={"Authorization": f"Bearer {eleventh['manage_token']}"},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "webhook_quota_exceeded"

        still_disabled = db.execute(
            text("SELECT active FROM webhook_subscriptions WHERE id=:id"),
            {"id": eleventh["id"]},
        ).scalar_one()
        assert still_disabled is False, "a 409'd reactivate must not partially apply"
    finally:
        db.close()


def test_reactivate_409s_when_unverified_per_host_quota_is_full(client, monkeypatch):
    """Round-7 fix #2 (replaces round-5 fix #5's global-cap version):
    reactivating an UNVERIFIED, auto-disabled row re-arms its challenge and
    puts it back in the unverified pool for ITS domain -- the now-per-host
    `MAX_UNVERIFIED_PER_HOST` must be re-checked under the same advisory
    lock, same as the verified branch's own host/global recounts, or a
    reactivate can re-inflate that domain's pool past its cap with no check
    at all."""
    import billcommons_api.routers.webhooks as webhooks_module

    monkeypatch.setattr(webhooks_module, "MAX_UNVERIFIED_PER_HOST", 1)

    host = _fresh_host()
    created = client.post("/api/v1/webhooks", json=_body(url=f"https://{host}/hook/a")).json()
    db = get_session()
    try:
        db.execute(
            text(
                "UPDATE webhook_subscriptions SET active=false, verified=false, "
                "challenge_token=NULL, disabled_reason='domain_quota_exceeded', "
                "disabled_at=now() WHERE id=:id"
            ),
            {"id": created["id"]},
        )
        db.commit()
    finally:
        db.close()

    # A second, DISTINCT unverified-active row on the SAME domain eats the
    # one remaining slot (cap of 1) so the reactivate below is what tips it
    # over.
    filler = client.post("/api/v1/webhooks", json=_body(url=f"https://{host}/hook/b")).json()
    assert filler["verified"] is False

    resp = client.post(
        f"/api/v1/webhooks/{created['id']}/reactivate?mode=resume",
        headers={"Authorization": f"Bearer {created['manage_token']}"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "webhook_quota_exceeded"

    db = get_session()
    try:
        still_disabled = db.execute(
            text("SELECT active FROM webhook_subscriptions WHERE id=:id"),
            {"id": created["id"]},
        ).scalar_one()
        assert still_disabled is False, "a 409'd reactivate must not partially apply"
    finally:
        db.close()


def test_reactivate_regenerates_challenge_token_and_resets_gc_clock_when_unverified(client):
    """Round-4 fix #3 (kimi Finding 1 -- its sole block reason -- + codex
    HIGH #2): a domain/global-quota disable AT PROMOTION time (see
    workers/webhooks/dispatch_webhooks.py's `_attempt_challenge`) leaves the
    row `verified=False` with `challenge_token` already cleared to NULL.
    Reactivating that exact row must (a) hand it a REAL new challenge token
    (never leave it NULL -- a null token crashes the next challenge
    attempt's `sub.challenge_token.encode()`) and (b) reset the 24h
    challenge-GC clock, which `run_challenges` keys off `created_at` (READ
    from that function before writing this test/fix) -- otherwise a
    reactivation close to the original 24h mark gets silently hard-deleted
    within minutes. Proven end-to-end: after reactivating, the dispatcher's
    own `_attempt_challenge` (imported directly, same sys.path trick
    test_webhooks_payload_contract.py already uses) is run against a fake
    receiver that echoes the NEW token back -- it must promote to verified,
    which is only possible if reactivate actually put a real, correct token
    on the row."""
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    WORKERS_WEBHOOKS = Path(__file__).resolve().parents[3] / "workers" / "webhooks"
    sys.path.insert(0, str(WORKERS_WEBHOOKS))
    import dispatch_webhooks as dw
    from billcommons_schema.models import WebhookSubscription

    created = client.post("/api/v1/webhooks", json=_body()).json()
    db = get_session()
    try:
        old_created_at = db.execute(
            text("SELECT created_at FROM webhook_subscriptions WHERE id=:id"),
            {"id": created["id"]},
        ).scalar_one()

        # Simulate exactly what _attempt_challenge's domain/global-quota
        # disable branch writes: verified stays False, challenge_token goes
        # NULL, active goes False -- and back-date created_at so the "reset
        # the GC clock" half of the fix is actually exercised, not
        # accidentally passing because created_at was already ~now.
        db.execute(
            text(
                "UPDATE webhook_subscriptions SET active=false, verified=false, "
                "challenge_token=NULL, disabled_reason='domain_quota_exceeded', "
                "disabled_at=now(), created_at = now() - interval '1 hour' "
                "WHERE id=:id"
            ),
            {"id": created["id"]},
        )
        db.commit()

        resp = client.post(
            f"/api/v1/webhooks/{created['id']}/reactivate?mode=resume",
            headers={"Authorization": f"Bearer {created['manage_token']}"},
        )
        assert resp.status_code == 200, resp.text

        row = db.get(WebhookSubscription, uuid.UUID(created["id"]))
        db.refresh(row)
        assert row.challenge_token is not None and len(row.challenge_token) > 10, (
            "a reactivated unverified sub must get a REAL challenge token, "
            "never leave it NULL (fix #3)"
        )
        assert row.created_at > old_created_at, (
            "created_at must be bumped forward -- the 24h challenge GC keys "
            "off created_at, and this row's clock was deliberately set an "
            "hour in the past to prove the reset actually happens"
        )
        assert row.consecutive_failures == 0
        assert row.next_attempt_at is None

        # End-to-end: the dispatcher's real challenge attempt, against a
        # fake client that echoes the NEW token exactly, must promote.
        class _EchoingClient:
            def fetch(self, url, *, method, body, headers, require_body=True):
                import json as _json

                token = _json.loads(body)["challenge"]
                return SimpleNamespace(status=200, headers={}, body=token.encode())

        import datetime as _dt

        dw._attempt_challenge(db, _EchoingClient(), row, now=_dt.datetime.now(_dt.timezone.utc))
        db.refresh(row)
        assert row.verified is True, (
            "with a real token restored and capacity available, the next "
            "challenge attempt must promote the subscription"
        )
    finally:
        db.close()


def test_reactivate_resets_challenge_attempts_when_unverified(client):
    """Round-7 fix #7 (opus LOW #6): a fresh 24h challenge-GC window
    previously still inherited the OLD `challenge_attempts` count --
    `backoff_delay` is a function of that counter, so a sub reactivated
    after e.g. 10 prior attempts started its brand-new 24h window already
    backed off to the 6h max-backoff cap, getting only ~4 more attempts in
    that window instead of the ~14 a genuinely fresh challenge gets.
    `challenge_attempts` must reset to 0 alongside the token/clock reset
    (fix #3's own three-part requirement, verified by the test above)."""
    from billcommons_schema.models import WebhookSubscription

    created = client.post("/api/v1/webhooks", json=_body()).json()
    db = get_session()
    try:
        db.execute(
            text(
                "UPDATE webhook_subscriptions SET active=false, verified=false, "
                "challenge_token=NULL, challenge_attempts=10, "
                "disabled_reason='domain_quota_exceeded', disabled_at=now() "
                "WHERE id=:id"
            ),
            {"id": created["id"]},
        )
        db.commit()

        resp = client.post(
            f"/api/v1/webhooks/{created['id']}/reactivate?mode=resume",
            headers={"Authorization": f"Bearer {created['manage_token']}"},
        )
        assert resp.status_code == 200, resp.text

        row = db.get(WebhookSubscription, uuid.UUID(created["id"]))
        db.refresh(row)
        assert row.challenge_attempts == 0, (
            "challenge_attempts must reset to 0 on an unverified reactivate, "
            "or the fresh 24h GC window inherits the old backoff schedule"
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# No outbound HTTP, ever, anywhere in this router
# ---------------------------------------------------------------------------


def test_router_source_never_calls_the_ssrf_transport():
    """Static companion to the dynamic socket-level test below: the router
    imports `admit_url` from billcommons_shared.safe_http (pure string
    parsing, no network -- used to validate a subscribed url's shape) but
    must never construct a client or call `.fetch(...)`, which is the only
    thing in that module that actually opens a connection."""
    import pathlib

    import billcommons_api.routers.webhooks as webhooks_module

    src = pathlib.Path(webhooks_module.__file__).read_text()
    assert ".fetch(" not in src
    assert "SafeHttpClient" not in src
    assert "new_safe_http_client" not in src


def test_router_never_opens_an_outbound_socket(client, monkeypatch):
    """Monkeypatch socket.socket.connect for the duration of one request
    cycle -- any attempt at outbound HTTP anywhere reachable from this
    router (directly, or via an import it pulls in) trips it and fails the
    test. The DB connection itself goes over a socket too, so this can't be
    a blanket "no connect() at all" -- it specifically flags a connect to
    port 443/80 (an HTTP(S) destination), which the DB driver never uses."""
    original_connect = socket.socket.connect

    def guarded_connect(self, address, *args, **kwargs):
        try:
            port = address[1] if isinstance(address, tuple) else None
        except (TypeError, IndexError):
            port = None
        if port in (80, 443):
            raise AssertionError(
                f"webhooks router attempted an outbound HTTP(S) connect to {address!r} "
                "-- this router must be DB-only."
            )
        return original_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)

    create_resp = client.post("/api/v1/webhooks", json=_body())
    assert create_resp.status_code == 201
    created = create_resp.json()

    client.get(
        f"/api/v1/webhooks/{created['id']}",
        headers={"Authorization": f"Bearer {created['manage_token']}"},
    )
    client.delete(
        f"/api/v1/webhooks/{created['id']}",
        headers={"Authorization": f"Bearer {created['manage_token']}"},
    )
