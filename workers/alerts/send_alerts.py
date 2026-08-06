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

Also piggybacks webhook lifecycle notices ("webhook created" /
"webhook auto-disabled") for workers/webhooks/dispatch_webhooks.py -- that
worker runs on Railway and has no Resend key by design (see its own module
docstring: an SSRF escape from a box process reaches the home LAN/tailnet, an
SSRF escape from a disposable Railway container does not). The dispatcher only
sets `webhook_subscriptions.notify_pending`; this script drains it, the same
box-has-the-mail-creds split alerts already use. Guarded by an
information_schema probe so it no-ops cleanly until migration 0012 exists
-- see `has_scope`'s probe below for the exact same pattern already in this
file for migration 0011.

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

# Mirrors billcommons_shared.watermark.COMMIT_SAFETY_LAG_SECONDS (single
# source of truth for every OTHER reader of bill_events) -- the sender must
# never read the head of bill_events for the same reason the API doesn't
# (seq visible out of order across commits). A literal copy, not an import,
# for the same reason topic_membership_sql() below is a literal copy of the
# API's topic registry: this script runs straight from the working tree via
# a systemd timer with no deploy step and deliberately does not depend on
# billcommons_shared (see this module's own docstring). Kept in sync by
# apps/api/tests' own contract test
# (test_alerts_sender_watermark_matches_shared_constant), the same
# duplicate-value-plus-contract-test shape test_sender_membership_map_
# matches_the_api_topics already uses for the topic predicates below.
#
# Value + empirical basis: see billcommons_shared/watermark.py's docstring
# (2026-08-04 pre-ship measurement, spec §8's D14 gate: 452,013 bill_events
# rows, max observed skew 98.2s, p99.99 97.1s; 240s ~= 2.4x observed max).
COMMIT_SAFETY_LAG_SECONDS = 240

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
    "votes": "Vote recorded",
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


def resend_send_plain(api_key: str, to_addr: str, subject: str, html: str, text_body: str) -> str:
    """Like `resend_send` but without a List-Unsubscribe header -- a webhook
    lifecycle notice is not a digest subscription and has no unsubscribe
    link to advertise."""
    payload = {"from": FROM_ADDR, "to": [to_addr], "subject": subject, "html": html, "text": text_body}
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "billcommons-alerts/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["id"]


