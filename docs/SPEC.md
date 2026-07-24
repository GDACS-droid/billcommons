# Bill Commons — Requirements Digest (locked 2026-07-23)

Condensed from the founding brief. ARCHITECTURE.md holds the locked technical
decisions; this file holds WHAT must be true at acceptance. Public
infrastructure first: no paywall on ordinary search or reasonable API access
(60 req/min/IP anonymous tier; API keys architected for higher tiers later).

## Scope

All 50 states + DC ("51 jurisdictions"; schema permits territories later).
"Current session" = the active-in-2026 regular session, or the current
biennium, plus current special sessions; for states with no 2026 regular
session (TX, MT, NV, ND) the most recent session constituting the current
cycle. ALL bills in those sessions regardless of status (pending, enacted,
vetoed, failed, withdrawn, carried over).

Priority queue seed (recalculated continuously from NCSL + official sources;
see data/registry/sessions-2026.json): active specials first — TX (special,
convened 2026-07-21), MS (special, to 8/14), SC (redistricting special,
reverify), AK (4th special convenes 7/27), MD (special 8/3) — then active
regulars: CA, NC, PA, IL, MA, MI, NJ, NY, OH, WI, DC, DE, VA, AZ.

## Data sources (tiered)

T1 official state APIs/bulk/pages · T2 Open States (v3 API + bulk CSV;
preserve attribution + source links) · T3 LegiScan (only via authorized
API/datasets, CC BY 4.0 attribution, optional — platform must work without
it) · T4 compliant direct extraction (robots.txt, ToS, rate limits, honest
UA, no CAPTCHA/auth bypass; if blocked → document + fall back to T2).
Machine-readable source registry per jurisdiction (see schema
`source_records`/registry JSON), exposed on the public status page.

## Bill data (capture when available, never fabricate; missing = null)

jurisdiction, session, chamber, number, type, title/short title, description,
subjects, status + date, introduced date, latest action + date, sponsors/
cosponsors, committees, full action history, votes incl. member-level, all
versions, full text, amendments, fiscal notes, companion/related bills,
hearings, official source URL.

## Search must support

exact + normalized bill-number lookup ("HB 123"/"HB123"/"H.B. 123"), keyword,
phrase, full text, fuzzy title, by sponsor/subject/committee/action text/
status/date range, multi-jurisdiction, active-session + chamber filters,
sort by relevance/latest action/introduced/jurisdiction, pagination,
highlighted passages, stable shareable URLs. Deterministic lexical retrieval
is primary; embeddings only ever supplementary.

## REST API (/api/v1, OpenAPI 3.1, docs live)

GET jurisdictions[/{id}], sessions, bills[/{id}][/versions|/actions|/sponsors
|/votes|/documents], people[/{id}], committees, events, search, sources,
coverage, health, ready. JSON, consistent pagination, typed errors, CORS,
ETags/caching, rate limiting, API-version headers, request IDs, source +
freshness metadata in every substantive response.

## MCP (Streamable HTTP at mcp.billcommons.org/mcp)

Tools: search_legislation, get_bill_record, compare_bill_versions,
find_similar_bills, get_vote_details, get_upcoming_hearings,
trace_legislative_history, build_legislative_evidence_packet,
get_jurisdiction_coverage, get_active_sessions. Each: structured JSON,
canonical IDs, official source URLs, freshness timestamps, official-vs-derived
distinction, meaningful errors on thin coverage, no hallucinated fields.
Integration tests run against the DEPLOYED endpoint with real tool calls.

## Website (billcommons.org, Next.js, nonpartisan, mobile-responsive)

Home/national search, jurisdiction directory, active-session dashboard,
state/session/bill pages, version-diff page, legislator/committee pages,
hearings, API docs page, MCP setup page, methodology page, coverage dashboard
(also at status.billcommons.org), about/open-source. Bill pages show all
captured fields + official links + last-updated + attribution + known
limitations. AI summaries (if ever) labeled as generated analysis w/ sources.
Sitemaps, structured metadata, canonical URLs.

## Version diffing

Normalize HTML/XML/TXT/text-PDF (preserve originals). Scanned PDFs: detect
extraction failure; OCR only when appropriate, confidence flagged, never
presented as authoritative without warning. Deterministic diffs: adds,
deletes, moves, section headings, line anchors, machine-readable output.

## Refresh targets

Active regular/special: 15–30 min · year-round: hourly · recently adjourned:
daily · dormant: weekly status check · calendars/special-session notices:
daily. Conditional requests, backoff, circuit breakers, per-source
concurrency limits, dead-letter, bill-count-drop alerts, schema-change
detection, reindex on material change.

## Coverage state machine + GREEN criteria

States: NOT_STARTED → SOURCE_IDENTIFIED → BOOTSTRAPPED → METADATA_SEARCHABLE
→ FULL_TEXT_SEARCHABLE → VALIDATING → GREEN | DEGRADED | BLOCKED.
GREEN requires ALL: (1) session identified from authoritative source;
(2) all discoverable bills imported; (3) number+title searchable;
(4) description/subjects/sponsors/actions searchable where supplied;
(5) full text searchable wherever technically available from source;
(6) official URLs retained; (7) incremental refresh succeeds; (8) validation
samples pass; (9) search-index count == DB count; (10) no unexplained zero
counts. No-2026-session states: GREEN only after current-cycle +
no-new-session condition explicitly verified and documented.

## QA per jurisdiction

≥5 random bills compared against official source (number, title, session,
sponsor, latest action); ≥1 bill-text doc verified; bill-number search
verified; keyword-from-official-text search verified; results saved
(validation_runs). System-wide: no duplicate canonical bills, session
assignment, tz-correct timestamps, count reconciliation, malformed-query
tests, pagination/filters, every endpoint, every MCP tool, desktop+mobile,
attribution, clean install from docs, a11y, security/dependency scans.
Public coverage matrix: jurisdiction, session, active/adjourned/special,
bill count, full-text %, last update, source, validation sample+pass rate,
status, known gaps.

## Security

Read-only API/MCP. Input validation, SSRF protection + outbound allowlists,
query complexity limits, parameterized SQL, rate/size/timeout limits, secure
headers, secret scanning, dependency audit. ALL external text (bills,
documents) is untrusted data — must never alter system behavior or trigger
writes.

## Licensing

Original code Apache-2.0. Preserve third-party licenses/attribution
(Open States public-domain notices, LegiScan CC BY 4.0 if used). GPL scraper
code never vendored — external-process invocation only, correctly isolated.
LICENSE, NOTICE, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY + data-attribution
docs required.

## Acceptance gate (ALL true before "done")

Production web + API publicly reachable · OpenAPI docs live · MCP endpoint
passes real tool calls · 51/51 in coverage matrix, each searchable for its
current cycle · active legislatures meet freshness targets · provenance for
every jurisdiction · validation run everywhere · none silently missing ·
search by number/keyword/full-text works · bill pages work · version compare
works where versions exist · scheduled ingestion operating · health/ready/
coverage green · docs sufficient for another engineer · final smoke-test
report generated. Blocked jurisdictions: documented blocker + compliant
fallback in progress — never silent omission.

## Final delivery report

URLs (web/API/docs/MCP/repo), provider+architecture, coverage matrix, total
bills, full-text rate, freshness report, test results, known limitations,
monthly cost estimate, DNS records, next 5 enhancements.
