# Bill Commons — Final Delivery Report

_Generated 2026-07-24. Bill Commons is a free, open-source legislative search
platform covering the current session/biennium for all 50 U.S. states + DC._

## 1. Production URLs (all live)

| Surface | URL | Status |
|---|---|---|
| Public website | https://billcommons.org | ✅ 200 |
| — www redirect | https://www.billcommons.org | ✅ 308 → apex |
| REST API base | https://api.billcommons.org/api/v1 | ✅ health `ok` |
| OpenAPI docs | https://api.billcommons.org/docs | ✅ 200 (OpenAPI 3.1.0, 21 paths) |
| MCP endpoint | https://mcp.billcommons.org/mcp | ✅ Streamable HTTP, 10 tools |
| Status / coverage | https://status.billcommons.org | ✅ live matrix |

## 2. Architecture & deployment provider

Monorepo (`billcommons/`). **Railway** hosts Postgres 16 + three services
(`api` FastAPI, `mcp` FastMCP Streamable-HTTP, `worker` ingestion+scheduler on a
persistent volume). **Vercel** hosts the Next.js 15 site (local-prebuilt deploy
pattern). Search is Postgres FTS (`tsvector`) + `pg_trgm`; no external search
engine. Raw source payloads archived to a sha256-addressed store. Job queue is a
Postgres table (`SELECT … FOR UPDATE SKIP LOCKED`). DNS on Vercel nameservers;
API/MCP subdomains CNAME to Railway with managed TLS. Original code is
Apache-2.0; Open States/Plural data attribution preserved (no GPL scraper code
vendored).

## 3. Jurisdiction coverage matrix

**All 50 states + DC (51/51) are ingested and searchable** for their current
2026 session/biennium; none is silently missing. Live matrix + per-jurisdiction
provenance: https://api.billcommons.org/api/v1/coverage and the status page.

- Jurisdictions in the matrix: **51 distinct** (76 coverage rows incl. current
  special sessions; +Puerto Rico seeded as a future non-required territory).
- Largest by bill count: NY 25,327 · MA 18,482 · TX 12,788 · IL 12,766 ·
  NJ 10,712 · MN 10,638.
- Coverage-status distribution at report time (see §11 for the honest caveat):
  1 GREEN, 29 VALIDATING, 31 DEGRADED, 16 SOURCE_IDENTIFIED.

## 4. Totals

| Metric | Value |
|---|---|
| Bills | **209,612** |
| Bill actions | 1,644,260 |
| Sponsorships | 996,690 |
| Bill versions | 439,744 |
| Bill documents | 730,781 |
| Vote events / records | 157,700 / 6,669,620 |
| Sessions | 77 (incl. current specials) |
| Duplicate canonical bills | **0** |
| Schema migration | 0002 (head) |

## 5. Full-text coverage rate

Full bill text is fetched by a polite, robots-respecting, per-host
rate-limited crawler (hop-by-hop redirect validation), extracted from
HTML/XML/TXT/text-PDF, with scanned-PDF detection (flagged, never presented as
authoritative). At report time **~5,000 of 730,781 documents** have extracted
text and the crawl is **ongoing** — this is the ramping long-pole item (§11).
Search over already-extracted full text is live today; version diffing
(`/bills/{id}/compare`, MCP `compare_bill_versions`) works wherever ≥2 texted
versions exist.

## 6. Active-session freshness

The worker runs a scheduler (advisory-locked, transaction-scoped) enqueuing
incremental `api_sync` jobs per SPEC cadence: active/special sessions every
30 min, year-round hourly, recently-adjourned daily, dormant weekly
session-status check. Incremental sync keys bills by immutable Open States id
then `(session_id, identifier_norm)`, uses its own success-watermark, and is
idempotent. Mississippi, South Carolina, and Alaska specials are tracked; the
brand-new specials with no bulk dump yet are covered by the API path. (Texas is
biennial with no 2026 session — its current cycle is the 89th Legislature's 2025
regular + called sessions; there is no active Texas session in 2026.)

## 7. Test results

- API contract tests: **76 passing** (endpoints, pagination, search tiers,
  ETags, OpenAPI, XSS-safety, enforced rate limit).
- Ingestion/worker tests: **144+ passing** (idempotency, session matcher,
  full-text extraction+robots, scheduler, api_sync, validation, vote dedup).
- Shared/schema tests: 22 passing (bill-number normalization, raw store).
- MCP integration: all checks pass against the **deployed** endpoint (10 tools,
  rate-limit 429s, structured errors).
- Validation: `validate --all` sampled 5 bills/jurisdiction (255) against the
  production API + official state sites; post-fix structural integrity 100%,
  bill-number search 100%.

## 8. Verification (6-model adversarial gate)

