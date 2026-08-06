"""Tests for workers/webhooks/dispatch_webhooks.py.

Split in two halves by what they need:

  * Pure-function tests (signing, backoff math, payload shape, taxonomy,
    target parsing) need neither a DB nor a network and always run.
  * DB-backed tests exercise the challenge/drain/rotation/at-least-once
    machinery against the live Railway DB this whole suite runs against (see
    apps/api/tests/conftest.py's own docstring for the repo-wide convention)
    plus a real local TLS receiver (same _local_https_server pattern as
    packages/shared/tests/test_safe_http.py). They are SKIPPED with a clear
    reason if migration 0012's tables are not present -- applying that
    migration to prod is an orchestrator ship-gate (spec §8), not something
    this test file may do itself.
"""
from __future__ import annotations

import datetime as dt
import http.server
import json
import random
import ssl
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import text

import dispatch_webhooks as dw
from billcommons_schema.models import Bill, BillEvent, Jurisdiction, Session as SessionModel, WebhookDelivery, WebhookSubscription
from billcommons_shared.db import get_session

TEST_HOSTNAME = "webhook-dispatch-test.billcommons.internal"


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


def test_signature_vector():
    """Fixed secret/timestamp/body -> known hex, per §7. The expected value
    is a hand-computed vector (not merely a re-derivation via the same
    hmac.new call the code under test makes), so a change to the signed
    string's format (e.g. dropping the "." separator, or signing timestamp
    and body in the wrong order) would be caught rather than silently
    matching a self-referential re-computation."""
    sig = dw.sign_body("test-secret", 1735689600, b'{"hello":"world"}')
    # Hand-computed vector (via `hmac.new(b"test-secret",
    # b"1735689600." + body, hashlib.sha256).hexdigest()` run independently
    # of dispatch_webhooks.py, not a re-derivation using the code under
    # test) -- pins the exact signed-string format: f"{timestamp}." prefixed
    # directly onto the raw body bytes, HMAC-SHA256 keyed on the secret.
    assert sig == "sha256=f6a1c3267600f4b02dc7632569cd4c5406cff3438e03f0059701b7531b4b6861"

    # A different secret, timestamp, or body must all change the signature --
    # pins that every documented input actually participates in the MAC
    # (a bug that dropped the timestamp from the signed string, for
    # instance, would leave these equal).
    other_secret = dw.sign_body("different-secret", 1735689600, b'{"hello":"world"}')
    other_timestamp = dw.sign_body("test-secret", 1735689601, b'{"hello":"world"}')
    other_body = dw.sign_body("test-secret", 1735689600, b'{"hello":"there"}')
    assert len({sig, other_secret, other_timestamp, other_body}) == 4

    # Deterministic: signing the identical inputs again reproduces the exact
    # same signature (an HMAC keyed on wall-clock time or randomness would
    # fail this).
    assert dw.sign_body("test-secret", 1735689600, b'{"hello":"world"}') == sig


def test_backoff_delay_bounds_and_monotonic_growth():
    rng = random.Random(0)
    d0 = dw.backoff_delay(0, rng=rng)
    d3 = dw.backoff_delay(3, rng=rng)
    d_big = dw.backoff_delay(50, rng=rng)  # must cap, not overflow
    assert dt.timedelta(seconds=0) <= d0 <= dt.timedelta(minutes=1.2)
    assert d3 <= dw.MAX_BACKOFF
    assert d_big <= dw.MAX_BACKOFF


def test_backoff_delay_jitter_is_within_20_percent():
    rng = random.Random(42)
    for n in (1, 2, 4):
        base_minutes = min(2**n, dw.MAX_BACKOFF.total_seconds() / 60)
        delay = dw.backoff_delay(n, rng=rng)
        low = dt.timedelta(minutes=base_minutes * 0.8)
        high = dt.timedelta(minutes=base_minutes * 1.2)
        assert low <= delay <= high, (n, delay, low, high)


def test_retry_after_delay_parses_and_caps():
    assert dw.retry_after_delay("30") == dt.timedelta(seconds=30)
    assert dw.retry_after_delay(str(int(dw.MAX_BACKOFF.total_seconds()) + 999)) == dw.MAX_BACKOFF
    assert dw.retry_after_delay(None) is None
    assert dw.retry_after_delay("not-a-number") is None
    # Verify round-5 fix #7 (opus #4): a non-positive Retry-After ("0" or a
    # negative value) is unparseable, same as `None` -- NOT
    # `timedelta(seconds=0)` (the pre-fix pinned expectation this test used
    # to assert), which would make the sub eligible again on the very next
    # tick, hammered every ~120s for the whole 72h auto-disable window.
    assert dw.retry_after_delay("-5") is None
    assert dw.retry_after_delay("0") is None


@pytest.mark.parametrize(
    "status,expected_class,expected_disable",
    [
        (200, None, False),
        (204, None, False),
        (410, "http_4xx", True),
        (404, "http_4xx", False),
        (429, "http_429", False),
        # Round-4 fix #7: a 4xx NOT in NON_RETRYABLE_4XX gets its own class,
        # distinct from the hard-4xx 'http_4xx' above, so it never pads (or
        # interrupts) the 3-strike streak.
        (408, "http_4xx_retryable", False),
        (425, "http_4xx_retryable", False),
        (500, "http_5xx", False),
        (503, "http_5xx", False),
    ],
)
def test_classify_http_status(status, expected_class, expected_disable):
    assert dw.classify_http_status(status) == (expected_class, expected_disable)


def test_classify_transport_error_maps_every_safe_http_class():
    from billcommons_shared.safe_http import (
        ConnectionFailure,
        DnsFailure,
        SsrfRejected,
        TimeoutFailure,
        TlsFailure,
        TooLarge,
        TransportFailure,
    )

    for exc_cls in (
        DnsFailure, SsrfRejected, TimeoutFailure, TlsFailure, TooLarge,
        ConnectionFailure, TransportFailure,
    ):
        exc = exc_cls("reason")
        assert dw.classify_transport_error(exc) == exc.error_class


# ---------------------------------------------------------------------------
# Round-2 fix #5: 429 gets its own error class, distinct from http_4xx, so
# it never pads/interrupts the hard-4xx 3-strike streak.
# ---------------------------------------------------------------------------


def test_429_then_429_then_404_does_not_disable_but_three_404s_does():
    now = dt.datetime.now(dt.timezone.utc)

    def fresh_sub():
        return SimpleNamespace(
            id=uuid.uuid4(), url="https://example.test/hook", signing_secret="s3cr3t",
            last_seq=1, consecutive_failures=0, failing_since=None, next_attempt_at=None,
            active=True, disabled_reason=None, disabled_at=None, notify_pending=None,
            last_status=None, last_error=None, last_attempt_at=None, last_success_at=None,
        )

    # 429, 429, 404 -- must NOT disable.
    sub_a = fresh_sub()
    db_a = _FakeDb()
    statuses = iter([429, 429, 404])

    class _MultiStatusClient:
        def fetch(self, *a, **kw):
            return SimpleNamespace(status=next(statuses), headers={}, body=b"")

    client_a = _MultiStatusClient()
    for _ in range(3):
        sub_a.last_seq = 0  # re-present the same one event each drain call
        dw._drain_one(
            db_a, client_a, sub_a, watermark_seq=1, watermark_dt=now,
            deadline=time.monotonic() + 10, fetch_batch=lambda db, sub, *, watermark_seq: (
                _fake_events(1, now), False, 1
            ),
        )
    assert sub_a.active is True, "429, 429, 404 must not trip the hard-4xx 3-strike disable"
    assert sub_a.last_error == "http_4xx"
    assert sub_a.consecutive_failures == 1, "the 404 starts a fresh streak after the http_429s"

    # 404, 404, 404 -- must disable on the third.
    sub_b = fresh_sub()
    db_b = _FakeDb()
    statuses_b = iter([404, 404, 404])

    class _AllHardFourOhFourClient:
        def fetch(self, *a, **kw):
            return SimpleNamespace(status=next(statuses_b), headers={}, body=b"")

    client_b = _AllHardFourOhFourClient()
    for _ in range(3):
        sub_b.last_seq = 0
        dw._drain_one(
            db_b, client_b, sub_b, watermark_seq=1, watermark_dt=now,
            deadline=time.monotonic() + 10, fetch_batch=lambda db, sub, *, watermark_seq: (
                _fake_events(1, now), False, 1
            ),
        )
    assert sub_b.active is False, "three consecutive real hard-4xx failures must disable"
    assert sub_b.disabled_reason == "non_retryable_client_error"


def test_408_then_408_then_404_does_not_disable():
    """Round-4 fix #7 (grok #3): before this fix, 408 (retryable) and 404
    (a genuine hard-4xx) shared the SAME 'http_4xx' class -- 408, 408, 404
    saw no class change across all three calls and disabled after the
    THIRD call even though only the last one was actually a hard-4xx. With
    408 reclassified to its own 'http_4xx_retryable' class, the 404 starts
    a fresh streak of 1 and must not disable."""
    now = dt.datetime.now(dt.timezone.utc)
    sub = SimpleNamespace(
        id=uuid.uuid4(), url="https://example.test/hook", signing_secret="s3cr3t",
        last_seq=1, consecutive_failures=0, failing_since=None, next_attempt_at=None,
        active=True, disabled_reason=None, disabled_at=None, notify_pending=None,
        last_status=None, last_error=None, last_attempt_at=None, last_success_at=None,
    )
    db = _FakeDb()
    statuses = iter([408, 408, 404])

    class _MultiStatusClient:
        def fetch(self, *a, **kw):
            return SimpleNamespace(status=next(statuses), headers={}, body=b"")

    client = _MultiStatusClient()
    for _ in range(3):
        sub.last_seq = 0  # re-present the same one event each drain call
        dw._drain_one(
            db, client, sub, watermark_seq=1, watermark_dt=now,
            deadline=time.monotonic() + 10, fetch_batch=lambda db, sub, *, watermark_seq: (
                _fake_events(1, now), False, 1
            ),
        )
    assert sub.active is True, "408, 408, 404 must not trip the hard-4xx 3-strike disable"
    assert sub.last_error == "http_4xx", "the 404 itself is still classified http_4xx"
    assert sub.consecutive_failures == 1, "the class change from http_4xx_retryable resets the streak to 1"


def test_parse_topic_target_scoped_and_unscoped():
    assert dw.parse_topic_target("cybersecurity") == ("cybersecurity", None)
    assert dw.parse_topic_target("cybersecurity:NC") == ("cybersecurity", "NC")


def test_scope_clause_unknown_topic_returns_none():
    sub = SimpleNamespace(kind="topic", target="not-a-real-topic")
    assert dw.scope_clause(sub) is None


def test_scope_clause_unparseable_bill_ids_returns_none():
    sub = SimpleNamespace(kind="bills", target="not-a-uuid,also-not")
    assert dw.scope_clause(sub) is None


def test_kind_clause_none_means_no_filter():
    sub = SimpleNamespace(event_kinds=None)
    assert dw.kind_clause(sub) is None
    sub2 = SimpleNamespace(event_kinds="")
    assert dw.kind_clause(sub2) is None


def test_build_delivery_payload_shape_and_cursor_roundtrip():
    """Pure, no DB: uses SimpleNamespace stand-ins the same way
    apps/api/tests/test_feeds_atom.py's lone-surrogate test does, to prove
    the SHAPE without needing a live row."""
    from billcommons_shared.cursor import decode_cursor

    now = dt.datetime.now(dt.timezone.utc)
    bill = SimpleNamespace(
        id=uuid.uuid4(), jurisdiction_id=uuid.uuid4(), session_id=uuid.uuid4(),
        chamber="lower", identifier="HB 1", identifier_norm="HB 1", title="An act",
        short_title=None, bill_type=None, status="introduced", status_date=None,
        introduced_date=None, latest_action_text=None, latest_action_date=None,
        source_url=None, updated_at=now,
    )
    event = SimpleNamespace(seq=42, kind="status", detail="in_committee -> passed", changed_at=now)
    delivery_id = uuid.uuid4()

    raw = dw.build_delivery_payload(
        events=[(event, bill, "NC", "2026 Session")],
        watermark_seq=100,
        watermark_changed_at=now,
        has_more=False,
        delivery_id=delivery_id,
    )
    payload = json.loads(raw)
    assert payload["api_version"] == "1"
    assert payload["has_more"] is False
    assert payload["delivery_id"] == str(delivery_id)
    assert decode_cursor(payload["cursor"]) == 42
    assert len(payload["events"]) == 1
    item = payload["events"][0]
    assert item["id"] == "42"  # STABLE dedupe key -- see build_delivery_payload's docstring
    assert decode_cursor(item["cursor"]) == 42
    assert item["kind"] == "status"
    assert item["bill"]["identifier"] == "HB 1"
    assert item["bill"]["jurisdiction_abbreviation"] == "NC"
    assert item["bill"]["session_identifier"] == "2026 Session"


