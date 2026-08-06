# How do I…

A map from the questions a new consumer actually asks to the concrete
endpoint or MCP tool that answers them. Written after a real external
evaluator (a platform-policy team tracking youth-safety/AI bills) filed
gaps that turned out to already be built but were not discoverable from
the tool/endpoint surface alone -- this file is that fix.

Base API URL: `https://api.billcommons.org/api/v1` (see `docs/api/examples.md`
for full request/response examples, `docs/SPEC.md` for the whole contract).
MCP server: `https://mcp.billcommons.org/mcp` (Streamable HTTP, 11 tools,
anonymous/keyless, per-IP rate limited).

## …discover new bills on a subject?

Two tools, and they answer different questions.

- **`GET /search?q=...`** (MCP: `search_legislation`) is a full-text search
  over bill titles, descriptions, AND ingested document text (not just a
  known-bill-number lookup) -- `websearch_to_tsquery` syntax, so
  `"age verification"` (quoted phrase), `moderation OR disclosure`, and
  `privacy -health` (exclusion) all work, with a fuzzy title-similarity
  fallback if the exact query has no hits. Use this for an arbitrary
  keyword or phrase you supply.
- **`GET /topics`** / **`GET /topics/{slug}`** (MCP: `list_topics`) are
  curated cross-state trackers -- e.g. `artificial-intelligence`,
  `youth-online-safety`, `platform-accountability`, `cybersecurity`,
  `data-privacy`, `cryptocurrency`, `local-government` -- each a title +
  structured-subject-tag membership rule tuned against the live corpus for
  precision over recall (not a raw keyword match, so it also catches bills
  a keyword search would miss via subject tags). `list_topics` returns each
  topic's live `bill_count` and how to fetch its bills. Call this FIRST if
  the question is "does Bill Commons track subject X across states" rather
  than "find bills matching this phrase."

## …monitor status changes on bills I care about?

**`GET /changes`** -- an append-only, cursor-paginated change feed over
`bill_events`. Poll with the `next_cursor` from the previous response;
`has_more: false` means you're caught up. Two properties it guarantees:
NEVER SKIP (a consumer that pages to the end has seen every event) and
TOTAL ORDER (one monotonic cursor, no ties). Filter with `ids` (up to 200
bill UUIDs -- this is what makes it usable for a watchlist instead of
diffing the whole national feed), `jurisdiction`, or `kind` (`created`,
`status`, `actions`, `sponsors`, `text`, `metadata`, `votes`).

**Safety lag**: the feed never serves an event less than
`COMMIT_SAFETY_LAG_SECONDS` (120s) old. `seq` is allocated at INSERT but
only becomes visible at COMMIT, so a slow writer can hold a LOWER seq that
only appears after higher ones are already visible -- serving right up to
"now" risks a consumer advancing past a change it never actually saw. This
bounds the failure to *latency* (up to 120s late) instead of *silently
missing a change forever*. Poll no more often than every ~30-60s; polling
faster than the lag window just re-fetches the same watermark.

**Per-jurisdiction Atom feed**: `GET /feeds/{jurisdiction}.atom` (e.g.
`/feeds/NC.atom`) -- the most recent ~100 change events for one
jurisdiction as a standard Atom 1.0 feed, honoring the same safety-lag
discipline as `/changes`, for consumers that want an RSS-reader-compatible
feed rather than a JSON polling loop. A jurisdiction with no recent events
still returns a valid feed with zero entries -- that is a real, quiet
answer, not an error.

## …get notified by email instead of polling?

**`POST /alerts/subscribe`** -- subscribe an email to a topic's digest
(national, or scoped to one jurisdiction via the `jurisdiction` field).
Sent nightly, at most 30 events per email with a link to the full tracker
for anything beyond that. A brand-new subscription starts from the day it
was created, not a replay of history. Unsubscribe link is in every email
(`GET /alerts/unsubscribe?token=...`, no auth required, by design).

## …get pushed events instead of polling `/changes`?

**`POST /api/v1/webhooks`** -- push delivery over the same `bill_events` log
`/changes` serves, for a topic, one jurisdiction, or up to 64 specific bill
ids (a smaller cap than `/changes`' own `ids` filter above -- a webhook
subscription's url/kind/target/event_kinds together have to fit inside a
Postgres uniqueness index, which `/changes`' stateless `ids` filter never
does). See `docs/api/webhooks.md` for the full contract (signature
verification, retry/disable policy, and how to backfill a gap with
`/changes` using a delivered `cursor`). Short version: creation returns
`verified: false` immediately -- the API never makes outbound HTTP calls, a
separate worker verifies your endpoint within a couple of minutes and then
starts delivering, at-least-once, with an HMAC signature over every POST.

## …check coverage or jurisdictions before trusting an empty result?

**`GET /jurisdictions`** / **`GET /jurisdictions/{id}`** (accepts either a
UUID or a 2-letter abbreviation, e.g. `/jurisdictions/NC`) and
**`GET /coverage`** (MCP: `get_jurisdiction_coverage`) -- Bill Commons is
under active ingestion across all 50 states + DC, and coverage is uneven.
An empty search result or thin bill list does NOT mean "no such
legislation exists" -- check coverage status first. `search_legislation`
and `/topics/{slug}` attach a structured `coverage_warning` automatically
when the relevant jurisdiction is below a reliable coverage threshold.

## …get federal (US Congress) bills?

**Not covered, and this is permanent, not a gap in progress.** Bill Commons
is STATE legislatures only: no federal bills, no H.R./S. numbers, no
Congressional committees, and no city/county ordinances either (state bills
that *regulate* cities -- preemption, home rule, municipal finance -- ARE in
scope and searchable). An empty result for a federal bill number or a
municipal code question is not evidence of anything about that bill; it's
simply out of scope. See the MCP server's `instructions` block for the full,
explicit out-of-scope list (current session only, no hearings/committee
calendars, no legislator/committee records beyond bare sponsor names).

## …get vote records?

**`GET /bills/{id}/votes`** (MCP: `get_vote_details`, by `bill_id` or a
single `vote_event_id`) -- vote events with member-level yes/no/other/absent
records where the upstream source provides them. A new vote also lands in
the change feed as a `votes` event with a human-readable tally in `detail`
(e.g. `"House: Third Reading -- passed 98-12"`) the moment it's ingested,
so a `/changes` or `/feeds/{jurisdiction}.atom` subscriber learns a vote
happened without having to separately poll each bill's vote list.

## …cite a bill or a specific claim about it?

**`build_legislative_evidence_packet`** (MCP only) -- compiles a single
citation-ready packet (full record, timeline, votes, hearings) with
explicit official-vs-derived labeling on every field, a `how_to_cite` block
(one-line `cite_as`, `permalink`, `snapshot_id` that changes if and only if
a cited fact changes), and per-section truncation flags so a capped section
never silently reads as "that's the whole history."
