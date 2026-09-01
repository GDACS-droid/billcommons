# Private Scout canary harness

`scripts/private_scout_canary.py` is an operator-only, production canary
harness. It is deliberately not a deployment or configuration tool. It makes
one owner-scoped Scout request through the configured HTTPS API and only after
all local guards pass.

Before using it, an authorized operator must have completed the private cohort
steps in the [deployment runbook](deployment-runbook.md#4-scout-cohortcanary-enablement): the API and Scout worker must be enabled for the named
cohort, evidence storage must be healthy, and public rollout must remain off.

The harness requires all of the following inputs. Do not put their values in a
shell history, ticket, or captured terminal transcript.

- `BILLCOMMONS_SCOUT_CANARY_EMAIL`, or `--email`: the single normalized canary
  identity to create or reuse.
- `BILLCOMMONS_SCOUT_CANARY_QUERY`, or `--query`: one Florida Scout query.
- `BILLCOMMONS_SCOUT_CANARY_API_BASE`, or `--api-base`: the HTTPS API origin.
- `BILLCOMMONS_SCOUT_CANARY_EMAILS`: nonempty server-side private cohort that
  includes exactly that normalized email.
- `BILLCOMMONS_SCOUT_ENABLED=true` and
  `BILLCOMMONS_SCOUT_ALLOW_PUBLIC=false`: the feature must be on for the
  named cohort but never in public-rollout mode.
- `BILLCOMMONS_SCOUT_CANARY_ORIGIN`, or `--origin`, if the standard
  `https://billcommons.org` origin is not in `BILLCOMMONS_ALLOWED_ORIGINS`.
- `DATABASE_URL` and `ACCOUNT_SESSION_SECRET`: existing operator/runtime
  configuration used to safely upsert the account and create the same signed
  account-session cookie as the API. The program never prints either value or
  the signed cookie.
- `--ack-production-canary`: explicit acknowledgement that the request can
  cause bounded production work.

The harness rejects HTTP endpoints, an empty cohort, a non-allowlisted email,
and `BILLCOMMONS_SCOUT_ALLOW_PUBLIC=true` before it opens a database session or
calls the API. It sends the initial request, polls until an actual terminal
state, then submits the exact same request once more only for completed or
partial work. Success requires the second response to be a fresh cache reuse.
Failed or canceled work is reported truthfully and is not retried because it
is not cacheable.

Its JSON output is intentionally limited to the observed terminal (or timeout)
status, partial flag, finding count, and integer usage counters. It excludes customer and job IDs,
session cookies, API keys, API response bodies, source URLs, excerpts, and
source bytes.

Run from the repository root with the API, shared, and schema packages
available on `PYTHONPATH`:

```bash
PYTHONPATH=apps/api:packages/shared:packages/schema \
  python scripts/private_scout_canary.py --ack-production-canary
```

The script performs no deployment, migration, provider smoke, or external
configuration change. A nonzero exit is a canary result requiring operator
review, not a reason to resubmit blindly.