# ---------------------------------------------------------------------------
# Round-3 fix #10: challenges get a GUARANTEED minimum slice even when
# deliveries already consumed the whole tick budget.
# ---------------------------------------------------------------------------


def test_challenge_deadline_has_a_floor_even_when_tick_deadline_already_passed():
    now = time.monotonic()
    tick_deadline_already_past = now - 5.0  # deliveries burned through the whole tick
    deadline = dw._challenge_deadline(now=now, tick_deadline=tick_deadline_already_past)
    assert deadline >= now + dw.CHALLENGE_MIN_SECONDS - 0.01, (
        "a tick_deadline already in the past must not collapse the "
        "challenge slice to zero (fix #10)"
    )


def test_challenge_deadline_still_respects_the_normal_budget_when_tick_has_room():
    now = time.monotonic()
    roomy_tick_deadline = now + 100.0
    deadline = dw._challenge_deadline(now=now, tick_deadline=roomy_tick_deadline)
    assert deadline == pytest.approx(now + dw.CHALLENGE_BUDGET_SECONDS, abs=0.01), (
        "with plenty of tick budget left, the deadline must be the normal "
        "CHALLENGE_BUDGET_SECONDS slice, not the floor"
    )


# ---------------------------------------------------------------------------
# Round-3 fix #13: an unsent 'created' notice must survive an auto-disable
# in the same window -- combined into 'created_disabled', never silently
# overwritten to plain 'disabled'.
# ---------------------------------------------------------------------------


def test_notify_disabled_preserves_a_pending_created_notice():
    sub_with_created = SimpleNamespace(notify_pending="created")
    dw._notify_disabled(sub_with_created)
    assert sub_with_created.notify_pending == "created_disabled"


def test_notify_disabled_is_plain_disabled_when_nothing_was_pending():
    sub_no_notice = SimpleNamespace(notify_pending=None)
    dw._notify_disabled(sub_no_notice)
    assert sub_no_notice.notify_pending == "disabled"


def test_notify_disabled_preserves_created_disabled_on_a_second_disable_cycle():
    """Round-5 fix #10 (deepseek #3): a SECOND disable (reactivate, then
    auto-disable again before send_alerts.py's next nightly run ever drains
    the still-pending 'created_disabled' notice from the FIRST cycle) must
    not fall through to the plain 'disabled' branch and lose the fact that
    this subscription ever verified at all."""
    sub_already_combined = SimpleNamespace(notify_pending="created_disabled")
    dw._notify_disabled(sub_already_combined)
    assert sub_already_combined.notify_pending == "created_disabled", (
        "a second disable cycle must not downgrade an already-combined "
        "'created_disabled' notice back to plain 'disabled'"
    )


def test_maybe_auto_disable_combines_with_a_pending_created_notice():
    now = dt.datetime.now(dt.timezone.utc)
    old_failing_since = now - dt.timedelta(hours=73)
    sub = SimpleNamespace(
        failing_since=old_failing_since, active=True, disabled_reason=None,
        disabled_at=None, notify_pending="created",
    )
    disabled = dw._maybe_auto_disable(sub, now=now, reason="too_many_failures")
    assert disabled is True
    assert sub.notify_pending == "created_disabled"


def test_build_challenge_payload_shape():
    token = "abc123"
    sub_id = uuid.uuid4()
    payload = json.loads(dw.build_challenge_payload(token, sub_id))
    assert payload == {"challenge": token, "subscription_id": str(sub_id)}


# ---------------------------------------------------------------------------
# Fix #7: 3-strike hard-4xx rule counts only a streak of the SAME error class
# ---------------------------------------------------------------------------


def test_record_failure_resets_consecutive_failures_on_a_different_error_class():
    """A timeout, then a hard-4xx, then another hard-4xx: the class switch
    from 'timeout' to 'http_4xx' must reset the counter to 1 (not carry the
    timeout's failure into the 4xx streak) -- otherwise the 3-consecutive-
    hard-4xx auto-disable rule fires after just 2 REAL hard-4xx failures."""
    now = dt.datetime.now(dt.timezone.utc)
    sub = SimpleNamespace(
        last_attempt_at=None, last_status=None, last_error=None,
        consecutive_failures=0, failing_since=None,
    )
    dw._record_failure(sub, status=None, error_class="timeout", now=now)
    assert sub.consecutive_failures == 1
    first_failing_since = sub.failing_since

    dw._record_failure(sub, status=400, error_class="http_4xx", now=now)
    assert sub.consecutive_failures == 1, "class switch must reset the streak to 1, not accumulate to 2"
    assert sub.failing_since == first_failing_since, "failing_since (the 72h wall clock) is untouched by the reset"

    dw._record_failure(sub, status=400, error_class="http_4xx", now=now)
    assert sub.consecutive_failures == 2, "same class as the previous failure keeps accumulating"


def test_record_failure_accumulates_within_the_same_error_class():
    now = dt.datetime.now(dt.timezone.utc)
    sub = SimpleNamespace(
        last_attempt_at=None, last_status=None, last_error=None,
        consecutive_failures=0, failing_since=None,
    )
    for expected in (1, 2, 3):
        dw._record_failure(sub, status=404, error_class="http_4xx", now=now)
        assert sub.consecutive_failures == expected


# ---------------------------------------------------------------------------
# Fix #1a: challenge retries back off exactly like a delivery failure
# ---------------------------------------------------------------------------


def test_apply_challenge_backoff_sets_next_attempt_at_in_the_future():
    now = dt.datetime.now(dt.timezone.utc)
    sub = SimpleNamespace(challenge_attempts=2, next_attempt_at=None)
    dw._apply_challenge_backoff(sub, now=now)
    assert sub.next_attempt_at is not None
    assert sub.next_attempt_at > now


# ---------------------------------------------------------------------------
# §7: rotation (a Nth+1 subscriber can never starve indefinitely) and
# truncation+drain (a big backlog is delivered as several POSTs within one
# tick's per-sub budget). Both pure-logic, no DB: `rotation_order` and
# `_drain_one`'s injectable `fetch_batch` are the seams that make that
# possible -- see their own docstrings in dispatch_webhooks.py.
# ---------------------------------------------------------------------------


