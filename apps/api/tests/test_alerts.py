"""/api/v1/alerts + the sender's topic contract.

The subscribe endpoint is the site's lead capture; the failure that matters
is silent -- an address accepted for a topic the sender doesn't know about
would sit subscribed forever receiving nothing. So the load-bearing test here
pins the API's topic table and the box-side sender's SQL membership map to
the same set of slugs and patterns.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from billcommons_api.routers.topics import TOPICS
from billcommons_shared.db import get_session

SENDER = Path(__file__).resolve().parents[3] / "workers/alerts/send_alerts.py"

_EMAIL_DOMAIN = "alerts-contract-test.example.com"


def _fresh_email() -> str:
    return f"test-{uuid.uuid4().hex[:10]}@{_EMAIL_DOMAIN}"


@pytest.fixture(autouse=True)
def _cleanup_test_subscriptions():
    """These tests run against the live DB the nightly sender reads; a leaked
    row would have it attempting delivery to a fake address every night."""
    yield
    db = get_session()
    try:
        db.execute(
            text("DELETE FROM alert_subscriptions WHERE email LIKE :pat"),
            {"pat": f"%@{_EMAIL_DOMAIN}"},
        )
        db.commit()
    finally:
        db.close()


def test_subscribe_then_resubscribe_is_idempotent(client):
    email = _fresh_email()
    body = {"email": email, "kind": "topic", "target": "artificial-intelligence"}
    first = client.post("/api/v1/alerts/subscribe", json=body)
    assert first.status_code == 201
    assert first.json()["subscribed"] is True

    again = client.post("/api/v1/alerts/subscribe", json=body)
    assert again.status_code == 201, "re-subscribing must not error"


def test_unknown_topic_is_rejected_loudly(client):
    """Accepting it would be worse than erroring: the address would be
    subscribed to a digest no sender will ever produce."""
    resp = client.post(
        "/api/v1/alerts/subscribe",
        json={"email": _fresh_email(), "kind": "topic", "target": "nonexistent-topic"},
    )
    assert resp.status_code == 422


def test_garbage_email_is_rejected(client):
    resp = client.post(
        "/api/v1/alerts/subscribe",
        json={"email": "not an email", "kind": "topic", "target": "artificial-intelligence"},
    )
    assert resp.status_code == 422


def test_unsubscribe_with_unknown_token_is_a_404(client):
    resp = client.get("/api/v1/alerts/unsubscribe", params={"token": "x" * 40})
    assert resp.status_code == 404


def test_sender_membership_map_matches_the_api_topics():
    """The nightly sender duplicates topic membership as raw SQL because it
    runs outside the API's env. If someone adds a topic to the API and not to
    the sender, subscribers to it would never get a digest -- silently. This
    pins the two by value: same slugs, and every API pattern string appears in
    the sender's predicate for that slug."""
    source = SENDER.read_text()
    match = re.search(r"def topic_membership_sql.*?return \{(.*?)\n    \}", source, re.S)
    assert match, "sender's topic_membership_sql not found"
    sql_block = match.group(1)

    sender_slugs = set(re.findall(r'"([a-z-]+)": \(', sql_block))
    assert sender_slugs == set(TOPICS), (
        f"API topics {sorted(TOPICS)} != sender topics {sorted(sender_slugs)}"
    )

    # Split the dict body into per-slug segments so a pattern present under
    # the WRONG topic still fails.
    boundaries = sorted(
        (sql_block.index(f'"{slug}": ('), slug) for slug in sender_slugs
    )
    segments = {
        slug: sql_block[start : boundaries[i + 1][0] if i + 1 < len(boundaries) else len(sql_block)]
        for i, (start, slug) in enumerate(boundaries)
    }
    for slug, topic in TOPICS.items():
        for pattern in (*topic.title_patterns, *topic.subject_patterns):
            assert pattern in segments[slug], (
                f"sender is missing pattern {pattern!r} for topic {slug!r}"
            )