def render_webhook_notice(kind: str, sub_id: str, scope: str, disabled_reason: str | None) -> tuple[str, str, str]:
    """Returns (subject, html, text) for one of the two webhook lifecycle
    messages -- NEVER the signing_secret or manage_token, which this script
    has no access to anyway (only the dispatcher and the API touch those
    columns' plaintext form, and the manage_token is only ever readable at
    creation, in the API response, not persisted in plaintext at all).
    """
    if kind == "created_disabled":
        # Verify round-3 fix #13: the dispatcher combines an unsent
        # "created" notice with a same-window auto-disable into this ONE
        # sentinel value (notify_pending is a single-slot column -- see
        # workers/webhooks/dispatch_webhooks.py's `_notify_disabled`) rather
        # than silently overwriting the fact the subscription ever
        # verified. Render both facts, in order, honestly.
        subject = "[Bill Commons] Webhook verified, then auto-disabled"
        reason_text = disabled_reason or "repeated delivery failures"
        html = f"""<div style="font-family:system-ui,sans-serif;max-width:620px;margin:0 auto">
<h2 style="margin-bottom:4px">Bill Commons</h2>
<p>Your webhook subscription verified earlier today -- and has since been
automatically disabled.</p>
<ul>
<li><b>Subscription id:</b> {sub_id}</li>
<li><b>Scope:</b> {scope}</li>
<li><b>Disable reason:</b> {reason_text}</li>
</ul>
<p>To resume, POST to <code>/api/v1/webhooks/{sub_id}/reactivate?mode=resume</code>
(keeps the backlog since disable) or <code>?mode=skip</code> (drops the
backlog, starts from now), with <code>Authorization: Bearer &lt;manage_token&gt;</code>
using the manage_token from creation. See {SITE}/docs/api/webhooks for details.</p>
<hr style="border:none;border-top:1px solid #ddd;margin:24px 0">
<p style="color:#888;font-size:12px">Free and open source -- <a href="{SITE}">billcommons.org</a>.</p>
</div>"""
        text_body = (
            f"Bill Commons -- webhook verified earlier today and has since been "
            f"auto-disabled.\n\n"
            f"Subscription id: {sub_id}\nScope: {scope}\nDisable reason: {reason_text}\n\n"
            f"To resume: POST /api/v1/webhooks/{sub_id}/reactivate?mode=resume "
            f"(keep the backlog) or ?mode=skip (drop it), with "
            f"Authorization: Bearer <manage_token> from creation.\n"
        )
    elif kind == "created":
        subject = "[Bill Commons] Webhook subscription created"
        html = f"""<div style="font-family:system-ui,sans-serif;max-width:620px;margin:0 auto">
<h2 style="margin-bottom:4px">Bill Commons</h2>
<p>Your webhook subscription is set up and verified.</p>
<ul>
<li><b>Subscription id:</b> {sub_id}</li>
<li><b>Scope:</b> {scope}</li>
</ul>
<p>Your endpoint has already answered the one-time verification challenge, so
deliveries have begun. Check status any time with
<code>GET /api/v1/webhooks/{sub_id}</code> using the manage_token you were
given at creation (shown once, not repeated here).</p>
<hr style="border:none;border-top:1px solid #ddd;margin:24px 0">
<p style="color:#888;font-size:12px">Free and open source -- <a href="{SITE}">billcommons.org</a>.</p>
</div>"""
        text_body = (
            f"Bill Commons -- webhook subscription created and verified.\n\n"
            f"Subscription id: {sub_id}\nScope: {scope}\n\n"
            f"Your endpoint has already answered the one-time verification "
            f"challenge, so deliveries have begun. Check status with "
            f"GET /api/v1/webhooks/{sub_id} using the manage_token from "
            f"creation.\n"
        )
    else:  # "disabled"
        subject = "[Bill Commons] Webhook subscription auto-disabled"
        reason_text = disabled_reason or "repeated delivery failures"
        html = f"""<div style="font-family:system-ui,sans-serif;max-width:620px;margin:0 auto">
<h2 style="margin-bottom:4px">Bill Commons</h2>
<p>Your webhook subscription was automatically disabled.</p>
<ul>
<li><b>Subscription id:</b> {sub_id}</li>
<li><b>Scope:</b> {scope}</li>
<li><b>Reason:</b> {reason_text}</li>
</ul>
<p>To resume, POST to <code>/api/v1/webhooks/{sub_id}/reactivate?mode=resume</code>
(keeps the backlog since disable) or <code>?mode=skip</code> (drops the
backlog, starts from now), with <code>Authorization: Bearer &lt;manage_token&gt;</code>
using the manage_token from creation. See {SITE}/docs/api/webhooks for details.</p>
<hr style="border:none;border-top:1px solid #ddd;margin:24px 0">
<p style="color:#888;font-size:12px">Free and open source -- <a href="{SITE}">billcommons.org</a>.</p>
</div>"""
        text_body = (
            f"Bill Commons -- webhook subscription auto-disabled.\n\n"
            f"Subscription id: {sub_id}\nScope: {scope}\nReason: {reason_text}\n\n"
            f"To resume: POST /api/v1/webhooks/{sub_id}/reactivate?mode=resume "
            f"(keep the backlog) or ?mode=skip (drop it), with "
            f"Authorization: Bearer <manage_token> from creation.\n"
        )
    return subject, html, text_body