def test_rotation_serves_the_previously_unserved_sub_first_next_tick():
    """Cap of 3 over 4 subs: sub D is left out of tick 1's batch (it's a
    tie with everyone else at `last_attempt_at=None`, and Python's stable
    sort keeps A/B/C ahead of it in list order) -- tick 2 MUST serve D
    first, proving the ordering doesn't just favor whoever happened to sort
    first once and keep favoring them forever."""
    cap = 3
    subs = [SimpleNamespace(name=name, last_attempt_at=None) for name in "ABCD"]

    tick1 = dw.rotation_order(subs, limit=cap)
    assert [s.name for s in tick1] == ["A", "B", "C"], "D correctly excluded from tick 1"

    tick1_time = dt.datetime(2026, 8, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
    for s in tick1:
        s.last_attempt_at = tick1_time

    tick2 = dw.rotation_order(subs, limit=cap)
    assert tick2[0].name == "D", "the sub left out of tick 1 must be served FIRST in tick 2"
    assert {s.name for s in tick2} == {"D", "A", "B"}


def test_rotation_makes_starvation_impossible_across_many_ticks():
    """N subs, cap C: every sub must have been served at least once within
    ceil(N / C) ticks, for ANY N/C -- not just the specific 4-over-3 example
    above. Parameterized down from the spec's literal "51 subs, cap 50" per
    the coordinator's own note (heavy for a pure-Python harness to matter,
    proves nothing extra at N=51 that N=11 doesn't)."""
    import math

    n_subs, cap = 11, 3
    subs = [SimpleNamespace(name=i, last_attempt_at=None) for i in range(n_subs)]
    served_at: dict[int, int] = {}

    expected_ticks = math.ceil(n_subs / cap)
    for tick in range(expected_ticks):
        batch = dw.rotation_order(subs, limit=cap)
        tick_time = dt.datetime(2026, 8, 4, 12, tick, 0, tzinfo=dt.timezone.utc)
        for s in batch:
            served_at.setdefault(s.name, tick)
            s.last_attempt_at = tick_time

    unserved = [s.name for s in subs if s.name not in served_at]
    assert not unserved, (
        f"subs {unserved} were never served within the {expected_ticks} ticks "
        f"required to cover {n_subs} subs at cap {cap} -- rotation starved them"
    )


# ---------------------------------------------------------------------------
# r11 fix #5 (opus C, HIGH): run_challenges' own rotation (challenge_
# attempted_at NULLS FIRST, migration 0015) and the disable-after-N-total-
# failed-attempts GC path. `_challenge_order_by` is pure/DB-independent
# (same seam `rotation_order` already is above) -- compiled to SQL text so
# the ordering choice is provable without a live DB or the column actually
# existing yet on this environment's Postgres (see that column's own
# `deferred=True` docstring in billcommons_schema.models).
# ---------------------------------------------------------------------------


def test_challenge_order_by_rotates_on_challenge_attempted_at_when_available():
    clause = dw._challenge_order_by(True)
    compiled = str(clause)
    assert "challenge_attempted_at" in compiled
    assert "NULLS FIRST" in compiled.upper()


def test_challenge_order_by_falls_back_to_created_at_before_migration_0015():
    """Probe-guarded degradation (same shape `_notify_pending_supports_
    created_disabled`/`_creation_events_table_exists` already use for their
    own pending migrations) -- a database that has not yet run migration
    0015 must get the OLD, unchanged ordering, not a query referencing a
    column that does not exist."""
    clause = dw._challenge_order_by(False)
    compiled = str(clause)
    assert "created_at" in compiled
    assert "challenge_attempted_at" not in compiled


def test_maybe_disable_after_challenge_attempts_disables_at_the_cap():
    now = dt.datetime.now(dt.timezone.utc)
    sub = SimpleNamespace(
        challenge_attempts=dw.CHALLENGE_ATTEMPT_DISABLE_AFTER,
        active=True, disabled_reason=None, disabled_at=None, notify_pending=None,
        challenge_token="tok", next_attempt_at=None,
    )
    disabled = dw._maybe_disable_after_challenge_attempts(sub, now=now)
    assert disabled is True
    assert sub.active is False
    assert sub.disabled_reason == "challenge_timeout"
    assert sub.disabled_at == now
    assert sub.notify_pending == "disabled"
    assert sub.challenge_token is None


def test_maybe_disable_after_challenge_attempts_leaves_the_sub_alone_below_the_cap():
    now = dt.datetime.now(dt.timezone.utc)
    sub = SimpleNamespace(
        challenge_attempts=dw.CHALLENGE_ATTEMPT_DISABLE_AFTER - 1,
        active=True, disabled_reason=None, disabled_at=None, notify_pending=None,
        challenge_token="tok", next_attempt_at=None,
    )
    disabled = dw._maybe_disable_after_challenge_attempts(sub, now=now)
    assert disabled is False
    assert sub.active is True
    assert sub.disabled_reason is None


# ---------------------------------------------------------------------------
# r12 fix #2 (agy HIGH + grok 1 HIGH + opus 2 HIGH -- convergent, three
# legs): the challenge_attempted_at stamp must commit in its OWN
# transaction BEFORE the outbound POST, never left open across it. This
# repo's live test DB is still on migration 0012 (0015 not applied -- see
# module docstring), so `column_available=True` is exercised here against
# a plain fake session that records call ORDER, not a real Postgres
# column -- the same "prove the mechanism, not the live schema" seam
# `_challenge_order_by`'s own tests above already use for the identical
# reason.
# ---------------------------------------------------------------------------


class _StampOrderDb:
    """Tracks exactly which raw UPDATEs are DURABLE (survived a commit) vs
    still pending (would be discarded by a rollback) -- proves the
    stamp-commits-before-the-fetch invariant without a live DB."""

    def __init__(self):
        self.calls: list[str] = []
        self.durable_stamps = 0
        self._pending_stamps = 0

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "challenge_attempted_at" in sql and "UPDATE" in sql.upper():
            self.calls.append("stamp")
            self._pending_stamps += 1
        elif "pg_advisory_xact_lock" in sql:
            self.calls.append("advisory_lock")
        else:
            self.calls.append("execute")
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    def commit(self):
        self.calls.append("commit")
        self.durable_stamps += self._pending_stamps
        self._pending_stamps = 0

    def rollback(self):
        self.calls.append("rollback")
        self._pending_stamps = 0  # anything not yet committed is discarded


def _challenge_sub():
    return SimpleNamespace(
        id=uuid.uuid4(), url="https://example.invalid/hook", challenge_token="tok",
        challenge_attempts=0, signing_secret="s3cr3t", last_error=None, last_status=None,
        next_attempt_at=None,
    )


def test_attempt_challenge_commits_the_stamp_before_the_fetch():
    db = _StampOrderDb()
    sub = _challenge_sub()

    class _FailingClient:
        def fetch(self, *a, **kw):
            db.calls.append("fetch")
            raise dw.TimeoutFailure("boom")

    now = dt.datetime.now(dt.timezone.utc)
    dw._attempt_challenge(db, _FailingClient(), sub, now=now, column_available=True)

    assert db.calls.index("stamp") < db.calls.index("commit") < db.calls.index("fetch"), (
        f"the stamp must commit in its OWN transaction BEFORE the outbound POST: {db.calls}"
    )
    assert db.durable_stamps == 1, (
        "the stamp must already be durable (committed) before the fetch runs at all"
    )


def test_attempt_challenge_crash_after_the_fetch_does_not_undo_the_already_committed_stamp():
    """The pre-fix bug: an exception escaping `_attempt_challenge` (a
    genuine bug, not a `SafeHttpError` -- those are caught inside the
    function) propagates to `run_challenges`' crash handler, which calls
    `db.rollback()`. Before this fix the stamp's UPDATE was still part of
    the SAME open transaction as the (about to fail) fetch -- rollback
    un-stamped the very attempt that just happened. After this fix the
    stamp already committed in its own transaction before the fetch ever
    ran, so a later rollback cannot touch it."""
    db = _StampOrderDb()
    sub = _challenge_sub()

    class _CrashingClient:
        def fetch(self, *a, **kw):
            db.calls.append("fetch")
            raise RuntimeError("simulated bug, not a SafeHttpError")

    now = dt.datetime.now(dt.timezone.utc)
    with pytest.raises(RuntimeError):
        dw._attempt_challenge(db, _CrashingClient(), sub, now=now, column_available=True)

    # The SAME recovery `run_challenges`' own except-Exception handler
    # performs on a crash.
    db.rollback()

    assert db.durable_stamps == 1, (
        "a fetch exception escaping _attempt_challenge must not roll back "
        "the ALREADY-COMMITTED stamp -- that was the pre-fix bug"
    )


class _FakeDb:
    """Enough of the Session surface for `_drain_one` to run: `commit()` is
    a no-op (nothing here is actually persisted -- the fake `fetch_batch`
    supplies events directly, so `_drain_one` never issues a real query),
    `add()` just records what would have been a WebhookDelivery row."""

    def __init__(self):
        self.added = []

    def commit(self):
        pass

    def add(self, obj):
        self.added.append(obj)


class _FakeHttpClient:
    def __init__(self, status=200):
        self.status = status
        self.calls: list[bytes] = []
        self.require_body_calls: list[bool] = []
        self.headers_calls: list[dict] = []

    def fetch(self, url, *, method, body, headers, require_body=True):
        self.calls.append(body)
        self.require_body_calls.append(require_body)
        self.headers_calls.append(headers)
        return SimpleNamespace(status=self.status, headers={}, body=b"")


def _fake_advance_last_seq(db, sub, new_value):
    """Test-only stand-in for `dw._advance_last_seq` (fix #11): the real one
    issues a `GREATEST` SQL UPDATE against a live DB, which `_FakeDb` cannot
    do -- this reproduces the identical GREATEST semantics in plain Python
    against the SimpleNamespace `sub` these pure-logic tests use, the same
    injectable-seam pattern `fetch_batch` already uses in this file."""
    current = sub.last_seq if sub.last_seq is not None else 0
    if new_value > current:
        sub.last_seq = new_value


def _fake_events(n: int, now: dt.datetime):
    """n in-memory (BillEvent-like, Bill-like, jur, session) tuples, seq
    1..n -- shaped exactly like what the real `_fetch_batch` returns."""
    out = []
    for seq in range(1, n + 1):
        bill = SimpleNamespace(
            id=uuid.uuid4(), jurisdiction_id=uuid.uuid4(), session_id=uuid.uuid4(),
            chamber=None, identifier=f"HB {seq}", identifier_norm=f"HB {seq}", title="t",
            short_title=None, bill_type=None, status=None, status_date=None,
            introduced_date=None, latest_action_text=None, latest_action_date=None,
            source_url=None, updated_at=now,
        )
        event = SimpleNamespace(seq=seq, bill_id=bill.id, kind="status", detail=None, changed_at=now)
        out.append((event, bill, "ZZ", "2026 Session"))
    return out


def test_truncation_drains_a_backlog_as_several_posts_within_one_tick():
    """5 events, batch size forced to 2 (via the fake fetch_batch -- the
    coordinator's own allowance: "batch limit forced to 2 via
    constant/monkeypatch" -- this IS the monkeypatch, just expressed as a
    substitute data source rather than mutating the module constant, since
    the fake source makes MAX_EVENTS_PER_BATCH irrelevant to this test).
    Expected: 3 POSTs (2, 2, 1), cursor (`sub.last_seq`) advances to the
    LAST delivered seq after each batch, has_more=true on the first two,
    has_more=false + cursor at the watermark on the final one."""
    now = dt.datetime.now(dt.timezone.utc)
    all_events = _fake_events(5, now)
    batch_size = 2

    seen_has_more: list[bool] = []
    seen_last_seq_after_commit: list[int] = []

    def fake_fetch_batch(db, sub, *, watermark_seq):
        remaining = [e for e in all_events if e[0].seq > sub.last_seq and e[0].seq <= watermark_seq]
        if not remaining:
            return [], False, None
        batch = remaining[:batch_size]
        has_more = len(remaining) > batch_size
        return batch, has_more, batch[-1][0].seq

    sub = SimpleNamespace(
        id=uuid.uuid4(), url="https://example.test/hook", signing_secret="s3cr3t",
        last_seq=0, consecutive_failures=0, failing_since=None, next_attempt_at=None,
        active=True, disabled_reason=None, disabled_at=None, notify_pending=None,
        last_status=None, last_error=None, last_attempt_at=None, last_success_at=None,
    )
    db = _FakeDb()
    client = _FakeHttpClient(status=200)

    # Wrap fetch_batch to observe has_more per call without changing behavior.
    def observing_fetch_batch(db_, sub_, *, watermark_seq):
        events, has_more, last_raw_seq = fake_fetch_batch(db_, sub_, watermark_seq=watermark_seq)
        seen_has_more.append(has_more)
        return events, has_more, last_raw_seq

    events_sent, failed = dw._drain_one(
        db, client, sub, watermark_seq=5, watermark_dt=now,
        deadline=time.monotonic() + 10, fetch_batch=observing_fetch_batch,
        advance_last_seq=_fake_advance_last_seq,
    )

    assert failed is False
    assert events_sent == 5
    assert len(client.calls) == 3, "5 events at batch size 2 must be 3 POSTs (2, 2, 1)"
    assert seen_has_more == [True, True, False]
    assert sub.last_seq == 5, "cursor must land on the watermark after the final (non-truncated) batch"
    assert client.require_body_calls == [False, False, False], (
        "deliveries must never require the strict body read (fix #15) -- "
        "only challenges do"
    )

    # Each POST body's own has_more/cursor match what was actually delivered.
    bodies = [json.loads(b) for b in client.calls]
    assert [b["has_more"] for b in bodies] == [True, True, False]
    assert [b["events"][-1]["id"] for b in bodies] == ["2", "4", "5"]
    from billcommons_shared.cursor import decode_cursor

    assert [decode_cursor(b["cursor"]) for b in bodies] == [2, 4, 5]


# ---------------------------------------------------------------------------
# Fix #5: a raw batch that filters down to nothing must advance to the last
# RAW seq and keep draining -- never straight to the watermark, which would
# silently skip whatever is beyond this batch but still inside the window.
# ---------------------------------------------------------------------------


def test_filtered_batch_advances_to_last_raw_seq_and_continues_not_the_watermark():
    """Two fetch_batch calls: the first returns raw rows up through seq 3
    that ALL get filtered out downstream (has_more=True, so there is more
    behind them) and the second returns a real event at seq 7. If the bug
    were present, the first call's empty `events` would advance straight to
    watermark_seq=10 and the second call would never happen -- seq 7's event
    would be silently skipped forever (at-most-once)."""
    now = dt.datetime.now(dt.timezone.utc)
    calls = {"n": 0}

    real_event = _fake_events(1, now)[0]
    real_event = (SimpleNamespace(seq=7, bill_id=real_event[1].id, kind="status", detail=None, changed_at=now),) + real_event[1:]

    def fake_fetch_batch(db, sub, *, watermark_seq):
        calls["n"] += 1
        if calls["n"] == 1:
            # Raw rows existed (up to seq 3) but all filtered out downstream.
            return [], True, 3
        # Second call: the real event, seq 7, no more after it.
        return [real_event], False, 7

    sub = SimpleNamespace(
        id=uuid.uuid4(), url="https://example.test/hook", signing_secret="s3cr3t",
        last_seq=0, consecutive_failures=0, failing_since=None, next_attempt_at=None,
        active=True, disabled_reason=None, disabled_at=None, notify_pending=None,
        last_status=None, last_error=None, last_attempt_at=None, last_success_at=None,
    )
    db = _FakeDb()
    client = _FakeHttpClient(status=200)

    events_sent, failed = dw._drain_one(
        db, client, sub, watermark_seq=10, watermark_dt=now,
        deadline=time.monotonic() + 10, fetch_batch=fake_fetch_batch,
        advance_last_seq=_fake_advance_last_seq,
    )

    assert failed is False
    assert calls["n"] == 2, "the filtered-out first batch must not stop the drain loop"
    assert events_sent == 1
    assert sub.last_seq == 7, "must land on the real event's seq, not jump to the watermark (10)"
    assert sub.last_attempt_at is not None, "the filtered-batch advance stamps last_attempt_at too (fix #4)"


def test_filtered_batch_with_no_more_after_it_advances_and_stops():
    """A raw batch that filters to nothing and has_more=False (nothing left
    in the window) must still advance to the last raw seq -- not the
    watermark -- and stop, with no POST at all."""
    now = dt.datetime.now(dt.timezone.utc)

    def fake_fetch_batch(db, sub, *, watermark_seq):
        return [], False, 4

    sub = SimpleNamespace(
        id=uuid.uuid4(), url="https://example.test/hook", signing_secret="s3cr3t",
        last_seq=0, consecutive_failures=0, failing_since=None, next_attempt_at=None,
        active=True, disabled_reason=None, disabled_at=None, notify_pending=None,
        last_status=None, last_error=None, last_attempt_at=None, last_success_at=None,
    )
    db = _FakeDb()
    client = _FakeHttpClient(status=200)

    events_sent, failed = dw._drain_one(
        db, client, sub, watermark_seq=10, watermark_dt=now,
        deadline=time.monotonic() + 10, fetch_batch=fake_fetch_batch,
        advance_last_seq=_fake_advance_last_seq,
    )

    assert failed is False
    assert events_sent == 0
    assert len(client.calls) == 0
    assert sub.last_seq == 4, "advances to the last RAW seq, not the watermark"


# ---------------------------------------------------------------------------
# Fix #6: a 429 is also subject to the 72h wall-clock auto-disable
# ---------------------------------------------------------------------------


def test_429_after_72h_of_failing_since_auto_disables():
    now = dt.datetime.now(dt.timezone.utc)
    old_failing_since = now - dt.timedelta(hours=73)

    def fake_fetch_batch(db, sub, *, watermark_seq):
        if sub.last_seq >= 1:
            return [], False, None
        return _fake_events(1, now), False, 1

    sub = SimpleNamespace(
        id=uuid.uuid4(), url="https://example.test/hook", signing_secret="s3cr3t",
        last_seq=0, consecutive_failures=5, failing_since=old_failing_since, next_attempt_at=None,
        active=True, disabled_reason=None, disabled_at=None, notify_pending=None,
        last_status=None, last_error="http_4xx", last_attempt_at=None, last_success_at=None,
    )
    db = _FakeDb()
    client = _FakeHttpClient(status=429)

    events_sent, failed = dw._drain_one(
        db, client, sub, watermark_seq=1, watermark_dt=now,
        deadline=time.monotonic() + 10, fetch_batch=fake_fetch_batch,
    )

    assert failed is True
    assert sub.active is False, "a 429 continuing past 72h of failing_since must auto-disable (fix #6)"
    assert sub.disabled_reason == "too_many_failures"
    assert sub.notify_pending == "disabled"


# ---------------------------------------------------------------------------
# Verify round-5 fix #6 (codex MED): a single event whose serialized body
# still exceeds MAX_BODY_BYTES after halving as far as possible (down to
# length 1) must be SKIPPED with an audit row, never POSTed oversized.
# ---------------------------------------------------------------------------


def test_oversized_single_event_is_skipped_with_an_audit_row_not_posted(monkeypatch):
    """Force MAX_BODY_BYTES tiny (smaller than any real serialized event)
    so the halving loop bottoms out at length 1 while still over cap --
    that one event must never reach `http_client.fetch`, the cursor must
    advance PAST it, `last_error` must be stamped, and a WebhookDelivery
    audit row must record `error='payload_too_large'` with no status/
    duration (no HTTP attempt was ever made)."""
    monkeypatch.setattr(dw, "MAX_BODY_BYTES", 10)
    now = dt.datetime.now(dt.timezone.utc)

    def fake_fetch_batch(db, sub, *, watermark_seq):
        if sub.last_seq >= 1:
            return [], False, None
        return _fake_events(1, now), False, 1

    sub = SimpleNamespace(
        id=uuid.uuid4(), url="https://example.test/hook", signing_secret="s3cr3t",
        last_seq=0, consecutive_failures=0, failing_since=None, next_attempt_at=None,
        active=True, disabled_reason=None, disabled_at=None, notify_pending=None,
        last_status=None, last_error=None, last_attempt_at=None, last_success_at=None,
    )
    db = _FakeDb()
    client = _FakeHttpClient(status=200)

    events_sent, failed = dw._drain_one(
        db, client, sub, watermark_seq=1, watermark_dt=now,
        deadline=time.monotonic() + 10, fetch_batch=fake_fetch_batch,
        advance_last_seq=_fake_advance_last_seq,
    )

    assert failed is False, "an oversized-and-skipped event is not an HTTP failure"
    assert events_sent == 0, "a skipped event was never actually SENT"
    assert len(client.calls) == 0, "the oversized event must never reach the transport (fix #6)"
    assert sub.last_seq == 1, "the cursor must advance PAST the skipped event, or it would retry forever"
    assert sub.last_error == "payload_too_large"
    assert len(db.added) == 1
    audit_row = db.added[0]
    assert audit_row.error == "payload_too_large"
    assert audit_row.status is None, "no HTTP attempt was ever made for a skipped event"
    assert audit_row.event_count == 1
    assert sub.failing_since is None, (
        "r12 fix #3(b): a skip is not evidence about receiver health -- it "
        "must never arm the 72h wall-clock auto-disable clock"
    )
    assert sub.active is True, "one skip alone must never disable the subscription"


# ---------------------------------------------------------------------------
# r12 fix #3 (grok 2 MED + codex MED + opus 3 MED -- convergent, three
# angles): the oversized-skip branch was doubly wrong -- (a) it never
# reached ANY disable path, so the docs' own 72h off-ramp for a
# perpetually-oversized subscription was unreachable; (b) it armed
# `failing_since`, so a narrow sub that skips once and then sees a single,
# unrelated, transient failure much later gets treated as though it had
# been failing the WHOLE time in between and is disabled off ONE real
# failure. Both scenarios below are named for the exact wording of the
# fix's own two-part instruction.
# ---------------------------------------------------------------------------


def test_sparse_503_after_one_skip_stays_active(monkeypatch):
    """The sub skips exactly one oversized event (never arms
    `failing_since` -- proven directly), then sees a single, ordinary,
    transient 503 on a LATER, unrelated event. That one real failure must
    not instantly auto-disable the subscription: it is the FIRST failure
    that ever armed the wall clock, so `_maybe_auto_disable`'s 72h check
    reads a `failing_since` of `now`, not of the unrelated skip."""
    monkeypatch.setattr(dw, "MAX_BODY_BYTES", 10)
    now = dt.datetime.now(dt.timezone.utc)

    def skip_once(db, sub, *, watermark_seq):
        return _fake_events(1, now), False, 1

    sub = SimpleNamespace(
        id=uuid.uuid4(), url="https://example.test/hook", signing_secret="s3cr3t",
        last_seq=0, consecutive_failures=0, failing_since=None, next_attempt_at=None,
        active=True, disabled_reason=None, disabled_at=None, notify_pending=None,
        last_status=None, last_error=None, last_attempt_at=None, last_success_at=None,
    )
    db = _FakeDb()

    dw._drain_one(
        db, _FakeHttpClient(status=200), sub, watermark_seq=1, watermark_dt=now,
        deadline=time.monotonic() + 10, fetch_batch=skip_once,
        advance_last_seq=_fake_advance_last_seq,
    )
    assert sub.failing_since is None, "the skip alone must never arm the 72h clock"
    assert sub.active is True

    # A real, restored MAX_BODY_BYTES -- the next event is an ORDINARY
    # delivery, not another skip -- that gets a single transient 503.
    monkeypatch.setattr(dw, "MAX_BODY_BYTES", 512 * 1024)

    def one_real_failure(db, sub, *, watermark_seq):
        if sub.last_seq >= 2:
            return [], False, None
        return _fake_events(2, now)[1:], False, 2

    events_sent, failed = dw._drain_one(
        db, _FakeHttpClient(status=503), sub, watermark_seq=2, watermark_dt=now,
        deadline=time.monotonic() + 10, fetch_batch=one_real_failure,
        advance_last_seq=_fake_advance_last_seq,
    )

    assert failed is True
    assert sub.consecutive_failures == 1
    assert sub.active is True, (
        "a single transient failure right after an UNRELATED oversized "
        "skip must not instantly auto-disable -- the 72h clock only just "
        "started on THIS failure"
    )


def test_continuous_oversized_eventually_disables(monkeypatch):
    """A subscription whose scope keeps generating a fresh oversized event
    every batch must still reach a disable eventually -- `failing_since`
    is deliberately never armed for this class (see the sparse-503 test
    above), so it needs its OWN streak-based disable path instead
    (OVERSIZED_SKIP_DISABLE_AFTER)."""
    monkeypatch.setattr(dw, "MAX_BODY_BYTES", 10)
    monkeypatch.setattr(dw, "OVERSIZED_SKIP_DISABLE_AFTER", 3)
    now = dt.datetime.now(dt.timezone.utc)

    def all_oversized(db, sub, *, watermark_seq):
        seq = sub.last_seq + 1
        if seq > 5:
            return [], False, None
        return _fake_events(seq, now)[-1:], seq < 5, seq

    sub = SimpleNamespace(
        id=uuid.uuid4(), url="https://example.test/hook", signing_secret="s3cr3t",
        last_seq=0, consecutive_failures=0, failing_since=None, next_attempt_at=None,
        active=True, disabled_reason=None, disabled_at=None, notify_pending=None,
        last_status=None, last_error=None, last_attempt_at=None, last_success_at=None,
    )
    db = _FakeDb()
    client = _FakeHttpClient(status=200)

    events_sent, failed = dw._drain_one(
        db, client, sub, watermark_seq=5, watermark_dt=now,
        deadline=time.monotonic() + 10, fetch_batch=all_oversized,
        advance_last_seq=_fake_advance_last_seq,
    )

    assert failed is True
    assert len(client.calls) == 0, "an oversized event must never reach the transport, disable or not"
    assert sub.active is False, "a perpetually-oversized scope must eventually disable"
    assert sub.disabled_reason == "payload_too_large"
    assert sub.consecutive_failures == 3
    assert sub.last_seq == 3, "the cursor must have advanced past each skip up to the disable point"
    assert sub.failing_since is None, "still never armed -- this disable came from the streak, not the wall clock"
    assert len(db.added) == 3, "one audit row per skip, including the one that triggered the disable"


# ---------------------------------------------------------------------------
# r13 fix #4 (grok MED 2): a skip must CLEAR an already-armed failing_since,
# not just leave it (never re-arm it, the pre-existing behavior above) --
# an OLD armed clock from a real failure well before a long skip run must
# not keep accumulating wall-clock time the receiver was never contacted
# for.
# ---------------------------------------------------------------------------


def test_skip_clears_an_already_armed_failing_since():
    """fail@T0 arms failing_since; a skip run spans past the 72h
    AUTO_DISABLE_AFTER window; one late, ordinary 5xx must NOT trip the 72h
    disable -- failing_since restarts at the late failure's own timestamp,
    not the stale T0."""
    t0 = dt.datetime.now(dt.timezone.utc)
    sub = SimpleNamespace(
        last_attempt_at=None, last_status=None, last_error=None,
        consecutive_failures=0, failing_since=None,
    )
    dw._record_failure(sub, status=503, error_class="http_5xx", now=t0)
    assert sub.failing_since == t0

    t_skip = t0 + dt.timedelta(hours=80)
    dw._record_skip(sub, error_class="payload_too_large", now=t_skip)
    assert sub.failing_since is None, "a skip must clear an already-armed failing_since"

    t_late = t0 + dt.timedelta(hours=81)
    dw._record_failure(sub, status=503, error_class="http_5xx", now=t_late)
    assert sub.failing_since == t_late, "failing_since restarts at the late failure's own timestamp"

    disabled = dw._maybe_auto_disable(sub, now=t_late, reason="too_many_failures")
    assert disabled is False, "the 72h clock only just (re)started -- must not disable off the stale T0"


def test_record_skip_is_a_no_op_on_an_unarmed_failing_since():
    """The common case (no prior failure) -- a skip has nothing to clear
    and must not raise or otherwise misbehave when failing_since is
    already None."""
    now = dt.datetime.now(dt.timezone.utc)
    sub = SimpleNamespace(
        last_attempt_at=None, last_status=None, last_error=None,
        consecutive_failures=0, failing_since=None,
    )
    dw._record_skip(sub, error_class="payload_too_large", now=now)
    assert sub.failing_since is None


# ---------------------------------------------------------------------------
# r13 fix #5 (deepseek LOW 3): the X-BillCommons-Delivery-Attempt header must
# not inherit an oversized-skip streak's length -- a receiver that was never
# once contacted must not see its first real delivery arrive claiming to be
# a Nth retry.
# ---------------------------------------------------------------------------


def test_delivery_attempt_is_1_after_an_oversized_skip_streak():
    sub = SimpleNamespace(consecutive_failures=5, last_error="payload_too_large")
    assert dw._delivery_attempt(sub) == 1


def test_delivery_attempt_is_streak_plus_one_for_real_failures():
    sub = SimpleNamespace(consecutive_failures=2, last_error="http_5xx")
    assert dw._delivery_attempt(sub) == 3


def test_delivery_attempt_is_1_with_no_prior_history():
    sub = SimpleNamespace(consecutive_failures=0, last_error=None)
    assert dw._delivery_attempt(sub) == 1


def test_first_real_delivery_after_a_skip_run_is_stamped_attempt_1(monkeypatch):
    """End-to-end via `_drain_one`: three oversized skips (consecutive_
    failures=3, last_error='payload_too_large'), then a normal-sized event
    that actually reaches the transport -- the ACTUAL HTTP header sent must
    read '1', not '4'."""
    monkeypatch.setattr(dw, "MAX_BODY_BYTES", 10)
    now = dt.datetime.now(dt.timezone.utc)

    def all_oversized(db, sub, *, watermark_seq):
        seq = sub.last_seq + 1
        if seq > 3:
            return [], False, None
        return _fake_events(seq, now)[-1:], seq < 3, seq

    sub = SimpleNamespace(
        id=uuid.uuid4(), url="https://example.test/hook", signing_secret="s3cr3t",
        last_seq=0, consecutive_failures=0, failing_since=None, next_attempt_at=None,
        active=True, disabled_reason=None, disabled_at=None, notify_pending=None,
        last_status=None, last_error=None, last_attempt_at=None, last_success_at=None,
    )
    db = _FakeDb()
    client = _FakeHttpClient(status=200)
    dw._drain_one(
        db, client, sub, watermark_seq=3, watermark_dt=now,
        deadline=time.monotonic() + 10, fetch_batch=all_oversized,
        advance_last_seq=_fake_advance_last_seq,
    )
    assert sub.consecutive_failures == 3
    assert sub.last_error == "payload_too_large"
    assert len(client.calls) == 0, "sanity: all three were skips, none reached the transport yet"

    # Now a real, normal-sized event finally reaches the receiver.
    monkeypatch.setattr(dw, "MAX_BODY_BYTES", 512 * 1024)

    def one_real_delivery(db, sub, *, watermark_seq):
        if sub.last_seq >= 4:
            return [], False, None
        return _fake_events(4, now)[-1:], False, 4

    dw._drain_one(
        db, client, sub, watermark_seq=4, watermark_dt=now,
        deadline=time.monotonic() + 10, fetch_batch=one_real_delivery,
        advance_last_seq=_fake_advance_last_seq,
    )
    assert len(client.headers_calls) == 1
    assert client.headers_calls[0]["X-BillCommons-Delivery-Attempt"] == "1", (
        "the first RECEIVER-FACING delivery after a skip-only streak must "
        "not inherit that streak's length"
    )


# ---------------------------------------------------------------------------
# r13 fix #6 (kimi LOW 3): the non-retryable-4xx branch (streak<3) was the
# only failure branch that never called `_maybe_auto_disable` -- a hard-4xx
# receiver interleaved with something that resets the 3-strike counter
# below the cap (e.g. a skip, or any other class change) could otherwise
# evade every disable rule forever.
# ---------------------------------------------------------------------------


def test_non_retryable_4xx_below_3_strikes_still_calls_maybe_auto_disable(monkeypatch):
    """A hard-4xx receiver, contacted every tick (never a skip -- this is
    the PERSISTENT-hard-4xx case the fixlist calls out), fails for well
    over 72h. The 3-strike rule alone cannot fire here because each
    individual tick's own `_drain_one` call only ever sees ONE failure per
    invocation (consecutive_failures never reaches 3 within a single call in
    this harness's per-tick simulation) -- the 72h wall clock must be the
    rule that eventually fires."""
    now = dt.datetime.now(dt.timezone.utc)
    t0 = now - (dw.AUTO_DISABLE_AFTER + dt.timedelta(hours=1))

    def one_hard_4xx(db, sub, *, watermark_seq):
        if sub.last_seq >= 1:
            return [], False, None
        return _fake_events(1, t0), False, None  # cursor does NOT advance on failure

    sub = SimpleNamespace(
        id=uuid.uuid4(), url="https://example.test/hook", signing_secret="s3cr3t",
        last_seq=0, consecutive_failures=1, failing_since=t0, next_attempt_at=None,
        active=True, disabled_reason=None, disabled_at=None, notify_pending=None,
        last_status=404, last_error="http_4xx", last_attempt_at=t0, last_success_at=None,
    )
    db = _FakeDb()
    client = _FakeHttpClient(status=404)

    dw._drain_one(
        db, client, sub, watermark_seq=1, watermark_dt=now,
        deadline=time.monotonic() + 10, fetch_batch=one_hard_4xx,
        advance_last_seq=_fake_advance_last_seq,
    )

    assert sub.consecutive_failures < 3, "sanity: the 3-strike rule alone must not be what fired here"
    assert sub.active is False, (
        "a persistent hard-4xx receiver, below the 3-strike threshold on "
        "every individual check, must still be caught by the 72h wall "
        "clock -- this branch used to never call _maybe_auto_disable at all"
    )
    assert sub.disabled_reason == "too_many_failures"


def test_non_retryable_4xx_interleaved_with_skips_eventually_disables(monkeypatch):
    """The fixlist's own named interaction with fix #4: skips clear
    failing_since AND (via `_record_failure`/`_record_skip`'s shared
    reset-on-class-change rule, pre-existing) reset `consecutive_failures`
    to 1 on the very next hard-4xx -- an interleave that puts a skip
    between EVERY single hard-4xx hit can defeat both rules at once (the
    3-strike streak never reaches 3, and the wall clock keeps getting
    freshly re-armed). But a receiver interleaved LESS perfectly -- three
    hard-4xx hits landing consecutively at some point, which any real
    event stream eventually does unless every single delivery happens to
    be skip-sized -- must still be caught by the 3-strike rule this fix
    adds to this branch. Pattern: skip, 4xx, skip, 4xx, then three 4xx in a
    row (no intervening skip) -- proves the fix closes the gap for a
    receiver that is not skip-shielded on literally every single attempt."""
    now = dt.datetime.now(dt.timezone.utc)

    def hard_4xx_event(db, sub, *, watermark_seq):
        seq = sub.last_seq + 1
        return _fake_events(seq, now)[-1:], False, None  # cursor does NOT advance on failure

    def oversized_skip_event(db, sub, *, watermark_seq):
        seq = sub.last_seq + 1
        return _fake_events(seq, now)[-1:], False, seq  # cursor DOES advance past a skip

    sub = SimpleNamespace(
        id=uuid.uuid4(), url="https://example.test/hook", signing_secret="s3cr3t",
        last_seq=0, consecutive_failures=0, failing_since=None, next_attempt_at=None,
        active=True, disabled_reason=None, disabled_at=None, notify_pending=None,
        last_status=None, last_error=None, last_attempt_at=None, last_success_at=None,
    )
    db = _FakeDb()

    def hit_4xx():
        monkeypatch.setattr(dw, "MAX_BODY_BYTES", 512 * 1024)
        client = _FakeHttpClient(status=404)
        dw._drain_one(
            db, client, sub, watermark_seq=sub.last_seq + 1, watermark_dt=now,
            deadline=time.monotonic() + 10, fetch_batch=hard_4xx_event,
            advance_last_seq=_fake_advance_last_seq,
        )

    def hit_skip():
        monkeypatch.setattr(dw, "MAX_BODY_BYTES", 10)
        client = _FakeHttpClient(status=200)
        dw._drain_one(
            db, client, sub, watermark_seq=sub.last_seq + 1, watermark_dt=now,
            deadline=time.monotonic() + 10, fetch_batch=oversized_skip_event,
            advance_last_seq=_fake_advance_last_seq,
        )

    hit_skip()
    hit_4xx()
    assert sub.consecutive_failures == 1, "reset-on-class-change: the skip just before this 4xx reset the streak"
    hit_skip()
    hit_4xx()
    assert sub.consecutive_failures == 1, "reset again -- neither rule has fired yet"
    assert sub.active is True

    # Two MORE hard-4xx hits with NO intervening skip -- combined with the
    # one already recorded just above, that is three consecutive same-class
    # hits, uninterrupted, the streak this time is allowed to actually
    # accumulate.
    hit_4xx()
    hit_4xx()

    assert sub.active is False, (
        "three consecutive hard-4xx hits (uninterrupted by a class-resetting "
        "skip) must still disable via the 3-strike rule, even though earlier "
        "skips in the same stream kept resetting the streak and the wall "
        "clock"
    )
    assert sub.disabled_reason == "non_retryable_client_error"
    assert sub.consecutive_failures == 3


# ---------------------------------------------------------------------------
# Verify round-5 fix #8 (opus #5): an unrecognized/stale scope (a retired
# topic slug, unparseable bill ids) must disable the subscription VISIBLY,
# never quiet-run forever with last_error=null.
# ---------------------------------------------------------------------------


def test_unknown_scope_disables_the_subscription_instead_of_quiet_running():
    now = dt.datetime.now(dt.timezone.utc)

    def fake_fetch_batch(db, sub, *, watermark_seq):
        return None  # scope_clause returned None -- unrecognized/stale target

    sub = SimpleNamespace(
        id=uuid.uuid4(), url="https://example.test/hook", signing_secret="s3cr3t",
        last_seq=0, consecutive_failures=0, failing_since=None, next_attempt_at=None,
        active=True, disabled_reason=None, disabled_at=None, notify_pending=None,
        last_status=None, last_error=None, last_attempt_at=None, last_success_at=None,
    )
    db = _FakeDb()
    client = _FakeHttpClient(status=200)

    events_sent, failed = dw._drain_one(
        db, client, sub, watermark_seq=10, watermark_dt=now,
        deadline=time.monotonic() + 10, fetch_batch=fake_fetch_batch,
    )

    assert len(client.calls) == 0, "an unknown scope must never POST anything"
    assert sub.active is False, "an unknown scope must disable the subscription, not quiet-run (fix #8)"
    assert sub.disabled_reason == "unknown_scope"
    assert sub.notify_pending == "disabled"
    assert sub.last_attempt_at is not None


def test_unknown_scope_disable_preserves_a_pending_created_notice():
    """Same 'created' -> 'created_disabled' combining rule every other
    disable path already gets (round-3 fix #13) -- the unknown-scope disable
    path routes through `_notify_disabled` too, not a hand-rolled
    assignment that could bypass it."""
    now = dt.datetime.now(dt.timezone.utc)

    def fake_fetch_batch(db, sub, *, watermark_seq):
        return None

    sub = SimpleNamespace(
        id=uuid.uuid4(), url="https://example.test/hook", signing_secret="s3cr3t",
        last_seq=0, consecutive_failures=0, failing_since=None, next_attempt_at=None,
        active=True, disabled_reason=None, disabled_at=None, notify_pending="created",
        last_status=None, last_error=None, last_attempt_at=None, last_success_at=None,
    )
    db = _FakeDb()
    client = _FakeHttpClient(status=200)

    dw._drain_one(
        db, client, sub, watermark_seq=10, watermark_dt=now,
        deadline=time.monotonic() + 10, fetch_batch=fake_fetch_batch,
    )
    assert sub.notify_pending == "created_disabled"


# ---------------------------------------------------------------------------
# Verify round-5 fix #4's GC half: prune_creation_events, probe-guarded
# exactly like `_creation_events_table_exists`.
# ---------------------------------------------------------------------------


def test_prune_creation_events_is_a_noop_when_the_table_is_absent():
    class _FakeProbeDb:
        def execute(self, stmt, *a, **kw):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    assert dw.prune_creation_events(_FakeProbeDb()) == 0


# ---------------------------------------------------------------------------
# Verify round-7 fix #6 (opus LOW #5): the information_schema probes are
# schema-qualified -- an unqualified probe returns MultipleResultsFound (an
# uncaught crash) the moment a same-named table/constraint is visible in
# more than one schema on the search_path.
# ---------------------------------------------------------------------------


def test_creation_events_table_exists_probe_is_schema_qualified():
    class _CapturingDb:
        def __init__(self):
            self.last_stmt = None

        def execute(self, stmt, *a, **kw):
            self.last_stmt = str(stmt)
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    db = _CapturingDb()
    dw._creation_events_table_exists(db)
    assert "table_schema = current_schema()" in db.last_stmt


def test_notify_pending_supports_created_disabled_probe_is_schema_qualified():
    class _CapturingDb:
        def __init__(self):
            self.last_stmt = None

        def execute(self, stmt, *a, **kw):
            self.last_stmt = str(stmt)
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    db = _CapturingDb()
    dw._notify_pending_supports_created_disabled(db)
    assert "constraint_schema = current_schema()" in db.last_stmt


# ---------------------------------------------------------------------------
# Verify round-5 fix #2 (kimi #2): the advisory-lock connection must commit
# immediately after every acquire/re-check, or it can idle-in-transaction
# past `idle_in_transaction_session_timeout` and silently lose the lock.
# ---------------------------------------------------------------------------


class _FakeLockConn:
    def __init__(self, acquired: bool):
        self._acquired = acquired
        self.execute_calls = 0
        self.commit_calls = 0

    def execute(self, stmt, params=None):
        self.execute_calls += 1
        return SimpleNamespace(scalar_one=lambda: self._acquired)

    def commit(self):
        self.commit_calls += 1


def test_try_acquire_lock_commits_immediately_after_every_execute():
    conn = _FakeLockConn(acquired=True)
    result = dw._try_acquire_lock(conn, 12345)
    assert result is True
    assert conn.execute_calls == 1
    assert conn.commit_calls == 1, (
        "the connection must commit right after the acquire/re-check query "
        "(fix #2) -- an uncommitted transaction sitting on this connection "
        "is exactly what idle_in_transaction_session_timeout can kill, "
        "silently releasing the singleton lock mid-tick"
    )


def test_try_acquire_lock_commits_even_when_not_acquired():
    conn = _FakeLockConn(acquired=False)
    result = dw._try_acquire_lock(conn, 12345)
    assert result is False
    assert conn.commit_calls == 1


# ---------------------------------------------------------------------------
# DB-backed tests
# ---------------------------------------------------------------------------


def _webhook_tables_present() -> bool:
    db = get_session()
    try:
        return db.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'webhook_subscriptions'"
            )
        ).scalar_one_or_none() is not None
    finally:
        db.close()


