# Monetization runbook — API keys, quota, billing, manual snapshot fulfillment

Phase 1 (keys + metering, no Stripe) and Phase 2 (billing --
`billcommons_api.routers.billing`, live as of 2026-08-21) are both in
scope now. Phase 3 (automated snapshot builder) will extend this file when
it lands; the "§manual-snapshot" section below describes the manual
process that stands in until then.

## Applying migration 0019

`packages/schema/alembic/versions/0019_api_keys_and_billing.py` adds the
`api_customers` / `api_keys` / `api_subscriptions` / `api_customer_usage` /
`api_key_usage` / `api_key_usage_subnets` / `stripe_events` /
`account_login_tokens` / `snapshot_artifacts` / `snapshot_entitlements` /
`snapshot_downloads` tables, plus a nullable `webhook_subscriptions.customer_id`
column. **This migration is applied by the operator, on the operator's own
schedule** -- it is not run automatically by CI or by this branch's merge.

```bash
cd packages/schema
DATABASE_URL=<railway-url> ../../.venv/bin/alembic upgrade head
```

Verify:

```sql
select count(*) from api_customers;   -- 0, table exists
select count(*) from api_keys;        -- 0, table exists
```

Two manual post-migration steps:

1. Mint a `bc_live_` key for the founder's own monitoring (see "Mint a key
   by hand" below).
2. Confirm `is_trusted_client` (the internal-renderer bypass) still
   short-circuits before any of this new code runs -- unaffected by this
   migration, but worth a spot check after any deploy that touches
   `quota.py`.

## Applying migration 0020 (2026-08-21 fix pass)

`packages/schema/alembic/versions/0020_billing_terminal_status_and_snapshot_intent_index.py`
re-creates `uq_api_subscriptions_one_active_per_customer` so
`incomplete_expired` joins `canceled` as a terminal subscription status
(fixlist item 3) -- a failed-first-payment Checkout that expired ~23h
later was, pre-fix, neither `canceled` NOR re-checkoutable: the customer's
own retries 409'd forever, and a guest's retry captured money that got
silently canceled with no refund. Pure index change, same operator
process as 0019:

```bash
cd packages/schema
DATABASE_URL=<railway-url> ../../.venv/bin/alembic upgrade head
```

`snapshot_entitlements.stripe_payment_intent_id` already had a partial
unique index from migration 0019 (fixlist item 22 turned out to already be
satisfied) -- no new index for that one.

## Running the monetization test suite against Postgres (`BILLCOMMONS_TEST_DATABASE_URL`)