def drain_webhook_notifications(conn, get_api_key, *, dry_run: bool = False) -> tuple[int, int]:
    """Probe-guarded no-op until migration 0012's table exists -- same
    information_schema pattern `has_scope` below uses for migration 0011.
    Sends the two lifecycle messages `notify_pending` marks and clears it on
    success; a crash before the clear means a re-send next night, same
    duplicate-over-silent-loss trade the topic digest loop above makes.
    """
    with conn.cursor() as cur:
        # Verify round-9 fix #3: schema-qualified, same reasoning as
        # dispatch_webhooks.py's `_creation_events_table_exists` (round-7
        # fix #6) -- an unqualified probe can match a same-named table in
        # ANOTHER schema, wrongly treating migration 0012 as applied.
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'webhook_subscriptions' "
            "AND table_schema = current_schema()"
        )
        if cur.fetchone() is None:
            return 0, 0
        cur.execute(
            "SELECT id, email, kind, target, notify_pending, disabled_reason "
            "FROM webhook_subscriptions WHERE notify_pending IS NOT NULL ORDER BY id"
        )
        rows = cur.fetchall()

    sent = failed = 0
    api_key: str | None = None
    for sub_id, email, kind, target, notify_pending, disabled_reason in rows:
        scope = f"{kind}:{target}"
        subject, html, text_body = render_webhook_notice(
            notify_pending, str(sub_id), scope, disabled_reason
        )
        if dry_run:
            print(f"DRY-WEBHOOK {email} {sub_id}: {subject}", flush=True)
            continue
        if api_key is None:
            api_key = get_api_key()
        try:
            msg_id = resend_send_plain(api_key, email, subject, html, text_body)
        except Exception as exc:  # noqa: BLE001 -- one bad address must not kill the run
            print(f"WEBHOOK-NOTIFY FAIL {email} {sub_id}: {exc}", flush=True)
            failed += 1
            continue
        # Round-2 fix #9: clear with a compare-and-swap on the EXACT value
        # this loop read (`notify_pending = %s` in the WHERE, not just
        # `id = %s`) -- the dispatcher can flip `notify_pending` between
        # this script's SELECT above and this UPDATE (e.g. a subscription
        # that was 'created' when read gets auto-disabled a moment later,
        # setting notify_pending='disabled'); a plain `WHERE id = %s` would
        # clobber that newer, not-yet-sent 'disabled' notice and lose it
        # silently. A CAS miss (0 rows updated) means exactly that raced --
        # leave the flag alone; the next nightly run picks up whatever it
        # is now.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE webhook_subscriptions SET notify_pending = NULL "
                "WHERE id = %s AND notify_pending = %s",
                (sub_id, notify_pending),
            )
        conn.commit()
        sent += 1
        print(f"WEBHOOK-NOTIFY SENT {email} {sub_id} {notify_pending} (resend {msg_id})", flush=True)
    return sent, failed


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
        "youth-online-safety": (
            "(lower(b.title) LIKE '%%age verification%%'"
            " OR lower(b.title) LIKE '%%age-verification%%'"
            " OR lower(b.title) LIKE '%%app store accountability%%'"
            " OR lower(b.title) LIKE '%%social media%%minor%%'"
            " OR lower(b.title) LIKE '%%minor%%social media%%'"
            " OR lower(b.title) LIKE '%%social media%%child%%'"
            " OR lower(b.title) LIKE '%%child%%social media%%'"
            " OR lower(b.title) LIKE '%%child%%online safety%%'"
            " OR lower(b.title) LIKE '%%age-appropriate design%%'"
            " OR EXISTS (SELECT 1 FROM bill_subjects s WHERE s.bill_id = b.id AND"
            " lower(s.subject) LIKE '%%age verification%%'))"
        ),
        "platform-accountability": (
            "(lower(b.title) LIKE '%%content moderation%%'"
            " OR lower(b.title) LIKE '%%social media platform%%'"
            " OR lower(b.title) LIKE '%%online platform%%'"
            " OR lower(b.title) LIKE '%%social media compan%%'"
            " OR lower(b.title) LIKE '%%social networking%%'"
            " OR EXISTS (SELECT 1 FROM bill_subjects s WHERE s.bill_id = b.id AND"
            " lower(s.subject) LIKE '%%social media%%'))"
        ),
        "cybersecurity": (
            "(lower(b.title) LIKE '%%cybersecurity%%'"
            " OR lower(b.title) LIKE '%%cyber security%%'"
            " OR lower(b.title) LIKE '%%data breach%%'"
            " OR lower(b.title) LIKE '%%ransomware%%'"
            " OR lower(b.title) LIKE '%%encryption%%'"
            " OR EXISTS (SELECT 1 FROM bill_subjects s WHERE s.bill_id = b.id AND"
            " lower(s.subject) LIKE '%%cybersecurity%%'))"
        ),
        "local-government": (
            "(lower(b.title) LIKE '%%local government%%'"
            " OR lower(b.title) LIKE '%%home rule%%'"
            " OR lower(b.title) LIKE '%%preemption%%'"
            " OR lower(b.title) LIKE '%%municipal%%'"
            " OR EXISTS (SELECT 1 FROM bill_subjects s WHERE s.bill_id = b.id AND"
            " (lower(s.subject) LIKE '%%local government%%'"
            " OR lower(s.subject) LIKE '%%municipalit%%'"
            " OR lower(s.subject) LIKE '%%municipal government%%')))"
        ),
    }