requires_migration_0012 = pytest.mark.skipif(
    not _webhook_tables_present(),
    reason=(
        "migration 0012 (webhook_subscriptions/webhook_deliveries) has not "
        "been applied to this database. Applying it to prod is an "
        "orchestrator ship-gate (spec §8), not something this test suite "
        "does -- run `alembic upgrade head` (packages/schema) first, then "
        "re-run this file."
    ),
)


# ---------------------------------------------------------------------------
# Round-3 fix #11: last_seq must never be dragged BACKWARD by a concurrent
# quiet-run/success advance racing a newer reactivate?mode=skip watermark.
# ---------------------------------------------------------------------------


@requires_migration_0012
def test_advance_last_seq_never_drags_the_cursor_backward(fixture_jurisdiction):
    jurisdiction, _bill = fixture_jurisdiction
    db = get_session()
    try:
        sub = _make_subscription(
            db, url=f"https://{TEST_HOSTNAME}/advance-last-seq-hook",
            kind="jurisdiction", target=jurisdiction.abbreviation,
            verified=True, last_seq=100,
        )
        # Simulate a concurrent POST /webhooks/{id}/reactivate?mode=skip
        # fast-forwarding last_seq to a NEWER watermark, on a different
        # "session" (a direct SQL write, standing in for the API's own ORM
        # write on a different connection).
        db.execute(
            text("UPDATE webhook_subscriptions SET last_seq = 500 WHERE id = :id"),
            {"id": sub.id},
        )
        db.commit()

        # An in-flight tick's quiet-run advance computed against a STALE,
        # smaller watermark (300 < 500) must NOT drag the cursor back down.
        dw._advance_last_seq(db, sub, 300)

        stored = db.execute(
            text("SELECT last_seq FROM webhook_subscriptions WHERE id = :id"),
            {"id": sub.id},
        ).scalar_one()
        assert stored == 500, "GREATEST must keep the higher, newer value (fix #11)"
        assert sub.last_seq == 500, "the in-memory attribute must reflect the true DB value too"

        # And a genuinely higher new value still advances normally.
        dw._advance_last_seq(db, sub, 800)
        stored_after = db.execute(
            text("SELECT last_seq FROM webhook_subscriptions WHERE id = :id"),
            {"id": sub.id},
        ).scalar_one()
        assert stored_after == 800

        db.execute(text("DELETE FROM webhook_subscriptions WHERE id = :id"), {"id": sub.id})
        db.commit()
    finally:
        db.close()