`apps/api/tests/_monetization_sqlite.py` backs `test_api_keys.py`,
`test_quota.py`, `test_billing.py`, and `test_rate_limit.py`. By default it
builds its own app on an in-memory SQLite engine. Set
`BILLCOMMONS_TEST_DATABASE_URL` to run the SAME suite against a real
Postgres instance instead (the `FOR UPDATE` locks, `ON CONFLICT` upserts,
and UUID binding SQLite can't emulate get their first real exercise there)
-- **always a disposable/throwaway instance, never the live Railway
Postgres DB**. Migrations must already be applied (`alembic upgrade head`
from `packages/schema`).

Before every test the harness clears the 9 monetization tables with a
plain `DELETE FROM` (children before parents, no `CASCADE`). Two gates
are required or it refuses to run (raises `RuntimeError` at import time,
2026-08-21 fix-pass item 1 -- this used to be an unconditional
`TRUNCATE ... CASCADE`, which on Postgres empties every table with an FK
to a named table, including `webhook_subscriptions` since migration
0019 -- a live table this suite has nothing to do with):

| Var | Required value |
|---|---|
| `BILLCOMMONS_TEST_DATABASE_URL` | A Postgres URL whose host is `127.0.0.1`/`localhost`, OR whose text contains `_staging`/`_test` (e.g. `bc_staging`). A Railway/production hostname (`*.railway.app`, `*.rlwy.net`, anything else) is refused. |
| `BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE` | Must be exactly `1`. Not inferred from the URL -- this is the operator affirmatively opting into row deletion. |

```bash
BILLCOMMONS_TEST_DATABASE_URL=postgresql://bc@127.0.0.1:54329/bc_staging \
BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE=1 \
  .venv/bin/pytest apps/api/tests/test_billing.py apps/api/tests/test_api_keys.py \
    apps/api/tests/test_quota.py apps/api/tests/test_rate_limit.py -q
```

## Required environment variables (Phase 1 + 2)

| Var | Purpose |
|---|---|
| `BILLCOMMONS_REVEAL_KEY` | Fernet key (`cryptography.fernet.Fernet.generate_key()`), used to encrypt a not-yet-revealed key's plaintext at rest (Phase 2's checkout-minted keys; the Phase 1 login-minted key is revealed inline and never needs this). |
| `ACCOUNT_SESSION_SECRET` | HMAC secret signing the `bc_session` cookie. |
| `BILLCOMMONS_ADMIN_TOKEN` | Bearer token for `GET /api/v1/admin/usage`. 404s (not 401s) on a mismatch. |
| `BILLCOMMONS_ANON_DAILY_LIMIT` / `BILLCOMMONS_ANON_DAILY_LIMIT_SUBNET` | Anonymous daily caps (default 2,000/IP, 5,000/24). |
| `BILLCOMMONS_ALLOWED_ORIGINS` | Comma-separated origin allowlist for the cookie-authenticated `/account`/`/billing` CORS (default `https://billcommons.org,https://www.billcommons.org`). |
| `BILLCOMMONS_PUBLIC_SITE_URL` | Base URL used to build Checkout `success_url`/`cancel_url`, the Portal `return_url`, and magic-link URLs (default `https://billcommons.org`). |
| `RESEND_API_KEY` | Magic-link email delivery. **If unset, the link is logged at WARN instead of emailed** -- fine for local/dev, never acceptable in production (the account is then unreachable by anyone who can't read the API's logs). |
| `OPERATOR_ALERT_EMAIL` | Founder notification address: snapshot orders, dunning/duplicate-subscription events, permanent webhook errors. |
| `STRIPE_SECRET_KEY` | Restricted Stripe API key (`rk_live_...`/`rk_test_...` -- never the account `sk_live`). See "Stripe setup" below. |
| `STRIPE_WEBHOOK_SECRET` | Signing secret for the `/api/v1/billing/webhook` endpoint. |
| `STRIPE_PRICE_BUILDER_MONTHLY` / `STRIPE_PRICE_BUILDER_ANNUAL` / `STRIPE_PRICE_SCALE_MONTHLY` / `STRIPE_PRICE_SCALE_ANNUAL` | Stripe Price ids for the two subscription tiers. |
| `STRIPE_PRICE_SNAPSHOT_STATE` / `STRIPE_PRICE_SNAPSHOT_FULL` | Stripe Price ids for the two one-time snapshot purchases. |
| `BILLCOMMONS_API_RATE_LIMIT_CHECKOUT` / `BILLCOMMONS_API_RATE_LIMIT_CHECKOUT_SUBNET` | 2026-08-21 fix pass (item 4/E2): `/billing/checkout` and `/billing/checkout/snapshot` are unauthenticated writes against the shared Stripe account -- `QuotaMiddleware` exempts this whole router, so these two endpoints enforce their own strict per-IP (default 5/minute) and per-/24 (default 20/minute) limits. |

All of the above are now validated at process startup (`app.py`'s
`_validate_monetization_env`, extended in the 2026-08-21 fix pass, item 23)
-- a missing var logs at ERROR when the process boots, instead of surfacing
as a 500 (or, inside the Stripe webhook specifically, a misclassified
`HTTPException` that used to bypass the permanent-error handling and leave
Stripe retrying forever) the first time the code path is hit.

## Runway numReplicas=1 assertion

