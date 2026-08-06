"""The webhooks-lifecycle piggyback on the nightly alerts sender.

Uses fake DB-API objects (not the live DB) -- this script runs outside any
installed package's virtualenv by design (see its own module docstring) and
has no pytest/conftest DB fixture of its own; these tests exercise its pure
control flow (probe -> query -> send -> clear) against a scriptable fake
connection/cursor, the same shape psycopg's real Connection/Cursor context
managers present.
"""
from __future__ import annotations

import secrets

import send_alerts


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self._conn.executed.append((query.strip().split()[0], query, params))
        if "information_schema.tables" in query:
            self._result = [(1,)] if self._conn.table_present else []
        elif query.strip().startswith("SELECT id, email, kind, target"):
            self._result = list(self._conn.pending_rows)
        else:
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return self._result


class _FakeConn:
    def __init__(self, *, table_present: bool, pending_rows: list[tuple]):
        self.table_present = table_present
        self.pending_rows = pending_rows
        self.executed: list[tuple] = []
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1


def test_noop_when_table_absent():
    conn = _FakeConn(table_present=False, pending_rows=[])
    sent, failed = send_alerts.drain_webhook_notifications(conn, get_api_key=lambda: "unused")
    assert (sent, failed) == (0, 0)
    # Only the probe query ran -- proves it stops there and never queries a
    # table that migration 0012 might not have created yet.
    assert len(conn.executed) == 1
    assert "information_schema.tables" in conn.executed[0][1]


def test_table_probe_is_schema_qualified():
    """Verify round-9 fix #3: an unqualified `information_schema.tables`
    probe can match a same-named table in ANOTHER schema (e.g. a
    differently-scoped search_path, or a same-named table some other
    schema happens to have), wrongly treating migration 0012 as applied.
    Same query-shape assertion this repo already uses for the identical
    fix in dispatch_webhooks.py's `_creation_events_table_exists`."""
    conn = _FakeConn(table_present=True, pending_rows=[])
    send_alerts.drain_webhook_notifications(conn, get_api_key=lambda: "unused")
    probe_query = conn.executed[0][1]
    assert "table_schema = current_schema()" in probe_query


def test_drains_pending_notifications_and_clears_the_flag():
    sub_id = "11111111-1111-1111-1111-111111111111"
    conn = _FakeConn(
        table_present=True,
        pending_rows=[(sub_id, "sub@example.com", "topic", "cybersecurity", "created", None)],
    )
    sent_messages = []

    def fake_get_api_key():
        return "fake-key"

    def fake_resend_send_plain(api_key, to_addr, subject, html, text_body):
        sent_messages.append((api_key, to_addr, subject))
        return "msg-123"

    orig = send_alerts.resend_send_plain
    send_alerts.resend_send_plain = fake_resend_send_plain
    try:
        sent, failed = send_alerts.drain_webhook_notifications(conn, fake_get_api_key)
    finally:
        send_alerts.resend_send_plain = orig

    assert (sent, failed) == (1, 0)
    assert sent_messages == [("fake-key", "sub@example.com", "[Bill Commons] Webhook subscription created")]
    update_calls = [e for e in conn.executed if e[0] == "UPDATE"]
    assert len(update_calls) == 1
    # (id, notify_pending) -- fix #9: the clear is a CAS on the value read.
    assert update_calls[0][2] == (sub_id, "created")
    assert conn.commits == 1


def test_dry_run_sends_nothing_and_never_clears_the_flag():
    sub_id = "22222222-2222-2222-2222-222222222222"
    conn = _FakeConn(
        table_present=True,
        pending_rows=[(sub_id, "sub2@example.com", "jurisdiction", "FL", "disabled", "too_many_failures")],
    )
    sent, failed = send_alerts.drain_webhook_notifications(
        conn, get_api_key=lambda: (_ for _ in ()).throw(AssertionError("must not fetch a key in dry-run")),
        dry_run=True,
    )
    assert (sent, failed) == (0, 0)
    update_calls = [e for e in conn.executed if e[0] == "UPDATE"]
    assert update_calls == []