def _generate_cert(hostname: str):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(issuer).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return key_pem, cert.public_bytes(serialization.Encoding.PEM)


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        self.server.requests.append(raw)
        self.server.behavior(self, raw)


@pytest.fixture()
def receiver():
    import tempfile

    servers = []

    def make(behavior):
        key_pem, cert_pem = _generate_cert(TEST_HOSTNAME)
        key_file = tempfile.NamedTemporaryFile(suffix=".key", delete=False)
        cert_file = tempfile.NamedTemporaryFile(suffix=".crt", delete=False)
        key_file.write(key_pem)
        cert_file.write(cert_pem)
        key_file.close()
        cert_file.close()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_file.name, key_file.name)
        server = http.server.HTTPServer(("127.0.0.1", 0), _RecordingHandler)
        server.behavior = behavior
        server.requests = []
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        return server, server.server_port, cert_pem

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def test_client(monkeypatch):
    """A SafeHttpClient wired to trust our throwaway per-test CA and resolve
    TEST_HOSTNAME to 127.0.0.1 -- same technique as
    packages/shared/tests/test_safe_http.py."""
    from billcommons_shared import safe_http

    state = {"cert_pem": None}
    real_create_default_context = ssl.create_default_context

    def fake_create_default_context(*a, **kw):
        context = real_create_default_context(*a, **kw)
        if state["cert_pem"] is not None:
            context.load_verify_locations(cadata=state["cert_pem"].decode("ascii"))
        return context

    monkeypatch.setattr(safe_http.ssl, "create_default_context", fake_create_default_context)

    def resolver(hostname):
        if hostname == TEST_HOSTNAME:
            return ["127.0.0.1"]
        raise safe_http.DnsFailure("unexpected_hostname_in_test")

    def make(port, cert_pem):
        state["cert_pem"] = cert_pem
        return safe_http.SafeHttpClient(resolver=resolver, port=port, address_policy=lambda ip: True)

    return make


