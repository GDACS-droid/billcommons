# Webhooks

Push delivery over the same `bill_events` log `GET /changes` serves. If
you'd rather poll, use `/changes` -- this exists for consumers who want to
be told, not to ask.

## Architecture, briefly

`POST /api/v1/webhooks` is DB-only: it creates your subscription with
`verified: false` and returns `201` immediately. The API never makes an
outbound HTTP request to your endpoint. A separate worker
(`workers/webhooks/dispatch_webhooks.py`, on its own schedule, ticking every
~2 minutes) does two things: it sends your endpoint a one-time verification
challenge, and once that's answered correctly, it drains new events to you.

**Honest V1 limits:**

- Latency floor: roughly a 2-minute dispatcher tick plus a 240-second
  "commit safety lag" the underlying log requires (the same lag `/changes`
  itself withholds recent events behind -- see that endpoint's docs, and
  `billcommons_shared/watermark.py` for the 2026-08-04 pre-ship measurement
  the current value is based on), so budget up to ~6 minutes from a
  legislative action landing in the corpus to your endpoint being POSTed.
  This is a floor, not an SLA.
- Up to 100 events per delivery POST; a bigger backlog arrives as several
  POSTs in sequence (`has_more: true` on all but the last).
- Best-effort ordering by `seq` (the underlying log's total order) --
  events for the same bill in the same delivery are in order; ordering
  across concurrently-processed subscriptions is not guaranteed and
  shouldn't be relied on.
- At-least-once delivery. A delivery can be retried after your endpoint
  already processed it (a network blip after your 200, a dispatcher restart
  mid-tick). Dedupe on each event's `id` (see below).

## 1. Subscribe

```
POST /api/v1/webhooks
{
  "url": "https://your-service.example.com/billcommons-hook",
  "email": "you@example.com",
  "kind": "topic",
  "target": "artificial-intelligence",
  "jurisdiction": "FL",        // optional, kind="topic" only -- scopes a
                                // topic to one state, same idea as
                                // /api/v1/alerts/subscribe's jurisdiction
  "event_kinds": "status,text" // optional CSV subset of the seven kinds;
                                // omit for all
}
```

`url` requirements: `https://` only, default port (443) only, no userinfo
(`https://user:pass@host/...` is rejected), no fragment. `kind` is one of:

- `topic` -- `target` is a topic slug from `GET /topics` (e.g.
  `artificial-intelligence`, `youth-online-safety`). `jurisdiction` narrows
  it to one state.
- `jurisdiction` -- `target` is a two-letter state abbreviation. Every
  change in that jurisdiction's bills, unfiltered by topic.
- `bills` -- `target` is a comma-separated list of up to 64 bill UUIDs.

Response:

```
201
{
  "id": "...",
  "manage_token": "...",     // shown ONCE -- store it now
  "signing_secret": "...",   // shown ONCE -- store it now
  "verified": false,
  "note": "..."
}
```

`manage_token` is the bearer credential for `GET`/`DELETE`/`reactivate` on
this subscription. `signing_secret` is the HMAC key every delivery (and the
verification challenge) is signed with. They are two different secrets on
purpose: a leak of one does not compromise the other.

Per-IP and per-domain quotas apply (5 new subscriptions per IP per day, 10
**verified** subscriptions per registrable domain, 500 **verified**
subscriptions globally for now, plus a separate cap of 10 active-but-
unverified subscriptions **per registrable domain, per creator IP**) -- a
`429`/`403` with an honest `error.message` explains which one you hit. The
per-IP daily quota counts creation *attempts*, not currently-existing
subscriptions -- deleting a subscription does not free up a slot for that
day.

The unverified cap is per-domain, not global, on purpose: a global cap on
never-verifying subscriptions is itself a shared kill switch (a handful of
IPs posting to domains they don't control can fill it and refuse every
OTHER caller's legitimate creation with no remedy of their own). Bounding
it per-domain instead means no single domain -- attacker-controlled or
not -- can hold more than 10 unverified rows at once, while unrelated
domains' creations are unaffected. It is additionally scoped **per creator
IP** within a domain: without that, an attacker could still fill a real
domain owner's entire unverified pool for that domain from its own IP,
403ing the owner's own legitimate attempt to subscribe a webhook for their
own domain. Scoped to (domain, IP), an attacker can only ever exhaust its
own budget against a domain -- a different caller's IP gets its own
independent 10-subscription budget against that same domain. The
unverified pool as a whole stays bounded some other way: challenge work is
tick-bounded, volume per IP is creation-event-bounded (the 5/day cap
above), and lifetime is GC-bounded (24h never-verified, 7d quota-disabled-
while-unverified).

The per-domain **and** global quotas are each enforced twice: once (loosely)
at creation, and again -- authoritatively -- the moment a subscription's
verification challenge succeeds (an unverifiable subscription never counts
against either cap at creation time, so a batch of subscriptions that were
all individually under quota when created can still collectively exceed it
once they all go on to verify). If your domain is already at 10 verified
subscriptions, or Bill Commons is at its global cap, by the time yours
answers the challenge, it is NOT silently garbage collected: it is marked
`disabled_reason: "domain_quota_exceeded"` or `disabled_reason:
"global_quota_exceeded"` respectively, so `GET /api/v1/webhooks/{id}` tells
you exactly why nothing is being delivered, rather than leaving you to
guess. A quota-disabled subscription can still be reactivated (see §5)
once capacity frees up, but it is not kept forever: an unverified
subscription that is also disabled this way is retained for 7 days from
the disable, then permanently deleted, same as any other never-verified
subscription's retry window (see §2) just longer, to give you a real
chance to come back once your domain or the global pool has room.

The per-domain quota buckets by **registrable domain** (eTLD+1), not full
hostname (`hooks.example.com` and `api.example.com` share one bucket),
computed via the real public suffix list (`publicsuffix2`, bundled offline
data, no network fetch at runtime) -- so unrelated sites under a multi-part
suffix like `co.uk`, or a hosted/PaaS domain like `github.io`, don't
collapse into one shared bucket the way a fixed-depth rule would. A raw
IP-literal `url` (IPv4 or IPv6) buckets on the **full literal address**,
never run through suffix-list logic at all -- two unrelated IPs that happen
to share a suffix are not the same subscriber.

## 2. Verification challenge

Within the next dispatcher tick or two, your endpoint receives:

```
POST <your url>
{"challenge": "<token>", "subscription_id": "<id>"}
```

signed exactly like a normal delivery (see below). **Respond with a 2xx
whose body is EXACTLY the challenge token** (leading/trailing whitespace is
stripped before comparing; anything else -- including a body that merely
*contains* the token, e.g. a logging/echo endpoint that wraps it in JSON --
is rejected). Once accepted, `verified` flips to `true` and normal delivery
begins -- unless your registrable domain is already at its 10-verified-
subscription quota, or Bill Commons is at its global verified-subscription
cap, by that moment, in which case the subscription is marked disabled with
`disabled_reason: "domain_quota_exceeded"` or `disabled_reason:
"global_quota_exceeded"` respectively instead (see the quota note above).
Unverified subscriptions are retried for up to 24 hours, then deleted. Your
"webhook created" confirmation email is sent at this point -- AFTER
verification succeeds, not at the time you subscribed -- so it always
describes a subscription that is already verified and already delivering,
never one that's still waiting on the challenge.

Even during a tick where deliveries consume the entire 90-second budget,
challenges still get a guaranteed minimum slice (at least 15 seconds,
possibly overrunning the tick's own 120-second cadence slightly to give it)
-- otherwise a broad receiver outage that keeps every delivery attempt busy
for a full tick would starve verification challenges indefinitely, and a
brand-new subscription could get GC'd at the 24-hour unverified mark having
never been challenged even once.

## 3. Delivery payload + signature

```
POST <your url>
Content-Type: application/json
X-BillCommons-Signature: sha256=<hex hmac>
X-BillCommons-Timestamp: <unix seconds>
X-BillCommons-Delivery: <uuid, per ATTEMPT -- never a dedupe key>
X-BillCommons-Delivery-Attempt: <ordinal within the CURRENT failing streak>
User-Agent: BillCommons-Webhooks/1.0

{
  "api_version": "1",
  "events": [
    {
      "id": "<stable dedupe key -- equality only, do not parse it>",
      "cursor": "<opaque, same encoding as /changes>",
      "kind": "status",
      "detail": "in_committee -> passed",
      "changed_at": "2026-08-04T12:00:00+00:00",
      "bill": { "...": "the exact ChangeEvent.bill shape /changes serves" }
    }
  ],
  "cursor": "<opaque cursor of the LAST event in this payload>",
  "has_more": false,
  "lag_seconds": 246.8,
  "delivery_id": "<uuid, this ATTEMPT>"
}
```

(`lag_seconds` can never be below the 240-second commit-safety-lag floor
described above for a non-empty payload -- the example above reflects that.)

`bill` is the bill's **current** state, not a snapshot from the moment of
the event -- same as `/changes`. Several events for one bill in one delivery
(or across deliveries) can show the same current row.

### Verifying the signature (Python, receiver side)

```python
import hashlib
import hmac
import time

def verify(signing_secret: str, timestamp_header: str, raw_body: bytes, signature_header: str) -> bool:
    # Reject stale/replayed deliveries -- a 5-minute skew window.
    if abs(time.time() - int(timestamp_header)) > 300:
        return False
    expected = "sha256=" + hmac.new(
        signing_secret.encode(), f"{timestamp_header}.".encode() + raw_body, hashlib.sha256
    ).hexdigest()
    # Constant-time compare -- never `==` on a secret-derived value.
    return hmac.compare_digest(expected, signature_header)
```

Dedupe on each event's `id`, not on `X-BillCommons-Delivery` (that header
identifies the delivery ATTEMPT, not the events -- a retried attempt gets a
new one, but its events carry the same `id`s as the first attempt).

`X-BillCommons-Delivery-Attempt` is **1 on a fresh (or freshly-succeeded)
subscription's first try, and increments with each consecutive failure of
this window** -- it is `consecutive_failures + 1` at send time, not a
lifetime attempt count. A success resets it back to 1 for the next window.
Useful for a receiver that wants to log/alert on "this is the Nth retry in a
row", not for reconstructing total attempts ever made.

## 4. Retry / disable policy (wall-clock terms)

- `410 Gone` -- disabled immediately, reason `gone`.
- Other non-retryable 4xx (400, 401, 403, 404, 405, 406, 411, 413, 414, 415,
  422) -- disabled after 3 **consecutive occurrences of the SAME failure
  class**. An interleaved failure of a DIFFERENT class (a timeout, a 5xx,
  a `429`, ...) **resets this streak to zero** -- it takes 3 *consecutive*
  same-class hard-4xx responses in a row to disable, not merely 3 within
  some window. The 72h wall-clock auto-disable below is the backstop for
  an endpoint that never produces 3 in a row but also never really
  recovers.
- `429` -- its own failure class (`http_429`, never counted as, or
  resetting, the hard-4xx streak above -- see the error-class table below).
  Backs off for `Retry-After` (capped at 6 hours). A `Retry-After` of `0` or
  a negative value is treated as **absent** (falls back to the ordinary
  exponential backoff below) rather than "retry immediately" -- a broken
  `Retry-After` must never turn into a tight retry loop hammered every
  dispatcher tick for the whole 72-hour auto-disable window. Also subject to
  the 72h wall-clock auto-disable below, same as every other failure class
  -- a permanently-429ing endpoint does not retry forever.
- `5xx`, timeout, TLS, connection, DNS, or transport failure -- exponential
  backoff (`min(2^n minutes, 6 hours)`, +/-20% jitter).
- Auto-disabled once failing continuously for **about 3 days**
  (wall-clock, not a failure count -- a subscription failing once a day for
  a month is not the same as one failing continuously for 3 days, and only
  the latter auto-disables).
- A success clears the failure streak entirely.
- **Oversized single event -- skip, never brick.** A batch that is still
  over the 512KB cap after being split down to ONE event is not retried
  forever: that event is skipped, `last_error` is set to
  `payload_too_large`, and the cursor advances past it (recorded as a
  `webhook_deliveries` row with `error: "payload_too_large"` and no
  `status` -- no HTTP attempt was ever made). Oversized events are simply
  never pushed; `GET /api/v1/changes?cursor=...` (see "Recovering from a
  gap" below) is the completeness escape hatch if you need that event's
  data.
- **Unknown/stale scope -- disabled, visibly.** If your subscription's
  scope stops resolving (a `topic` slug that was retired, a `bills` list
  that no longer parses), the subscription is disabled with
  `disabled_reason: "unknown_scope"` rather than silently delivering
  nothing forever with `last_error: null`. `GET /api/v1/webhooks/{id}`
  tells you why; fix the scope and `reactivate` once you have (a new
  subscription with the corrected `target` also works).

### Error classes (`last_error` / a delivery's `error`)

`GET /api/v1/webhooks/{id}` and each entry in `recent_deliveries` expose an
error CLASS, never a free-text message (which could quote attacker- or
receiver-controlled content). One of:

| Class | Meaning |
|---|---|
| `http_4xx` | A non-retryable 4xx response (400/401/403/404/405/406/411/413/414/415/422). Counts toward the 3-strike streak above. (`410 Gone` also reports this class on `last_error`/a delivery's `error`, but disables immediately on the FIRST occurrence -- see §4 -- so it never actually joins or advances this streak.) |
| `http_4xx_retryable` | A 4xx response NOT in that non-retryable list (e.g. `408 Request Timeout`, `425 Too Early`) -- backs off exactly like a 5xx and is subject to the 72h wall-clock auto-disable, but never counts toward, or interrupts, the hard-4xx 3-strike streak above. Its own class exists for the same reason `http_429` has one: without it, an interleaved run like 408, 408, 404 would share `http_4xx` across all three responses and disable after the third response even though only ONE of them was a genuine hard-4xx. |
| `http_429` | A `429 Too Many Requests` response -- its own class, distinct from `http_4xx`, so a rate limit never pads (or resets) the hard-4xx streak. |
| `http_5xx` | A 5xx response. |
| `timeout` | Connect, TLS handshake, request, or body-read timeout, or the overall 15s wall-clock budget was exceeded. |
| `tls` | A TLS handshake failure at the `ssl` layer (bad cert, protocol mismatch, ...). |
| `connection` | The connection was refused, reset, or otherwise dropped by the peer (ECONNREFUSED, `ConnectionResetError`, `BrokenPipeError`) at any post-connect phase (TLS wrap, send, or a response/body read) -- distinct from `timeout`, which means "no answer within the budget," not "the peer actively dropped the connection." |
| `dns` | Resolution failed, returned no usable answer, or exceeded its share of the wall-clock budget. |
| `ssrf_rejected` | The url or a resolved address failed admission (non-`https`, non-default port, userinfo/fragment present, a redirect, or an address that isn't publicly routable). |
| `too_large` | The response body (on a verification challenge, which reads it) exceeded 64KB or arrived non-identity-encoded. |
| `payload_too_large` | This ONE outbound event's own serialized body still exceeded the 512KB delivery cap even at batch size 1 -- it was skipped (never POSTed), the cursor advanced past it, and `GET /api/v1/webhooks/{id}` records it as a `webhook_deliveries` row with no `status` (no HTTP attempt was ever made). See "Oversized single event" above. |
| `challenge_mismatch` | The verification challenge got a 2xx response, but its body did not equal the challenge token exactly -- distinct from `http_4xx`, since the endpoint DID accept the request; it just echoed the wrong body. |
| `transport` | A belt-and-suspenders catch-all inside the transport layer for anything unanticipated -- including a malformed HTTP response (a bad status line, invalid chunked encoding) your endpoint sent, which is a protocol violation, not a timeout. Should otherwise never appear in practice; if you see it for another reason, something upstream of the documented cases above broke in a new way. |
| `internal` | A bug in the dispatcher itself (not your endpoint) during this attempt. Backs off and counts toward the 72h auto-disable exactly like any other failure. |

## 5. Status, delete, reactivate

All three require `Authorization: Bearer <manage_token>`.

- `GET /api/v1/webhooks/{id}` -- verification/delivery health: `verified`,
  `active`, `last_success_at`, `last_status`, `consecutive_failures`,
  `failing_since`, an approximate cursor lag, and the last 10 delivery
  attempts (status/error class/duration). Never returns either secret.
- `DELETE /api/v1/webhooks/{id}` -- unsubscribe, permanently.
- `POST /api/v1/webhooks/{id}/reactivate?mode=resume|skip` -- only for an
  auto-disabled subscription. `mode` is required, no default:
  - `resume` -- keeps the cursor where it was; the backlog since disable
    drains normally.
  - `skip` -- fast-forwards the cursor to now, dropping the backlog. This
    is **at-least-once**, not exactly-once: if the dispatcher had already
    loaded a batch for this subscription (read, but not yet POSTed) at the
    moment the reactivate lands, that one batch can still be delivered
    after the skip takes effect. If your receiver isn't idempotent on
    `delivery_id`/event `id` already (§7 already asks for this for the
    normal retry case), a `mode=skip` reactivate is the other place it
    matters.
  - If the subscription was **verified** at the time it was auto-disabled,
    reactivating it re-checks the same per-domain (10) and global (500)
    verified-subscription quotas creation and verification-promotion
    already enforce, under the same advisory lock creation uses -- a `409
    webhook_quota_exceeded` means your domain or Bill Commons overall is
    currently full; the subscription stays disabled, and you can retry
    later.
  - If the subscription was disabled while still **unverified** (a
    domain/global quota disable at the moment its own verification
    challenge would otherwise have succeeded -- see the section above),
    reactivating it issues a fresh challenge token, resets its challenge
    attempt count, and restarts its 24-hour unverified-GC clock, so it gets
    a genuine new shot at the verification challenge instead of being
    silently deleted a short time later. It also re-checks the same
    10-subscription **per-domain, per-creator-IP** cap on unverified
    subscriptions creation enforces -- a `409 webhook_quota_exceeded` here
    means that domain's unverified pool for the account that originally
    created this subscription is currently full; the subscription stays
    disabled and you can retry later.

## 6. Recovering from a gap

Every delivery's `cursor` is accepted **verbatim** by
`GET /api/v1/changes?cursor=...` -- if you suspect you missed something (an
extended outage on your end, a subscription that got auto-disabled and
reactivated with `mode=skip`), pull-backfill from your last known-good
`cursor` through `/changes` rather than waiting on redelivery.