def test_a_failed_send_does_not_clear_the_flag_and_does_not_stop_the_run():
    good_id = "33333333-3333-3333-3333-333333333333"
    bad_id = "44444444-4444-4444-4444-444444444444"
    conn = _FakeConn(
        table_present=True,
        pending_rows=[
            (bad_id, "bad@example.com", "topic", "cryptocurrency", "created", None),
            (good_id, "good@example.com", "topic", "cryptocurrency", "created", None),
        ],
    )

    def flaky_resend_send_plain(api_key, to_addr, subject, html, text_body):
        if to_addr == "bad@example.com":
            raise RuntimeError("simulated send failure")
        return "msg-ok"

    orig = send_alerts.resend_send_plain
    send_alerts.resend_send_plain = flaky_resend_send_plain
    try:
        sent, failed = send_alerts.drain_webhook_notifications(conn, get_api_key=lambda: "k")
    finally:
        send_alerts.resend_send_plain = orig

    assert (sent, failed) == (1, 1)
    update_calls = [e for e in conn.executed if e[0] == "UPDATE"]
    assert len(update_calls) == 1
    assert update_calls[0][2] == (good_id, "created")


def test_render_created_notice_says_verified_not_upcoming():
    """Fix #10: the 'created' notice is only ever sent AFTER the dispatcher's
    verification challenge already succeeded (notify_pending='created' is
    set in the accepted branch of _attempt_challenge, never at subscribe
    time) -- the wording must say the endpoint is verified and delivery has
    begun, never that a challenge is still upcoming/about to be sent."""
    _subj, html, text_body = send_alerts.render_webhook_notice(
        "created", "sub-id", "topic:cybersecurity", None
    )
    for blob in (html, text_body):
        lowered = blob.lower()
        assert "verified" in lowered or "already answered" in lowered
        assert "delivery" in lowered or "deliver" in lowered
        assert "will be posted" not in lowered
        assert "will be sent" not in lowered
        assert "shortly" not in lowered


def test_clear_is_a_compare_and_swap_on_the_value_read():
    """Round-2 fix #9: the UPDATE that clears `notify_pending` must be a
    CAS on the exact value this loop read (`WHERE id = %s AND
    notify_pending = %s`), not a bare `WHERE id = %s` -- otherwise a
    dispatcher flip between this script's SELECT and its UPDATE (e.g.
    'created' -> 'disabled') would clobber the newer, not-yet-sent notice
    instead of leaving it for the next run."""
    sub_id = "55555555-5555-5555-5555-555555555555"
    conn = _FakeConn(
        table_present=True,
        pending_rows=[(sub_id, "sub3@example.com", "topic", "cybersecurity", "created", None)],
    )

    def fake_resend_send_plain(api_key, to_addr, subject, html, text_body):
        return "msg-cas"

    orig = send_alerts.resend_send_plain
    send_alerts.resend_send_plain = fake_resend_send_plain
    try:
        sent, failed = send_alerts.drain_webhook_notifications(conn, get_api_key=lambda: "k")
    finally:
        send_alerts.resend_send_plain = orig

    assert (sent, failed) == (1, 0)
    update_calls = [e for e in conn.executed if e[0] == "UPDATE"]
    assert len(update_calls) == 1
    _kind, query, params = update_calls[0]
    assert "notify_pending = NULL" in query.split("SET", 1)[1].split("WHERE")[0], (
        "SET clause must still null the column"
    )
    assert "notify_pending = %s" in query.split("WHERE", 1)[1], (
        "WHERE clause must condition on the value read (CAS), not just id"
    )
    assert params == (sub_id, "created")