@pytest.fixture()
def fixture_jurisdiction():
    """One throwaway jurisdiction/session/bill, ZZ-prefixed per the repo's
    own convention (apps/api/tests/test_feeds_atom.py) -- torn down in
    try/finally on a FRESH session, same reasoning as that file."""
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
        identifier="HB 1", identifier_norm="HB 1", title="A dispatcher test bill",
    )
    db.add(bill)
    db.flush()
    db.commit()
    try:
        yield jurisdiction, bill
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


def _make_subscription(
    db, *, url, kind="jurisdiction", target, verified=True, last_seq=0, event_kinds=None,
    host="webhook-dispatch-test.billcommons.internal", email="dispatcher-test@example.com",
):
    import hashlib
    import secrets

    manage_token = secrets.token_urlsafe(16)
    sub = WebhookSubscription(
        url=url, host=host,
        email=email, creator_ip="127.0.0.1",
        signing_secret=secrets.token_urlsafe(32),
        manage_token_hash=hashlib.sha256(manage_token.encode()).hexdigest(),
        kind=kind, target=target, event_kinds=event_kinds,
        verified=verified, active=True, last_seq=last_seq,
        challenge_token=secrets.token_urlsafe(16),
    )
    db.add(sub)
    db.commit()
    return sub


@pytest.fixture(autouse=True)
def _cleanup_test_subscriptions():
    yield
    if not _webhook_tables_present():
        return
    db = get_session()
    try:
        db.execute(text("DELETE FROM webhook_subscriptions WHERE email = 'dispatcher-test@example.com'"))
        db.commit()
    finally:
        db.close()


@requires_migration_0012
def test_challenge_exact_echo_accepts_substring_rejects(receiver, test_client, fixture_jurisdiction):
    jurisdiction, _bill = fixture_jurisdiction
    db = get_session()
    try:
        events = []

        def echo_more_than_token(handler, raw):
            body = json.loads(raw)
            events.append(body)
            handler.send_response(200)
            resp = (body["challenge"] + " and then some").encode()
            handler.send_header("Content-Length", str(len(resp)))
            handler.end_headers()
            handler.wfile.write(resp)

        server, port, cert_pem = receiver(echo_more_than_token)
        client = test_client(port, cert_pem)
        sub = _make_subscription(
            db, url=f"https://{TEST_HOSTNAME}/hook", kind="jurisdiction",
            target=jurisdiction.abbreviation, verified=False,
        )
        now = dt.datetime.now(dt.timezone.utc)
        dw._attempt_challenge(db, client, sub, now=now)
        db.refresh(sub)
        assert sub.verified is False, "substring echo must NOT verify"
        assert sub.challenge_attempts == 1
        assert sub.next_attempt_at is not None, "a failed challenge must back off (fix #1a)"
        assert sub.last_error == "challenge_mismatch", (
            "a 2xx with the wrong body must be labeled 'challenge_mismatch' "
            "(fix #11), not 'http_4xx' -- the endpoint DID accept the "
            "request, it just echoed the wrong body"
        )

        def echo_exact(handler, raw):
            body = json.loads(raw)
            handler.send_response(200)
            resp = body["challenge"].encode()
            handler.send_header("Content-Length", str(len(resp)))
            handler.end_headers()
            handler.wfile.write(resp)

        server2, port2, cert_pem2 = receiver(echo_exact)
        client2 = test_client(port2, cert_pem2)
        dw._attempt_challenge(db, client2, sub, now=now)
        db.refresh(sub)
        assert sub.verified is True
        assert sub.notify_pending == "created"
        assert sub.next_attempt_at is None, "a verified sub has no challenge backoff pending"
    finally:
        db.close()


@requires_migration_0012
def test_challenge_410_disables_immediately_instead_of_backing_off(
    receiver, test_client, fixture_jurisdiction
):
    """Round-7 fix #4 (codex MED #2): `_attempt_challenge` used to route
    EVERY non-2xx response, including a 410 Gone, through the generic
    backoff -- discarding `classify_http_status(410)`'s own
    `disable_immediately` signal and leaving a permanently-Gone challenge
    target getting POSTed every retry until the 24h challenge GC finally
    caught up, holding an unverified-quota slot the whole time. A single
    410 must instead disable the sub immediately: `active=False`,
    `disabled_reason='endpoint_gone'`, and a pending notice queued -- never
    a backoff/next_attempt_at."""
    jurisdiction, _bill = fixture_jurisdiction
    db = get_session()
    try:
        def gone(handler, raw):
            json.loads(raw)  # drain the body -- symmetry with the other receivers
            handler.send_response(410)
            handler.send_header("Content-Length", "0")
            handler.end_headers()

        server, port, cert_pem = receiver(gone)
        client = test_client(port, cert_pem)
        sub = _make_subscription(
            db, url=f"https://{TEST_HOSTNAME}/hook", kind="jurisdiction",
            target=jurisdiction.abbreviation, verified=False,
        )
        now = dt.datetime.now(dt.timezone.utc)
        dw._attempt_challenge(db, client, sub, now=now)
        db.refresh(sub)

        assert sub.verified is False
        assert sub.active is False, "a 410 challenge response must disable immediately"
        assert sub.disabled_reason == "endpoint_gone"
        assert sub.disabled_at is not None
        assert sub.notify_pending == "disabled"
        assert sub.challenge_token is None
        assert sub.next_attempt_at is None, (
            "a 410 disable must not ALSO schedule a backoff retry -- there is "
            "nothing left to retry"
        )
        assert sub.last_status == 410
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Round-3 fix #2: the per-domain quota is enforced AGAIN at verification
# promotion, not just at creation -- 11 subs on one domain, all created
# under quota (creation only counts VERIFIED subs, round-2 fix #7), then all
# 11 attempt to verify: exactly 10 must land verified, the 11th must be
# disabled with disabled_reason='domain_quota_exceeded'.
# ---------------------------------------------------------------------------


@requires_migration_0012
def test_domain_quota_enforced_at_verification_promotion_not_just_creation(
    receiver, test_client, fixture_jurisdiction
):
    """All 11 subs POST to the SAME real TLS receiver (`TEST_HOSTNAME`, via
    the standard `test_client` fixture -- its cert/SNI trust is already set
    up for that one hostname), but all share the SAME synthetic `host`
    COLUMN value (the quota-relevant one, set explicitly via
    `_make_subscription`'s `host=` param -- independent of the url's real
    hostname) so the per-domain quota recount at verification (fix #2) has
    something to bite on, while the actual network target stays the one
    hostname this test's TLS fixture already trusts."""
    jurisdiction, _bill = fixture_jurisdiction
    db = get_session()
    try:
        host = f"quota-promo-{uuid.uuid4().hex[:10]}.billcommons.internal"

        def echo_exact(handler, raw):
            body = json.loads(raw)
            handler.send_response(200)
            resp = body["challenge"].encode()
            handler.send_header("Content-Length", str(len(resp)))
            handler.end_headers()
            handler.wfile.write(resp)

        server, port, cert_pem = receiver(echo_exact)
        client = test_client(port, cert_pem)

        subs = [
            _make_subscription(
                db, url=f"https://{TEST_HOSTNAME}/hook-quota-promo-{i}", kind="jurisdiction",
                target=jurisdiction.abbreviation, verified=False, host=host,
            )
            for i in range(11)
        ]

        now = dt.datetime.now(dt.timezone.utc)
        for sub in subs:
            dw._attempt_challenge(db, client, sub, now=now)

        for sub in subs:
            db.refresh(sub)

        verified_count = sum(1 for s in subs if s.verified)
        disabled = [s for s in subs if not s.verified]
        assert verified_count == 10, (
            f"expected exactly 10 verified on one domain (fix #2), got {verified_count}"
        )
        assert len(disabled) == 1
        assert disabled[0].disabled_reason == "domain_quota_exceeded"
        assert disabled[0].active is False
        assert disabled[0].notify_pending == "disabled"

        db.execute(text("DELETE FROM webhook_subscriptions WHERE host = :h"), {"h": host})
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Round-4 fix #1: mirror round-3 fix #2's per-host recount with a GLOBAL one
# at promotion time -- an attacker spreading unverifiable-until-verified
# creations across many DISTINCT domains (each individually under the
# per-host cap) could all go on to verify in the same window and blow past
# MAX_ACTIVE_GLOBAL, which the creation-time check (round-3 fix #1, verified-
# only) cannot prevent for the same "wasn't verified yet at creation time"
# reason. Live DB rows are not empty-guaranteed (other suites run
# concurrently -- see apps/api/tests/test_webhooks_api.py's own docstring),
# so MAX_ACTIVE_GLOBAL is monkeypatched down RELATIVE to whatever is
# currently verified, exactly like that file's own global-quota tests do,
# rather than assuming a clean slate or actually creating 500 rows.
# ---------------------------------------------------------------------------


def _current_verified_global_count() -> int:
    db = get_session()
    try:
        return db.execute(
            text("SELECT count(*) FROM webhook_subscriptions WHERE active AND verified")
        ).scalar_one()
    finally:
        db.close()