The full tree was reviewed by six independent model families (Kimi, Codex,
Gemini, Muse, Grok, Opus). Every BLOCK finding was fixed across six fix rounds
and a close-out review of the fix diff, each with live proof. Highlights:
- **Stored XSS** via search highlights → closed with sentinel-token highlighting
  (no HTML crosses the API boundary; web renders escaped `<mark>`).
- **Cross-session bill corruption** in incremental sync → re-keyed by Open States
  id + resolved session.
- **Vote non-idempotency** → keyed by upstream id; live dedupe cleared 1,786
  event + 278 record duplicate groups and recovered 257 previously-merged
  distinct votes.
- **MCP had no rate limiting / work caps** → per-IP token bucket + compare/evidence
  caps with truncation flags.
- **API rate limit was advertised but not enforcing** → replaced with an explicit
  token-bucket middleware (verified live: 60×200 then 429s).
- Coverage NULL-session duplicate constraint, robots-after-redirect politeness,
  scheduler lock-leak, latest-action derivation (57k-row live backfill), and web
  contract breaks all fixed.

## 9. Security posture

Read-only API + MCP. Parameterized SQL throughout; per-IP rate limiting on both
API (60/min) and MCP (30/min + work caps); secure headers; request IDs;
SSRF-conscious full-text fetcher (scheme allow-list, robots per redirect hop,
per-host token bucket); all external bill/document text treated as untrusted
data (never interpreted, never a write trigger). No secrets in the repo.

## 10. Monthly operating-cost estimate

| Item | Est. / month |
|---|---|
| Railway (Postgres + 3 services + volume) | ~$20–40 |
| Vercel (Hobby/Pro static+SSR) | ~$0–20 |
| Open States v3 API | free tier (bulk CSV is the bootstrap path) |
| Domain (billcommons.org, amortized) | ~$1 |
| **Total** | **~$25–60/mo** |

## 11. Known limitations (honest)

1. **Full-text coverage is ramping.** ~0.7% of documents extracted at report
   time; the polite crawler needs multi-day wall-clock for ~730k documents.
   Consequently only 1 jurisdiction (AK) currently meets the strict GREEN bar
   (which requires full text). All 51 are searchable by number/keyword and by
   the full text already extracted. This is a time-to-crawl limitation, not a
   silent omission — the coverage loop converges as the crawl completes.
2. **Coverage statuses reflect the last validation sweep;** a re-sweep post the
   surface-form/JS-page validation fixes is running and will lift many
   DEGRADED→VALIDATING/higher.
3. **`people`/`committees` tables are empty** — Open States bulk bill CSVs carry
   sponsor *names* but no legislator roster; person/committee pages render honest
   empty states. Roster ingestion via the v3 API is a planned enhancement.
4. **Some cross-source validation legs** remain unverifiable on JS-rendered
   state sites (classified honestly, not failed) or reflect genuine source-data
   quirks (docket-vs-bill renumbering).
5. **No automated DB backup cron** yet (Railway provides platform snapshots);
   pg_dump procedure documented in `docs/operations/backup-restore.md`.
6. **Single-instance rate limiting** (in-process); horizontal scaling would need
   a shared store.

## 12. DNS records for billcommons.org

Registered at Vercel (Vercel nameservers). Final records:
```
billcommons.org.         ALIAS/A   → Vercel (project-assigned, automatic)
www.billcommons.org.     CNAME     → cname.vercel-dns.com.  (308 → apex)
status.billcommons.org.  CNAME     → cname.vercel-dns.com.  (rewrites to /coverage)
api.billcommons.org.     CNAME     → 4wvaon35.up.railway.app.
mcp.billcommons.org.     CNAME     → 51n0j65s.up.railway.app.
_railway-verify.api.     TXT       → railway-verify=ff1a8e9e…  (ownership)
_railway-verify.mcp.     TXT       → railway-verify=3195329d…  (ownership)
```
API docs live under `api.billcommons.org/docs` (no separate docs subdomain).

## 13. Next five highest-value enhancements

1. **Finish + sustain full-text extraction** (add worker replicas / raise
   politeness-safe concurrency) to drive jurisdictions to GREEN, then keep it
   fresh on the refresh cadence.
2. **Legislator & committee rosters** via the Open States v3 `/people` +
   `/committees` endpoints, wiring `person_id`/`organization_id` FKs so sponsor
   and committee pages become real.
3. **OCR pipeline** for scanned PDFs (currently flagged, not extracted) with a
   confidence indicator surfaced in the UI/API.
4. **Semantic supplement** (pgvector) layered *on top of* — never replacing —
   the deterministic lexical search, powering `find_similar_bills` / model-
   legislation detection across states.
5. **API-key tier + higher limits** and a lightweight usage dashboard, plus an
   automated nightly `pg_dump` to object storage.

---
_Repository: https://github.com/GDACS-droid/billcommons (public, Apache-2.0).
Full ops runbooks in `docs/operations/`, coverage methodology in
`docs/state-coverage/methodology.md`, API examples in `docs/api/examples.md`._
