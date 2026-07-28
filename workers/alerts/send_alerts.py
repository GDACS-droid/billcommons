#!/usr/bin/env python3
"""Nightly topic-digest sender for Bill Commons email alerts.

Runs on the box (systemd user timer `billcommons-alerts.timer`), NOT on
Railway: the Resend key and DB credentials live in ~/.config/billcommons/
on this machine, and shipping them into a cloud env var to save a timer was
judged the worse trade.

Sends via Resend from alerts@billcommons.org (domain verified on the Resend
account 2026-07-28) — subscriber mail must come from the product's own domain,
not a personal Gmail.

For each active subscription it walks bill_events from the subscription's
private cursor (`last_seq`) to the feed watermark, keeps events whose bill is
in the subscribed topic, and sends one digest email per subscription with
anything new. The cursor advances only after Gmail accepts the message, so a
crash re-sends a digest rather than silently skipping it -- duplicate email
is annoying, silent loss is a broken product.

A BRAND-NEW subscription fast-forwards to the watermark without sending:
"here is everything that ever happened" is not a useful first email.

Usage:
    send_alerts.py            # normal nightly run
    send_alerts.py --dry-run  # print what would be sent, send nothing
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import psycopg

DB_CONFIG = Path.home() / ".config/billcommons/railway-pg.json"
RESEND_CONFIG = Path.home() / ".config/billcommons/resend.json"
FROM_ADDR = "Bill Commons <alerts@billcommons.org>"
API_BASE = "https://api.billcommons.org"
SITE = "https://billcommons.org"

# Mirrors billcommons_api.routers.changes.COMMIT_SAFETY_LAG_SECONDS -- the
# sender must never read the head of bill_events for the same reason the API
# doesn't (seq visible out of order across commits).
COMMIT_SAFETY_LAG_SECONDS = 120

# Digest at most this many events per email; anything beyond is summarized as
# a count with a link. An adjournment sweep can retire hundreds of a topic's
# bills in one night and nobody reads a 400-row email.
MAX_EVENTS_PER_DIGEST = 30

KIND_LABEL = {
    "created": "New bill",
    "status": "Status change",
    "actions": "New action",
    "sponsors": "Sponsor change",
    "text": "Bill text available",
    "metadata": "Details updated",
}


def get_resend_key() -> str:
    return json.loads(RESEND_CONFIG.read_text())["api_key"]


def resend_send(
    api_key: str, to_addr: str, subject: str, html: str, text_body: str, unsub_url: str
) -> str:
    payload = {
        "from": FROM_ADDR,
        "to": [to_addr],
        "subject": subject,
        "html": html,
        "text": text_body,
        # Header-level unsubscribe so mail clients surface their own
        # unsubscribe affordance; the endpoint is GET-only, so no
        # one-click POST variant is advertised.
        "headers": {"List-Unsubscribe": f"<{unsub_url}>"},
    }
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Resend's edge 403s the default Python-urllib user agent.
            "User-Agent": "billcommons-alerts/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["id"]


def topic_membership_sql() -> dict[str, str]:
    """Per-topic SQL predicate over bills b, mirroring the API's TOPICS table
    (billcommons_api.routers.topics). Duplicated by value rather than imported
    because this script runs outside the API's virtualenv; the contract test
    in apps/api/tests keeps the two in sync."""
    return {
        "artificial-intelligence": (
            "(lower(b.title) LIKE '%%artificial intelligence%%' OR EXISTS ("
            "SELECT 1 FROM bill_subjects s WHERE s.bill_id = b.id AND "
            "lower(s.subject) LIKE '%%artificial intelligence%%'))"
        ),
        "data-privacy": (
            "(lower(b.title) LIKE '%%data privacy%%' OR lower(b.title) LIKE '%%consumer privacy%%'"
            " OR lower(b.title) LIKE '%%personal data%%' OR lower(b.title) LIKE '%%biometric%%'"
            " OR EXISTS (SELECT 1 FROM bill_subjects s WHERE s.bill_id = b.id AND"
            " lower(s.subject) LIKE '%%data privacy%%'))"
        ),
        "cryptocurrency": (
            "(lower(b.title) LIKE '%%cryptocurrency%%' OR lower(b.title) LIKE '%%digital asset%%'"
            " OR lower(b.title) LIKE '%%blockchain%%' OR lower(b.title) LIKE '%%virtual currency%%'"
            " OR EXISTS (SELECT 1 FROM bill_subjects s WHERE s.bill_id = b.id AND"
            " (lower(s.subject) LIKE '%%cryptocurrency%%' OR lower(s.subject) LIKE '%%blockchain%%')))"
        ),
    }


def render_digest(topic: str, events: list[dict], total: int, unsub: str) -> tuple[str, str, str]:
    """Returns (subject, html, text)."""
    n = total
    subject = f"[Bill Commons] {n} update{'s' if n != 1 else ''} on {topic.replace('-', ' ')} bills"
    lines_html, lines_text = [], []
    for e in events:
        label = KIND_LABEL.get(e["kind"], e["kind"])
        detail = f" — {e['detail']}" if e["detail"] else ""
        url = f"{SITE}/bills/{e['bill_id']}"
        head = f"{e['jur']} {e['identifier']}"
        lines_html.append(
            f'<li style="margin-bottom:10px"><a href="{url}" style="font-weight:600">{head}</a>'
            f" — {label}{detail}<br>"
            f'<span style="color:#555;font-size:13px">{e["title"][:160]}</span></li>'
        )
        lines_text.append(f"* {head} — {label}{detail}\n  {e['title'][:160]}\n  {url}")
    more = ""
    if total > len(events):
        more = (
            f'<p><a href="{SITE}/topics/{topic}">…and {total - len(events)} more — '
            f"see the full tracker</a></p>"
        )
    html = f"""<div style="font-family:system-ui,sans-serif;max-width:620px;margin:0 auto">