@requires_migration_0012
def test_global_quota_enforced_at_verification_promotion_not_just_creation(
    receiver, test_client, fixture_jurisdiction, monkeypatch
):
    """500 verified (simulated via a monkeypatched-down cap, standing in for
    the literal 500) + one more pending unverified: the promotion that would
    push the global verified count OVER the cap must refuse -- disabled with
    `disabled_reason='global_quota_exceeded'`, not silently promoted past
    it."""
    jurisdiction, _bill = fixture_jurisdiction
    verified_now = _current_verified_global_count()
    monkeypatch.setattr(dw, "MAX_ACTIVE_GLOBAL", verified_now + 1)

    db = get_session()
    try:
        def echo_exact(handler, raw):
            body = json.loads(raw)
            handler.send_response(200)
            resp = body["challenge"].encode()
            handler.send_header("Content-Length", str(len(resp)))
            handler.end_headers()
            handler.wfile.write(resp)

        server, port, cert_pem = receiver(echo_exact)
        client = test_client(port, cert_pem)

        # Two subs, DIFFERENT hosts (so the per-host cap never bites here --
        # only the global one should), both hitting the one real TLS
        # receiver this test's fixture already trusts.
        host_a = f"global-quota-promo-a-{uuid.uuid4().hex[:10]}.billcommons.internal"
        host_b = f"global-quota-promo-b-{uuid.uuid4().hex[:10]}.billcommons.internal"
        sub_a = _make_subscription(
            db, url=f"https://{TEST_HOSTNAME}/hook-global-a", kind="jurisdiction",
            target=jurisdiction.abbreviation, verified=False, host=host_a,
        )
        sub_b = _make_subscription(
            db, url=f"https://{TEST_HOSTNAME}/hook-global-b", kind="jurisdiction",
            target=jurisdiction.abbreviation, verified=False, host=host_b,
        )

        now = dt.datetime.now(dt.timezone.utc)
        dw._attempt_challenge(db, client, sub_a, now=now)
        dw._attempt_challenge(db, client, sub_b, now=now)

        db.refresh(sub_a)
        db.refresh(sub_b)

        verified = [s for s in (sub_a, sub_b) if s.verified]
        refused = [s for s in (sub_a, sub_b) if not s.verified]
        assert len(verified) == 1, (
            f"exactly one of the two must land verified once the cap "
            f"(verified_now + 1 = {verified_now + 1}) is hit, got {len(verified)}"
        )
        assert len(refused) == 1
        assert refused[0].disabled_reason == "global_quota_exceeded"
        assert refused[0].active is False
        assert refused[0].notify_pending == "disabled"

        db.execute(
            text("DELETE FROM webhook_subscriptions WHERE id IN (:a, :b)"),
            {"a": sub_a.id, "b": sub_b.id},
        )
        db.commit()
    finally:
        db.close()


@requires_migration_0012
def test_empty_batch_advances_to_watermark_without_posting(receiver, test_client, fixture_jurisdiction):
    jurisdiction, _bill = fixture_jurisdiction
    db = get_session()
    try:
        watermark_seq, watermark_dt = dw.compute_watermark(db)

        posted = {"n": 0}

        def never_called(handler, raw):
            posted["n"] += 1
            handler.send_response(200)
            handler.send_header("Content-Length", "0")
            handler.end_headers()

        server, port, cert_pem = receiver(never_called)
        client = test_client(port, cert_pem)

        # A jurisdiction target that matches nothing recent (a jurisdiction
        # with zero bills at all) -- filtered scope is legitimately empty.
        empty_abbr = f"ZZ{uuid.uuid4().hex[:6].upper()}"
        empty_j = Jurisdiction(name=f"Test State {empty_abbr}", abbreviation=empty_abbr, classification="state")
        db.add(empty_j)
        db.commit()

        sub = _make_subscription(
            db, url=f"https://{TEST_HOSTNAME}/hook", kind="jurisdiction",
            target=empty_abbr, verified=True, last_seq=0,
        )
        sent, failed = dw._drain_one(
            db, client, sub, watermark_seq=watermark_seq, watermark_dt=watermark_dt,
            deadline=time.monotonic() + 5,
        )
        assert sent == 0
        assert failed is False
        assert posted["n"] == 0, "empty batch must not POST at all"
        db.refresh(sub)
        assert sub.last_seq == watermark_seq

        db.execute(text("DELETE FROM webhook_subscriptions WHERE id = :i"), {"i": sub.id})
        db.execute(text("DELETE FROM jurisdictions WHERE id = :i"), {"i": empty_j.id})
        db.commit()
    finally:
        db.close()


