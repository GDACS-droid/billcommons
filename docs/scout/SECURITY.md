# Scout security

External content is hostile data, never instruction. P0 accepts a query and
jurisdiction, not an arbitrary URL.

## Enforced boundaries

- Every job/session/evidence/replay operation authenticates the account and scopes
  by `customer_id`; writes also apply the existing CSRF origin policy.
- Direct URL admission permits only HTTPS public destinations, pins resolved public
  addresses, and revalidates every redirect. Loopback, private, link-local, metadata,
  NAT64-private, userinfo, non-default ports, `file:`, `data:`, and URLs over 4096
  characters are rejected.
- Direct retrieval has request/redirect/retry/time/decompression limits and a 2 MiB
  body ceiling. PDFs additionally require MIME plus `%PDF-`; parsing occurs only in
  a spawned child with parent wall timeout, CPU/address-space, page, and returned-text
  caps. There is no in-process parser fallback.
- Retrieved excerpts render as React text, never HTML. Extractors cannot execute a
  shell, call tools, read credentials, or treat document prose as instructions.
- Browser work uses an allowlisted target, a fresh context, blocked service workers,
  no WebSockets, closed popups, per-request route validation, and independent wall,
  page, action, routed-request, byte, concurrency, daily-runtime, and cleanup bounds.
- Provider IDs and replay URLs are never public evidence. Replay resolution is
  owner-authorized operational context; safe logs contain normalized error classes.
- Sessions created through the Scout worker or durable lifecycle canary retain
  creation, use, release uncertainty, replay, and usage state. Cleanup runs on
  success/error/cancellation; leases, outcome-unknown heartbeats, an in-flight
  registry, and the reaper cover timeout/process failure. The separate low-level
  `solari-check` is an explicitly non-authoritative SDK smoke and is not lifecycle
  or cleanup-recovery evidence.
- Session creation is one-shot (`max_attempts=1`) because create has no idempotency
  key. If its outcome is unknown, Scout stops further browser escalation, charges a
  full session reservation, and holds a global slot from the outcome timestamp through
  the frozen drive-plus-cleanup horizon before marking it expired.

The cloud browser cannot pin the provider's DNS resolution the way the direct client
does. Browser navigation is therefore restricted to fixed official HTTPS hostnames,
re-admits every requested/redirect URL, blocks other schemes/hosts and WebSockets,
and depends on Solari's network resolver against DNS rebinding. This is an explicit
provider-boundary residual, not a claim of direct-client-equivalent IP pinning.

## Florida discovery boundary

Related-document links are untrusted data. Scout accepts only same-host,
same-session, same-bill Senate analysis/amendment paths, removes non-identity query
aliases, deduplicates before fetch, applies the job's immutable budgets, and rejects
HTML soft errors at PDF routes. Unchanged bytes reuse the content identity; changed
bytes retain a predecessor relation.

## Authorization and denial-of-wallet

Equivalent active/fresh queries coalesce. New jobs are limited per customer and
require either normalized server-side canary emails or a separate explicit public-
rollout acknowledgement. Customer-row locking reserves
worst-case browser time before admission; terminal actual runtime replaces the
reservation. Global live/cleanup-failed sessions consume durable slots. Request-
budget exhaustion becomes a truthful partial result, never a false completion.

Platform admission is serialized in PostgreSQL and caps active jobs, daily new jobs,
and actual-plus-reserved browser runtime across all customers. Per-owner quotas still
apply inside those ceilings. Admission also reserves every active job's worst-case
remaining evidence bytes against the retained-store ceiling, so a job is rejected
before retrieval or provider spend if the evidence contract cannot be met. Raw blobs
are capped at 2 MiB each and the PostgreSQL store has a separate advisory-lock-protected
retained-byte high-water mark; identical content is free, while a late concurrent
capacity failure terminalizes truthfully as `rawstore_capacity_exceeded`. Expired
terminal staging rows and old unreferenced blobs are reaped behind a live-finalization
barrier; referenced evidence is never deleted merely to reclaim capacity.

Scout evidence is immutable, SHA-256-addressed PostgreSQL data. Concurrent puts are
idempotent; reads re-hash bytes before returning them. Fresh unresolved stages block
orphan collection until a hash attaches or the retention window expires. This removes
the insecure assumption that two deployment services share a filesystem path.

## Verification and residual operations

Tests cover SSRF/private redirects, hostile prompt text, bad/oversized MIME, malformed
PDFs, XSS-safe rendering, CSRF/IDOR, duplicate/adversarial submissions, quotas,
browser crash/cancel/timeout, cleanup failure/reaping, delayed replay, and related-
document scope/dedupe/change. The guarded API/shared suite now refuses any database
other than an explicitly acknowledged local `_test` Postgres target. The final
backend/operations suites total 862 passing tests with 8 explicit skips; the web
contract adds 10 passing tests plus lint, typecheck, and production build.

Fresh independent review found no open critical/high Scout code issue. The authorized
dark window verified the pinned API/worker artifacts, named-account/private-only
bindings, additive `0025` revision, 300-second worker grace, service/read-path
monitoring, a durable released Solari session, and feature-off rollback reconciliation.
The dated production backup and disposable restore evidence is recorded in
`PRODUCTION_CANARY.md`.
The pre-existing local magic-link fallback logging behavior is outside Scout; Resend
must be configured correctly before production account flows take traffic.