<h2 style="margin-bottom:4px">Bill Commons</h2>
<p style="color:#555;margin-top:0">What moved on <a href="{SITE}/topics/{topic}">{topic.replace('-', ' ')}</a> since your last digest:</p>
<ul style="padding-left:18px">{''.join(lines_html)}</ul>
{more}
<hr style="border:none;border-top:1px solid #ddd;margin:24px 0">
<p style="color:#888;font-size:12px">Free and open source — <a href="{SITE}">billcommons.org</a>.
<a href="{unsub}">Unsubscribe</a> any time.</p>
</div>"""
    text_body = (
        f"Bill Commons — what moved on {topic.replace('-', ' ')}:\n\n"
        + "\n\n".join(lines_text)
        + (f"\n\n…and {total - len(events)} more: {SITE}/topics/{topic}" if total > len(events) else "")
        + f"\n\nUnsubscribe: {unsub}\n"
    )
    return subject, html, text_body


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    url = json.loads(DB_CONFIG.read_text())["DATABASE_PUBLIC_URL"]
    membership = topic_membership_sql()
    sent = skipped = fast_forwarded = 0
    api_key: str | None = None

    with psycopg.connect(url, connect_timeout=30) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT coalesce(max(seq), 0) FROM bill_events "
                "WHERE changed_at <= now() - make_interval(secs => %s)",
                (COMMIT_SAFETY_LAG_SECONDS,),
            )
            watermark = cur.fetchone()[0]

            cur.execute(
                "SELECT id, email, target, unsubscribe_token, last_seq "
                "FROM alert_subscriptions WHERE active AND kind = 'topic' "
                "ORDER BY email"
            )
            subs = cur.fetchall()

        for sub_id, email, target, unsub_token, last_seq in subs:
            predicate = membership.get(target)
            if predicate is None:
                print(f"SKIP {email} {target}: unknown topic", flush=True)
                continue
            if last_seq == 0:
                # New subscriber: start from now, don't replay history.
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE alert_subscriptions SET last_seq = %s WHERE id = %s",
                        (watermark, sub_id),
                    )
                conn.commit()
                fast_forwarded += 1
                continue

            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT e.seq, e.kind, e.detail, b.id, b.identifier, b.title,
                           j.abbreviation
                    FROM bill_events e
                    JOIN bills b ON b.id = e.bill_id
                    JOIN jurisdictions j ON j.id = b.jurisdiction_id
                    WHERE e.seq > %s AND e.seq <= %s AND {predicate}
                    ORDER BY e.seq
                    """,
                    (last_seq, watermark),
                )
                rows = cur.fetchall()

            if not rows:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE alert_subscriptions SET last_seq = %s WHERE id = %s",
                        (watermark, sub_id),
                    )
                conn.commit()
                skipped += 1
                continue

            events = [
                {
                    "kind": kind,
                    "detail": detail,
                    "bill_id": bill_id,
                    "identifier": identifier,
                    "title": title,
                    "jur": jur,
                }
                for _, kind, detail, bill_id, identifier, title, jur in rows[:MAX_EVENTS_PER_DIGEST]
            ]
            unsub_url = f"{API_BASE}/api/v1/alerts/unsubscribe?token={unsub_token}"
            subject, html, text_body = render_digest(target, events, len(rows), unsub_url)

            if args.dry_run:
                print(f"DRY {email}: {subject} ({len(rows)} events)", flush=True)
                continue

            if api_key is None:
                api_key = get_resend_key()
            try:
                msg_id = resend_send(api_key, email, subject, html, text_body, unsub_url)
            except Exception as exc:  # noqa: BLE001 - one bad address must not kill the run
                print(f"FAIL {email}: {exc}", flush=True)
                continue
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE alert_subscriptions SET last_seq = %s WHERE id = %s",
                    (watermark, sub_id),
                )
            conn.commit()
            sent += 1
            print(f"SENT {email} {target}: {len(rows)} events (resend {msg_id})", flush=True)

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(
        f"{stamp} done: {sent} sent, {skipped} quiet, {fast_forwarded} new subscriber(s) fast-forwarded",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