@requires_migration_0012
def test_crash_between_post_and_cursor_advance_resends(receiver, test_client, fixture_jurisdiction, monkeypatch):
    """Proves at-least-once: if the write side (WebhookDelivery row +
    last_seq advance) never commits after a successful POST, the next drain
    re-sends the SAME window rather than silently skipping it."""
    jurisdiction, bill = fixture_jurisdiction
    db = get_session()
    try:
        now = dt.datetime.now(dt.timezone.utc)
        event = BillEvent(bill_id=bill.id, kind="status", detail="test event", changed_at=now - dt.timedelta(seconds=dw.COMMIT_SAFETY_LAG_SECONDS + 30))
        db.add(event)
        db.commit()

        watermark_seq, watermark_dt = dw.compute_watermark(db)
        assert watermark_seq >= event.seq

        received = {"n": 0}

        def count_and_accept(handler, raw):
            received["n"] += 1
            handler.send_response(200)
            handler.send_header("Content-Length", "0")
            handler.end_headers()

        server, port, cert_pem = receiver(count_and_accept)
        client = test_client(port, cert_pem)

        sub = _make_subscription(
            db, url=f"https://{TEST_HOSTNAME}/hook", kind="jurisdiction",
            target=jurisdiction.abbreviation, verified=True, last_seq=event.seq - 1,
        )

        real_init = WebhookDelivery.__init__
        call_count = {"n": 0}

        def crashing_init(self, *a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated crash before the write transaction commits")
            return real_init(self, *a, **kw)

        monkeypatch.setattr(WebhookDelivery, "__init__", crashing_init)

        with pytest.raises(RuntimeError):
            dw._drain_one(
                db, client, sub, watermark_seq=watermark_seq, watermark_dt=watermark_dt,
                deadline=time.monotonic() + 5,
            )
        db.rollback()
        assert received["n"] == 1, "the POST DID happen before the simulated crash"

        fresh = db.get(WebhookSubscription, sub.id)
        assert fresh.last_seq == event.seq - 1, "cursor must NOT have advanced past the crash"

        monkeypatch.setattr(WebhookDelivery, "__init__", real_init)
        sent, failed = dw._drain_one(
            db, client, fresh, watermark_seq=watermark_seq, watermark_dt=watermark_dt,
            deadline=time.monotonic() + 5,
        )
        assert received["n"] == 2, "retry re-sent the same window -- at-least-once"
        assert sent == 1
        assert failed is False

        db.execute(text("DELETE FROM webhook_deliveries WHERE subscription_id = :i"), {"i": sub.id})
        db.execute(text("DELETE FROM webhook_subscriptions WHERE id = :i"), {"i": sub.id})
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Fix #1: challenges are gated on next_attempt_at and bounded by their own
# deadline -- a not-yet-due unverified sub must NOT be re-challenged, and
# run_challenges must stop as soon as its deadline passes.
# ---------------------------------------------------------------------------


@requires_migration_0012
def test_run_challenges_skips_a_sub_whose_next_attempt_at_is_in_the_future(receiver, test_client, fixture_jurisdiction):
    jurisdiction, _bill = fixture_jurisdiction
    db = get_session()
    try:
        posted = {"n": 0}

        def never_called(handler, raw):
            posted["n"] += 1
            handler.send_response(200)
            handler.send_header("Content-Length", "0")
            handler.end_headers()

        server, port, cert_pem = receiver(never_called)
        client = test_client(port, cert_pem)

        sub = _make_subscription(
            db, url=f"https://{TEST_HOSTNAME}/hook", kind="jurisdiction",
            target=jurisdiction.abbreviation, verified=False,
        )
        sub_id = sub.id
        now = dt.datetime.now(dt.timezone.utc)
        sub.next_attempt_at = now + dt.timedelta(hours=1)  # backed off, not due yet
        db.commit()

        # Batch item 13 (kimi L): NOT `assert processed == 0` -- `processed`
        # is a count over run_challenges' OWN unscoped, shared-live-DB-wide
        # SELECT (this suite runs concurrently with itself, see this file's
        # own module docstring), so another test's concurrently-due
        # unverified row landing in the same batch would inflate it and
        # flake this assertion for a reason unrelated to what THIS test
        # actually exercises. Scoped instead to a fresh SELECT on this
        # test's own `sub_id` -- the actual property under test (a sub
        # backed off into the future is untouched) either holds or doesn't,
        # regardless of what else concurrently shares the table.
        dw.run_challenges(db, client, now=now, deadline=time.monotonic() + 5)

        assert posted["n"] == 0, "a sub whose next_attempt_at is in the future must not be POSTed to"
        attempts, next_attempt_at = db.execute(
            text("SELECT challenge_attempts, next_attempt_at FROM webhook_subscriptions WHERE id = :id"),
            {"id": sub_id},
        ).one()
        assert attempts == 0, "a skipped-due-to-future-backoff sub must not record an attempt"
        assert next_attempt_at is not None, "its own backoff window must be untouched"
    finally:
        db.close()


@requires_migration_0012
def test_run_challenges_stops_at_its_own_deadline(receiver, test_client, fixture_jurisdiction):
    """Two due, unverified subs and a deadline that has ALREADY passed by
    the time the second one would be attempted: run_challenges must stop
    (fix #1c) rather than keep going past its own budget."""
    jurisdiction, _bill = fixture_jurisdiction
    db = get_session()
    try:
        posted = {"n": 0}

        def slow_but_counts(handler, raw):
            posted["n"] += 1
            handler.send_response(200)
            handler.send_header("Content-Length", "0")
            handler.end_headers()

        server, port, cert_pem = receiver(slow_but_counts)
        client = test_client(port, cert_pem)

        sub1 = _make_subscription(
            db, url=f"https://{TEST_HOSTNAME}/hook1", kind="jurisdiction",
            target=jurisdiction.abbreviation, verified=False,
        )
        sub2 = _make_subscription(
            db, url=f"https://{TEST_HOSTNAME}/hook2", kind="jurisdiction",
            target=jurisdiction.abbreviation, verified=False,
        )
        now = dt.datetime.now(dt.timezone.utc)

        # A deadline in the past -- the very first loop iteration's
        # `time.monotonic() >= deadline` check must trip immediately.
        processed = dw.run_challenges(db, client, now=now, deadline=time.monotonic() - 1)

        assert processed == 0, "a deadline already in the past must stop the loop before the first attempt"
        assert posted["n"] == 0

        db.execute(text("DELETE FROM webhook_subscriptions WHERE id IN (:a, :b)"), {"a": sub1.id, "b": sub2.id})
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Verify round-5 fix #1 (kimi HIGH #1): a crash INSIDE `_attempt_challenge`,
# racing a concurrent DELETE of the exact same row, must not crash the
# whole tick from inside the crash-isolation handler itself.
# ---------------------------------------------------------------------------


@requires_migration_0012
def test_run_challenges_survives_a_crash_racing_a_concurrent_delete(fixture_jurisdiction, monkeypatch):
    """Simulates exactly the bug the fix describes: `_attempt_challenge`
    raises a generic exception (standing in for any internal bug) AFTER the
    row it was handling has been deleted concurrently (via a real DELETE on
    a separate session/connection, standing in for
    `DELETE /api/v1/webhooks/{id}` racing this tick). Pre-fix, the recovery
    branch would touch `sub.id` on the now-rolled-back-and-expired `sub`
    object and raise ObjectDeletedError from INSIDE the except-block,
    escaping `run_challenges` entirely and killing the tick. Post-fix, the
    recovery uses the captured `sub_id` local and simply finds nothing to
    update -- `run_challenges` must return normally."""
    jurisdiction, _bill = fixture_jurisdiction
    db = get_session()
    try:
        sub = _make_subscription(
            db, url=f"https://{TEST_HOSTNAME}/crash-race-hook", kind="jurisdiction",
            target=jurisdiction.abbreviation, verified=False,
        )
        sub_id = sub.id
        # Batch item 13 (kimi L): captured BEFORE patching, and dispatched
        # on below -- this suite runs concurrently with itself (shared live
        # DB, see this file's own module docstring), so `run_challenges`'
        # own unscoped SELECT could pick up ANOTHER test's concurrently-due
        # unverified row in the SAME batch. Without this id check, patching
        # `dw._attempt_challenge` globally would crash-and-DELETE this
        # test's `sub_id` for EVERY row in the batch regardless of which
        # one it actually represents, corrupting unrelated tests. Scoped:
        # only THIS test's own row triggers the simulated crash; any other
        # row in the batch is handled by the real function, unaffected.
        real_attempt_challenge = dw._attempt_challenge

        def crashing_attempt_challenge(inner_db, http_client, inner_sub, *, now, column_available=False):
            if inner_sub.id != sub_id:
                return real_attempt_challenge(
                    inner_db, http_client, inner_sub, now=now, column_available=column_available
                )
            # Delete the row for real, on a SEPARATE session/connection --
            # exactly what a concurrent `DELETE /api/v1/webhooks/{id}`
            # would do -- then raise, simulating an internal bug reached
            # AFTER that race already happened.
            other = get_session()
            try:
                other.execute(text("DELETE FROM webhook_subscriptions WHERE id = :id"), {"id": sub_id})
                other.commit()
            finally:
                other.close()
            raise RuntimeError("simulated internal bug, racing a concurrent delete")

        monkeypatch.setattr(dw, "_attempt_challenge", crashing_attempt_challenge)

        now = dt.datetime.now(dt.timezone.utc)
        # Must not raise -- this IS the assertion. A pre-fix build raises
        # ObjectDeletedError out of this call. `processed` itself is NOT
        # asserted (batch item 13, same "unscoped shared-DB count" flake
        # reasoning as the sibling test above) -- the scoped `remaining`
        # check just below is the real, non-flaky proof this test's own
        # row was handled and stayed deleted.
        dw.run_challenges(db, None, now=now, deadline=time.monotonic() + 10)

        remaining = db.execute(
            text("SELECT count(*) FROM webhook_subscriptions WHERE id = :id"), {"id": sub_id}
        ).scalar_one()
        assert remaining == 0, "the row must stay deleted -- the crash-recovery path must not resurrect it"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Verify round-6 fix #1 (codex MED #2): a quota-disabled, still-unverified
# subscription (active=False, verified=False) falls OUTSIDE run_challenges'
# own SELECT (active.is_(True)) and outside every other GC path -- unbounded
# growth. `gc_unverified_inactive` closes that.
# ---------------------------------------------------------------------------


@requires_migration_0012
def test_gc_unverified_inactive_deletes_rows_older_than_7d_but_not_6d(fixture_jurisdiction):
    jurisdiction, _bill = fixture_jurisdiction
    db = get_session()
    try:
        now = dt.datetime.now(dt.timezone.utc)
        old_sub = _make_subscription(
            db, url=f"https://{TEST_HOSTNAME}/gc-old-hook", kind="jurisdiction",
            target=jurisdiction.abbreviation, verified=False,
        )
        young_sub = _make_subscription(
            db, url=f"https://{TEST_HOSTNAME}/gc-young-hook", kind="jurisdiction",
            target=jurisdiction.abbreviation, verified=False,
        )
        for sub_id, age in ((old_sub.id, dt.timedelta(days=8)), (young_sub.id, dt.timedelta(days=6))):
            db.execute(
                text(
                    "UPDATE webhook_subscriptions SET active = false, "
                    "disabled_reason = 'domain_quota_exceeded', disabled_at = :at "
                    "WHERE id = :id"
                ),
                {"at": now - age, "id": sub_id},
            )
        db.commit()

        deleted = dw.gc_unverified_inactive(db, now=now)
        assert deleted == 1, "only the >7d row must be deleted this pass"

        old_remaining = db.execute(
            text("SELECT count(*) FROM webhook_subscriptions WHERE id = :id"), {"id": old_sub.id}
        ).scalar_one()
        young_remaining = db.execute(
            text("SELECT count(*) FROM webhook_subscriptions WHERE id = :id"), {"id": young_sub.id}
        ).scalar_one()
        assert old_remaining == 0, "an 8-day-old quota-disabled, unverified row must be GC'd"
        assert young_remaining == 1, "a 6-day-old quota-disabled, unverified row must survive"

        db.execute(text("DELETE FROM webhook_subscriptions WHERE id = :id"), {"id": young_sub.id})
        db.commit()
    finally:
        db.close()


def test_gc_unverified_inactive_leaves_verified_subs_alone():
    """A VERIFIED subscription that later gets disabled must never be swept
    by this GC, even if it is old and inactive -- only never-verified rows
    are pure dead weight (see `gc_unverified_inactive`'s own docstring)."""

    class _FakeGcDb:
        def __init__(self):
            self.executed = []

        def execute(self, stmt, params=None):
            self.executed.append((stmt, params))
            return SimpleNamespace(rowcount=0)

        def commit(self):
            pass

    fake_db = _FakeGcDb()
    now = dt.datetime.now(dt.timezone.utc)
    deleted = dw.gc_unverified_inactive(fake_db, now=now)
    assert deleted == 0
    stmt, _params = fake_db.executed[0]
    assert "verified" in str(stmt).lower(), "the DELETE must filter on verified=False"


@requires_migration_0012
def test_run_challenges_also_gcs_stale_quota_disabled_subs(fixture_jurisdiction):
    """`gc_unverified_inactive` runs as part of `run_challenges` itself (not
    just as a standalone function) -- an 8-day-old quota-disabled sub is
    gone after one `run_challenges` call, with no verification traffic
    needed to trigger it."""
    jurisdiction, _bill = fixture_jurisdiction
    db = get_session()
    try:
        now = dt.datetime.now(dt.timezone.utc)
        sub = _make_subscription(
            db, url=f"https://{TEST_HOSTNAME}/run-challenges-gc-hook", kind="jurisdiction",
            target=jurisdiction.abbreviation, verified=False,
        )
        db.execute(
            text(
                "UPDATE webhook_subscriptions SET active = false, "
                "disabled_reason = 'global_quota_exceeded', disabled_at = :at "
                "WHERE id = :id"
            ),
            {"at": now - dt.timedelta(days=8), "id": sub.id},
        )
        db.commit()

        dw.run_challenges(db, None, now=now, deadline=time.monotonic() + 10)

        remaining = db.execute(
            text("SELECT count(*) FROM webhook_subscriptions WHERE id = :id"), {"id": sub.id}
        ).scalar_one()
        assert remaining == 0
    finally:
        db.close()


@requires_migration_0012
def test_run_challenges_disables_after_the_attempt_cap_is_reached(fixture_jurisdiction):
    """A challenge target that always transport-fails (connection refused,
    standing in for any transport failure class): repeated `run_challenges`
    calls back the sub off further out each time (`backoff_delay`), so a
    real tick-cadence loop would take hours to reach the cap -- this test
    drives the SAME attempt/backoff/disable machinery directly via repeated
    `_attempt_challenge` calls (bypassing the backoff wait, not the disable
    logic itself) up to CHALLENGE_ATTEMPT_DISABLE_AFTER, then proves
    run_challenges' own GC path (`gc_unverified_inactive`) reaps the now-
    disabled row on its very next call -- both halves of fix #5 in one
    scoped, non-flaky test (own row only, per batch item 13's convention
    above)."""
    jurisdiction, _bill = fixture_jurisdiction
    db = get_session()
    try:
        sub = _make_subscription(
            db, url=f"https://{TEST_HOSTNAME}:1/always-refused", kind="jurisdiction",
            target=jurisdiction.abbreviation, verified=False,
        )
        sub_id = sub.id
        now = dt.datetime.now(dt.timezone.utc)

        from billcommons_shared import safe_http

        class _AlwaysRefusingClient:
            def fetch(self, *a, **kw):
                raise safe_http.ConnectionFailure("refused")

        for _ in range(dw.CHALLENGE_ATTEMPT_DISABLE_AFTER):
            dw._attempt_challenge(db, _AlwaysRefusingClient(), sub, now=now)

        attempts, active, disabled_reason = db.execute(
            text(
                "SELECT challenge_attempts, active, disabled_reason "
                "FROM webhook_subscriptions WHERE id = :id"
            ),
            {"id": sub_id},
        ).one()
        assert attempts == dw.CHALLENGE_ATTEMPT_DISABLE_AFTER
        assert active is False, "the sub must be disabled once the attempt cap is reached"
        assert disabled_reason == "challenge_timeout"

        # gc_unverified_inactive only reaps rows with disabled_at set AND
        # older than UNVERIFIED_INACTIVE_RETENTION_DAYS -- freshly disabled
        # (disabled_at == now) must NOT be swept yet; back-date it to prove
        # run_challenges' own GC call (folded into every call, fix #1 round
        # 6) reaps it once it genuinely ages out, same as any other quota-
        # disabled row.
        db.execute(
            text(
                "UPDATE webhook_subscriptions SET disabled_at = :old "
                "WHERE id = :id"
            ),
            {"old": now - dt.timedelta(days=8), "id": sub_id},
        )
        db.commit()

        dw.run_challenges(db, None, now=now, deadline=time.monotonic() + 5)

        remaining = db.execute(
            text("SELECT count(*) FROM webhook_subscriptions WHERE id = :id"), {"id": sub_id}
        ).scalar_one()
        assert remaining == 0, "the challenge-timeout-disabled row must be GC'd like any other"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Verify round-6 fix #2 (codex MED #3): a kind='jurisdiction' target whose
# stored abbreviation resolves to NO Jurisdiction row must disable visibly
# (unknown_scope), not quiet-run forever as an empty-matching predicate.
# ---------------------------------------------------------------------------


@requires_migration_0012
def test_scope_clause_jurisdiction_stale_abbreviation_returns_none_with_real_db():
    db = get_session()
    try:
        sub = SimpleNamespace(kind="jurisdiction", target=f"ZZ{uuid.uuid4().hex[:6].upper()}")
        assert dw.scope_clause(sub, db) is None
    finally:
        db.close()


@requires_migration_0012
def test_scope_clause_jurisdiction_valid_abbreviation_returns_a_clause_with_real_db(fixture_jurisdiction):
    jurisdiction, _bill = fixture_jurisdiction
    db = get_session()
    try:
        sub = SimpleNamespace(kind="jurisdiction", target=jurisdiction.abbreviation)
        assert dw.scope_clause(sub, db) is not None
    finally:
        db.close()


@requires_migration_0012
def test_scope_clause_topic_with_stale_jurisdiction_scope_returns_none_with_real_db():
    sub = SimpleNamespace(kind="topic", target=f"artificial-intelligence:ZZ{uuid.uuid4().hex[:6].upper()}")
    db = get_session()
    try:
        assert dw.scope_clause(sub, db) is None
    finally:
        db.close()


def test_scope_clause_without_a_db_skips_the_jurisdiction_existence_check():
    """`db=None` (the default -- every existing pure-function test in this
    file relies on it) preserves the pre-fix behavior: a syntactically
    valid clause is still returned even for a bogus abbreviation, since
    there is no session to check existence against."""
    sub = SimpleNamespace(kind="jurisdiction", target="not-a-real-abbreviation")
    assert dw.scope_clause(sub) is not None


# ---------------------------------------------------------------------------
# Verify round-6 fix #4 (opus MED #3): migration 0013 probe guard. Without
# it, `_notify_disabled` writing 'created_disabled' on a pre-0013 database
# raises IntegrityError at commit, and the crash handler around every
# caller rolls back the ENTIRE disable this call was trying to persist.
# ---------------------------------------------------------------------------


def test_notify_pending_supports_created_disabled_probe_false_when_0013_absent():
    class _FakeProbeDb:
        def execute(self, stmt, *a, **kw):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    assert dw._notify_pending_supports_created_disabled(_FakeProbeDb()) is False


def test_notify_pending_supports_created_disabled_probe_true_when_0013_present():
    class _FakeProbeDb:
        def execute(self, stmt, *a, **kw):
            return SimpleNamespace(scalar_one_or_none=lambda: 1)

    assert dw._notify_pending_supports_created_disabled(_FakeProbeDb()) is True


def test_notify_disabled_falls_back_to_plain_disabled_when_probe_is_false():
    sub = SimpleNamespace(id=uuid.uuid4(), notify_pending="created")
    dw._notify_disabled(sub, supports_created_disabled=False)
    assert sub.notify_pending == "disabled", (
        "without migration 0013, writing 'created_disabled' would raise "
        "IntegrityError at commit and roll back the WHOLE disable (fix #4) "
        "-- must fall back to plain, 0012-legal 'disabled'"
    )


def test_notify_disabled_still_combines_when_probe_is_true():
    sub = SimpleNamespace(id=uuid.uuid4(), notify_pending="created")
    dw._notify_disabled(sub, supports_created_disabled=True)
    assert sub.notify_pending == "created_disabled"


def test_notify_disabled_default_still_combines_for_every_existing_caller():
    """The default (`supports_created_disabled=True`) preserves every
    pre-fix caller's behavior unchanged -- only `run_tick`'s real,
    probe-threaded call sites ever pass `False`."""
    sub = SimpleNamespace(id=uuid.uuid4(), notify_pending="created_disabled")
    dw._notify_disabled(sub)
    assert sub.notify_pending == "created_disabled"


def test_drain_one_unknown_scope_disable_respects_supports_created_disabled_false():
    now = dt.datetime.now(dt.timezone.utc)

    def fake_fetch_batch(db, sub, *, watermark_seq):
        return None  # scope_clause returned None -- unrecognized/stale target

    sub = SimpleNamespace(
        id=uuid.uuid4(), url="https://example.test/hook", signing_secret="s3cr3t",
        last_seq=0, consecutive_failures=0, failing_since=None, next_attempt_at=None,
        active=True, disabled_reason=None, disabled_at=None, notify_pending="created",
        last_status=None, last_error=None, last_attempt_at=None, last_success_at=None,
    )
    db = _FakeDb()
    client = _FakeHttpClient(status=200)

    dw._drain_one(
        db, client, sub, watermark_seq=10, watermark_dt=now,
        deadline=time.monotonic() + 10, fetch_batch=fake_fetch_batch,
        supports_created_disabled=False,
    )

    assert sub.active is False
    assert sub.disabled_reason == "unknown_scope"
    assert sub.notify_pending == "disabled", (
        "with the 0013 probe False, the unknown_scope disable path must "
        "fall back to plain 'disabled', not 'created_disabled' (fix #4)"
    )