def render_digest(
    topic: str, events: list[dict], total: int, unsub: str, scope: str | None = None
) -> tuple[str, str, str]:
    """Returns (subject, html, text).

    `scope` is a jurisdiction abbreviation or None for national. It goes in the
    subject line because a scoped digest and a national one are otherwise
    indistinguishable in an inbox, and a reader who sees only Florida bills in
    a national digest would reasonably conclude nothing else moved.
    """
    n = total
    where = f" ({scope})" if scope else ""
    subject = (
        f"[Bill Commons] {n} update{'s' if n != 1 else ''} on "
        f"{topic.replace('-', ' ')} bills{where}"
    )
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

            # This worker runs straight from the working tree (see the
            # systemd unit's ExecStart), so a code edit is live on the next
            # nightly run with no deploy step -- while the schema it depends on
            # only changes when a migration is applied. Selecting a column that
            # migration 0011 has not yet added would take down a working daily
            # send. Probe for it and fall back to national-only rather than
            # ordering the two by hand and hoping.
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'alert_subscriptions' AND column_name = 'jurisdiction'"
            )
            has_scope = cur.fetchone() is not None
            scope_col = "jurisdiction" if has_scope else "NULL::text"
            if not has_scope:
                print(
                    "WARN jurisdiction column absent (migration 0011 not applied); "
                    "treating every subscription as national",
                    flush=True,
                )
            cur.execute(
                f"SELECT id, email, target, unsubscribe_token, last_seq, {scope_col} "
                "FROM alert_subscriptions WHERE active AND kind = 'topic' "
                "ORDER BY email"
            )
            subs = cur.fetchall()

        for sub_id, email, target, unsub_token, last_seq, scope in subs:
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

            # A NULL scope is national (the original behaviour). A scoped
            # subscription still advances its cursor to the same watermark on a
            # quiet run, so narrowing never causes a backlog to accumulate.
            scope_clause = "" if scope is None else "AND j.abbreviation = %s"
            scope_params: tuple = () if scope is None else (scope,)
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT e.seq, e.kind, e.detail, b.id, b.identifier, b.title,
                           j.abbreviation
                    FROM bill_events e
                    JOIN bills b ON b.id = e.bill_id
                    JOIN jurisdictions j ON j.id = b.jurisdiction_id
                    WHERE e.seq > %s AND e.seq <= %s AND {predicate} {scope_clause}
                    ORDER BY e.seq
                    """,
                    (last_seq, watermark, *scope_params),
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
            subject, html, text_body = render_digest(
                target, events, len(rows), unsub_url, scope
            )

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

        webhook_sent, webhook_failed = drain_webhook_notifications(
            conn, get_resend_key, dry_run=args.dry_run
        )

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(
        f"{stamp} done: {sent} sent, {skipped} quiet, {fast_forwarded} new subscriber(s) fast-forwarded, "
        f"{webhook_sent} webhook notice(s) sent, {webhook_failed} webhook notice failure(s)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