def test_render_created_disabled_notice_combines_both_facts():
    """Round-3 fix #13: a subscription that verified and then auto-disabled
    within the same window (before this nightly run's previous pass ever
    drained the pending 'created' notice) must render ONE combined message
    mentioning BOTH facts, never silently just the disable."""
    subj, html, text_body = send_alerts.render_webhook_notice(
        "created_disabled", "sub-id", "topic:cybersecurity", "too_many_failures"
    )
    for blob in (subj, html, text_body):
        lowered = blob.lower()
        assert "verif" in lowered, "must still mention it verified"
        assert "disabl" in lowered, "must still mention it was disabled"
    assert "too_many_failures" in html
    assert "reactivate" in html


def test_drain_sends_the_combined_notice_for_created_disabled_rows():
    sub_id = "66666666-6666-6666-6666-666666666666"
    conn = _FakeConn(
        table_present=True,
        pending_rows=[(sub_id, "combo@example.com", "topic", "cybersecurity", "created_disabled", "gone")],
    )
    sent_messages = []

    def fake_resend_send_plain(api_key, to_addr, subject, html, text_body):
        sent_messages.append((to_addr, subject))
        return "msg-combo"

    orig = send_alerts.resend_send_plain
    send_alerts.resend_send_plain = fake_resend_send_plain
    try:
        sent, failed = send_alerts.drain_webhook_notifications(conn, get_api_key=lambda: "k")
    finally:
        send_alerts.resend_send_plain = orig

    assert (sent, failed) == (1, 0)
    assert sent_messages == [("combo@example.com", "[Bill Commons] Webhook verified, then auto-disabled")]
    update_calls = [e for e in conn.executed if e[0] == "UPDATE"]
    assert update_calls[0][2] == (sub_id, "created_disabled")


def test_render_webhook_notice_never_mentions_a_secret_or_token_value():
    """Kimi #4: the original version of this test checked the literal string
    "signing_secret" (WITH the underscore) against a haystack that had every
    underscore stripped out first -- `"signing_secret" not in
    blob.lower().replace("_", "")` can NEVER be False (the needle contains a
    character the haystack can never contain after that `.replace`), so the
    assertion passed no matter what `render_webhook_notice` actually
    rendered. Fixed two ways:
      (1) the label check now runs against the UN-stripped blob, with the
          exact label text (no `.replace("_", "")` at all).
      (2) a genuine VALUE check -- realistic secret-shaped token strings (the
          same length/alphabet `secrets.token_urlsafe(32)`/`(24)` actually
          produce elsewhere in this codebase) must not appear in the
          rendered output either. `render_webhook_notice`'s own signature
          (kind, sub_id, scope, disabled_reason) has no parameter a real
          secret could ever flow through today -- this proves that by
          construction and would catch a future change that added one and
          interpolated it into the template.
    """
    fake_signing_secret = secrets.token_urlsafe(32)
    fake_manage_token = secrets.token_urlsafe(32)

    _subj, html, text_body = send_alerts.render_webhook_notice(
        "created", "sub-id", "topic:cybersecurity", None
    )
    for blob in (html, text_body):
        # The bare word "manage_token" is fine -- the copy legitimately
        # tells the reader "using the manage_token you were given at
        # creation" without repeating it. What must never appear is the
        # internal STORAGE artifact name (`manage_token_hash`, the DB
        # column -- see migration 0012's docstring) or the HMAC key label,
        # or either secret's actual VALUE.
        assert "signing_secret" not in blob
        assert "manage_token_hash" not in blob
        assert fake_signing_secret not in blob
        assert fake_manage_token not in blob

    _subj2, html2, text_body2 = send_alerts.render_webhook_notice(
        "disabled", "sub-id", "jurisdiction:FL", "too_many_failures"
    )
    for blob in (html2, text_body2):
        assert "signing_secret" not in blob
        assert "manage_token_hash" not in blob
        assert fake_signing_secret not in blob
        assert fake_manage_token not in blob
    assert "too_many_failures" in html2
    assert "reactivate" in html2