`billcommons_api.api_keys`'s 60-second in-process key-resolution cache and
`quota.py`'s in-process burst/anonymous-daily counters are correct ONLY
under Railway's `numReplicas=1` for the API service. With a second replica:

* Revoke/rotate/suspend take effect immediately on whichever replica
  handled the mutation, but up to 60s late on the other (accepted, spec
  risk #10 -- `resolve_key`'s cache TTL).
  * A `rotating` key's 24h `revoke_at` is still checked on every cache
    HIT, so it dies on schedule regardless of replica or cache staleness.
* Burst and anonymous-daily counters would be N-times the advertised
  ceiling (each replica has its own counter) -- this is the same
  known limitation `RateLimitMiddleware` (bleed-stop) already had.

**Before ever raising `numReplicas` above 1**, move burst/anonymous
counters to Postgres or Redis; until then, if Railway ever reports the API
service running with `RAILWAY_REPLICA_ID` indicating more than one replica,
treat quota/rate-limit numbers as advisory only, not enforced.

## Mint a key by hand (operator monitoring / support)

```sql
-- 1. Create (or find) the customer row
insert into api_customers (email) values ('alberto@gdacs.net')
  on conflict (email) do nothing;
```

```python
# 2. Mint the key (run from apps/api with DATABASE_URL set)
from billcommons_shared.db import get_session
from billcommons_api.api_keys import mint_key
import uuid

db = get_session()
customer_id = uuid.UUID("...")  # from the insert above
row, full_key = mint_key(db, customer_id, environment="live", plan="scale")
db.commit()
print(full_key)  # shown exactly once -- record it now
```

## SQL snippets (admin usage)

```sql
-- Today's usage by customer (mirrors GET /api/v1/admin/usage)
select k.key_prefix, k.plan, k.status, c.email,
       coalesce(u.requests, 0) as requests,
       coalesce(u.heavy_requests, 0) as heavy_requests
from api_keys k
join api_customers c on c.id = k.customer_id
left join api_customer_usage u
  on u.customer_id = k.customer_id and u.usage_date = current_date
order by u.requests desc nulls last;

-- Key-sharing flag: >20 distinct /24s for one key in a day
select key_id, usage_date, count(distinct subnet) as distinct_subnets
from api_key_usage_subnets
where usage_date = current_date
group by key_id, usage_date
having count(distinct subnet) > 20;

-- GC: usage rows older than 400 days
delete from api_key_usage where usage_date < current_date - interval '400 days';
delete from api_key_usage_subnets where usage_date < current_date - interval '400 days';
delete from api_customer_usage where usage_date < current_date - interval '400 days';

-- GC: expired/used magic-link + reveal tokens (fixlist item 10). Every
-- accepted `POST /account/magic-link` writes ONE permanent
-- `account_login_tokens` row (A6 convention: GC alongside the usage
-- tables above); nothing else ever deletes them. This route is exempt
-- from `QuotaMiddleware` (A5) and bounded only by 20/hour/IP + 5/hour/
-- email, so any distributed source can grow this table (and its two
-- indexes) monotonically without this. 7 days covers the 15-minute TTL
-- with room to keep a recently-expired token around for support lookups.
delete from account_login_tokens where expires_at < now() - interval '7 days';
```

## Stripe setup (Phase 2)

**One shared Stripe account** (the owner runs other sub-businesses, e.g.
FLHQ, as Products on the same account) -- Bill Commons uses a **restricted
API key**, never the account's `sk_live`, and tags `metadata.app =
"billcommons"` on every object it creates so the webhook handler can tell
its own events apart from an unrelated sub-business's (`routers/billing.py`'s
module docstring covers the filter logic in detail).

**API version pin (2026-08-21 fix pass, item 7/E3):** `billing.py` sets
`stripe.api_version = "2025-03-31.basil"` at import time. This app's
`stripe==13.2.0` SDK's installed models have NO top-level
`Invoice.subscription` or `Subscription.current_period_end` field at all
(both basil-era removals) -- handlers read the basil+ shapes
(`invoice.parent.subscription_details.subscription`,
`subscription.items.data[0].current_period_end`) with a pre-basil fallback
for replayed old events.

**Item 13 correction (2026-08-21 round-2 fix pass):** `stripe.api_version`
only pins this app's OWN outbound calls -- it has NO effect on the shape of
INCOMING webhook payloads. An event is rendered at whatever API version is
configured on the Stripe **Dashboard webhook endpoint** itself (Developers
-> Webhooks -> your endpoint -> "API version"), or the account default if
that is left unset. **Set the webhook endpoint's API version explicitly to
`2025-03-31.basil` in the Dashboard** (do this once, when the endpoint is
created, and again any time the pin above is bumped) -- this file's own
`stripe.api_version` line does not do it for you. Do not bump either pin
without re-auditing every handler in `billing.py` against the new shape.

### 1. Products and prices

Create two Products in the Stripe Dashboard (Live mode, then mirror in Test
mode for local development):

* **Bill Commons API** -- recurring prices:
  | Lookup key | Price | Interval | Env var |
  |---|---|---|---|
  | `bc_builder_monthly` | $49 | monthly | `STRIPE_PRICE_BUILDER_MONTHLY` |
  | `bc_builder_annual` | $490 | yearly | `STRIPE_PRICE_BUILDER_ANNUAL` |
  | `bc_scale_monthly` | $299 | monthly | `STRIPE_PRICE_SCALE_MONTHLY` |
  | `bc_scale_annual` | $2,990 | yearly | `STRIPE_PRICE_SCALE_ANNUAL` |
* **Bill Commons Bulk Snapshot** -- one-time prices:
  | Lookup key | Price | Env var |
  |---|---|---|
  | `bc_snapshot_state` | $99 | `STRIPE_PRICE_SNAPSHOT_STATE` |
  | `bc_snapshot_full` | $499 | `STRIPE_PRICE_SNAPSHOT_FULL` |

Snapshot Checkout currently sends `payment_method_types=["card"]`. That is a
deliberate P0 fulfillment boundary: the webhook provisions only a paid
`checkout.session.completed` event and does not subscribe to
`checkout.session.async_payment_succeeded`. Do not enable delayed payment
methods until that event is implemented and covered by an idempotent
entitlement/notification test.

Set `statement_descriptor_suffix = BILLCOMMONS` on both Products. The app
reads price IDs from environment variables (never a `lookup_key` fetch at
boot) -- copy each Price's `price_...` id into the matching Railway env var
above. Do **not** set `metadata.app` on the Product/Price in the Dashboard
UI (Stripe doesn't let you tag Prices there in a way the webhook checks
anyway) -- the tagging that matters is on the Customer, Checkout Session,
and Subscription objects the API creates at request time, which
`routers/billing.py` already does in code.

### 2. Restricted API key

Dashboard → Developers → API keys → **Create restricted key**, named
`billcommons-api`, with write access to: Customers, Checkout Sessions,
Subscriptions, Billing Portal Sessions, Payment Intents, Charges/Refunds
(read), Webhook Endpoints (none needed at runtime). Put the `rk_live_...`
value in `STRIPE_SECRET_KEY`. Repeat with a `rk_test_...` key for the Test
mode / local-dev value.

### 3. Webhook endpoint

Dashboard → Developers → Webhooks → **Add endpoint**:
`https://api.billcommons.org/api/v1/billing/webhook`. Select exactly these
events (R7 -- this list REPLACES anything broader):

* `checkout.session.completed`
* `customer.subscription.created`
* `customer.subscription.updated`
* `customer.subscription.deleted`
* `invoice.paid`
* `invoice.payment_failed`
* `charge.refunded`

Copy the endpoint's signing secret into `STRIPE_WEBHOOK_SECRET`.

### 4. Customer Portal configuration

Dashboard → Settings → Billing → Customer portal:

* **Disable "Customers can update their email address."** Email is the
  immutable customer identity in this app (`api_customers.email` is the
  upsert key, A1) -- letting Stripe's portal change it out from under us
  would desync the two.
* Allow plan switching between Builder ⇄ Scale, and cancellation.
* Return URL: `https://billcommons.org/account` (the app already passes
  this per-request; the Dashboard default is only a fallback).

2026-08-21 fix-pass note (item 2): a Portal plan switch changes the
subscription's `items[0].price` but Stripe never rewrites
`subscription.metadata`. The sync now derives the plan from the CURRENT
price id first (built from the `STRIPE_PRICE_*` env vars above), falling
back to `metadata.plan` only when the price id doesn't match a known
plan (logged at WARN -- e.g. a Dashboard-created subscription on a price
outside the four env vars). A Portal switch is therefore reflected on the
very next `customer.subscription.updated` webhook, not only at the next
`checkout.session.completed`.

### 5. Local dev loop (`stripe listen`)

```bash
stripe login
stripe listen --forward-to localhost:8000/api/v1/billing/webhook
# prints a whsec_... -- put it in .env as STRIPE_WEBHOOK_SECRET for local runs
stripe trigger checkout.session.completed
```

Test-mode keys (`bc_test_...`) get the plan from a test-mode subscription,
so paid-tier behavior is fully testable without a live card.

### 6. Refund procedure

**Subscriptions:** Dashboard → the customer → Refund the charge. To ALSO
end access immediately (rather than let it run to period end), set
`cancel_access = "true"` in the **Refund's own metadata field** in the
Dashboard when issuing it. 2026-08-21 fix pass (item 12): the handler now
scans EVERY refund on the charge for the flag (`any(refund.metadata.
cancel_access == "true" for refund in refunds)`), not just
`refunds.data[-1]` -- Stripe's refund lists are newest-first, so `[-1]`
was actually the OLDEST refund, meaning a charge with a prior goodwill
refund silently ignored an explicit "cut access" instruction on a later
one. Leave the flag unset for a goodwill/partial refund that shouldn't
touch access (C8/D3).

**Snapshots:** refundable **only until the download link has been sent**
(`snapshot_entitlements.delivered_at IS NULL`) -- refunding after delivery
is a Dashboard action that does nothing but email the operator (C5); this
is stated on `/docs/bulk` and `/terms` above the buy button so it's not a
surprise.

### 7. "Scraper appears → convert" (base spec §7, unchanged by Phase 2)

1. Detect via the daily digest or `GET /api/v1/admin/usage` (anonymous
   heavy traffic concentrated in one /24).
2. They already receive `429` + `_BULK_ACCESS_MESSAGE` pointing at
   `/docs/bulk`, which now has live Checkout buttons for both the
   full-corpus snapshot and a state snapshot -- the intended outcome is
   they buy the $499 snapshot or subscribe to Scale, not that they keep
   scraping.
3. Persisting >24h without converting: resolve the IP owner (AWS reverse
   DNS / ASN abuse contact) and send the outreach email -- reuse
   `docs/operations/data-access-outreach-2026-08-09.md` +
   `send_data_access_outreach.py` -- offering a 14-day free Scale key and
   the $499 snapshot.
4. No reply in 72h: drop that /24's heavy limit to 5/min with a 429 body
   saying only "contact sales@billcommons.org". Never a silent block.

## §manual-snapshot -- fulfilling a snapshot order (Phase 2/3 stand-in)

Until the nightly automated builder (Phase 3) ships, a snapshot purchase is
fulfilled by hand within one business day:

```bash
# 1. Export each table to Parquet (zstd), full corpus + per-jurisdiction.
#    Run from a machine with DATABASE_URL set and duckdb installed.
duckdb -c "
INSTALL postgres; LOAD postgres;
ATTACH '<DATABASE_URL>' AS pg (TYPE postgres, READ_ONLY);
COPY (SELECT * FROM pg.bills) TO 'out/bills.parquet' (FORMAT parquet, COMPRESSION zstd);
COPY (SELECT * FROM pg.bill_versions) TO 'out/bill_versions.parquet' (FORMAT parquet, COMPRESSION zstd);
COPY (SELECT * FROM pg.bill_documents) TO 'out/bill_documents.parquet' (FORMAT parquet, COMPRESSION zstd);
COPY (SELECT * FROM pg.document_text) TO 'out/document_text.parquet' (FORMAT parquet, COMPRESSION zstd);
COPY (SELECT * FROM pg.bill_actions) TO 'out/bill_actions.parquet' (FORMAT parquet, COMPRESSION zstd);
COPY (SELECT * FROM pg.sponsorships) TO 'out/sponsorships.parquet' (FORMAT parquet, COMPRESSION zstd);
COPY (SELECT * FROM pg.vote_events) TO 'out/vote_events.parquet' (FORMAT parquet, COMPRESSION zstd);
COPY (SELECT * FROM pg.vote_records) TO 'out/vote_records.parquet' (FORMAT parquet, COMPRESSION zstd);
"

# For a state-scoped ($99) order, add a WHERE jurisdiction = '<abbr>' clause
# to each COPY and write to out/by_state/<AB>/ instead.

# 2. sha256 + manifest
sha256sum out/*.parquet > out/manifest.sha256

# 3. Upload to R2 (bucket billcommons-snapshots, one-off manual key --
#    the nightly builder's automated key layout is v1/{YYYY-MM-DD}/{scope}/…;
#    a manual fulfillment can reuse the same layout under manual/{order-id}/)
aws s3 cp out/ s3://billcommons-snapshots/manual/<order-id>/ --recursive \
  --endpoint-url https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com

# 4. Generate a 7-DAY presigned URL (NOT the 15-min self-serve TTL --
#    amendment A7 -- this is a manually-emailed link, not an API response)
python3 - <<'PY'
import boto3
s3 = boto3.client(
    "s3",
    endpoint_url="https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com",
    aws_access_key_id="<R2_ACCESS_KEY_ID>",
    aws_secret_access_key="<R2_SECRET_ACCESS_KEY>",
)
url = s3.generate_presigned_url(
    "get_object",
    Params={"Bucket": "billcommons-snapshots", "Key": "manual/<order-id>/bills.parquet"},
    ExpiresIn=7 * 24 * 3600,
)
print(url)
PY

# 5. Email the buyer the link(s), then mark the entitlement delivered:
```

```sql
update snapshot_entitlements
   set delivered_at = now()
 where stripe_checkout_session_id = '<checkout-session-id>';
```

Refund rule (amendment B5/A7): a `charge.refunded` webhook (Phase 2) revokes
the entitlement iff `delivered_at IS NULL` -- once step 5 above has run,
a refund no longer auto-revokes it.

Revoked paid API keys remain revoked across later subscription events and
renewals. To re-provision after a revocation, the customer must self-serve
through `/account` or an operator must perform the manual recovery flow.

## Env vars introduced across Phase 1 + 2 (names only, no values)

Phase 1: `BILLCOMMONS_REVEAL_KEY`, `ACCOUNT_SESSION_SECRET`,
`BILLCOMMONS_ADMIN_TOKEN`, `BILLCOMMONS_ANON_DAILY_LIMIT`,
`BILLCOMMONS_ANON_DAILY_LIMIT_SUBNET`, `BILLCOMMONS_ALLOWED_ORIGINS`.

Phase 2: `OPERATOR_ALERT_EMAIL`, `BILLCOMMONS_PUBLIC_SITE_URL`,
`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_BUILDER_MONTHLY`,
`STRIPE_PRICE_BUILDER_ANNUAL`, `STRIPE_PRICE_SCALE_MONTHLY`,
`STRIPE_PRICE_SCALE_ANNUAL`, `STRIPE_PRICE_SNAPSHOT_STATE`,
`STRIPE_PRICE_SNAPSHOT_FULL`.

`RESEND_API_KEY` already existed (feedback.py).
