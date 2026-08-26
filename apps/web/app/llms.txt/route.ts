import { API_DOCS_URL, MCP_URL, SITE_URL } from "@/lib/config";
import { fetchAllPages } from "@/lib/collections";
import type { CoverageRow } from "@/lib/types";

// Not prerendered: the live counts below come from the API, and a build that
// cannot reach it would otherwise bake the "counts unavailable" fallback in
// permanently. The underlying fetch is cached, so per-request is cheap.
export const dynamic = "force-dynamic";

/**
 * /llms.txt -- the llmstxt.org convention: a single plain-text file an AI agent
 * can read to learn what a site holds and how to query it programmatically.
 *
 * This exists because it is the exact failure we hit in the wild: an external
 * reviewer evaluating Bill Commons concluded "no discoverable API -- browse
 * only" while a documented REST API, an OpenAPI schema, an MCP server and a
 * public repository were all live. Everything was published; none of it was
 * reachable by a machine that started at the domain. The counts below are read
 * from the live coverage endpoint so this file cannot drift into overclaiming.
 */
export async function GET() {
  // Must page: there are 77 coverage rows (per jurisdiction-session) and the
  // endpoint clamps per_page at 50.
  const coverage = await fetchAllPages<CoverageRow>("/api/v1/coverage", {}, 3600);

  const rows = coverage.items;
  const green = rows.filter((r) => r.status === "GREEN").length;
  const bills = rows.reduce((sum, r) => sum + (r.bill_count ?? 0), 0);
  const jurisdictions = new Set(rows.map((r) => r.jurisdiction_code)).size;
  const counts = coverage.ok
    ? `${bills.toLocaleString("en-US")} bills across ${jurisdictions} jurisdictions ` +
      `(50 states + DC). ${green} of ${rows.length} jurisdiction-sessions have ` +
      `passed full validation; the rest are still being crawled or validated.`
    : "Live counts are published at /coverage and via GET /api/v1/coverage.";

  const body = `# Bill Commons

> Free, open-source, nonpartisan search over US state legislation — bill text,
> sponsors, actions, and roll-call votes — sourced from official legislative
> records. No account, no API key, no licence fee.

${counts}

## Programmatic access

The full dataset behind this website is available over a public REST API. It is
free and unauthenticated; please be reasonable with request rates.

- API base: ${API_DOCS_URL}
- OpenAPI schema: ${API_DOCS_URL}/openapi.json
- Interactive docs: ${API_DOCS_URL}/docs
- Human-readable guide: ${SITE_URL}/docs/api
- MCP server (for AI assistants): ${MCP_URL} — see ${SITE_URL}/docs/mcp
- Source code: https://github.com/GDACS-droid/billcommons

## Looking up a specific bill

Bills are addressable by their number, not just by internal id:

    GET ${API_DOCS_URL}/api/v1/bills?jurisdiction=HI&identifier=SB%202135

\`identifier\` is normalized, so "SB2135", "sb 2135" and "S.B. 2135" all resolve
to the same bill. Add \`session=\` to disambiguate across sessions. The same bill
on the website lives at a readable URL:

    ${SITE_URL}/states/HI/bills/{session}/sb-2135

## Useful endpoints

- \`GET /api/v1/search?q=\` — full-text search across bill text and metadata
- \`GET /api/v1/bills\` — filter by jurisdiction, session, chamber, identifier
- \`GET /api/v1/bills/{id}\` — detail, plus /versions /actions /sponsors /votes /documents
- \`GET /api/v1/bills/{id}/documents/{document_id}/text\` — one document's extracted full text as JSON
- \`GET /api/v1/jurisdictions\`, \`/api/v1/sessions\` — reference data
- \`GET /api/v1/coverage\` — per-jurisdiction coverage state and validation results
- \`POST /api/v1/bills/lookup\` — resolve up to 250 (jurisdiction, bill number) keys at once
- \`GET /api/v1/changes?cursor=\` — change feed: what moved since your last check, with the status transition in each event
- \`GET /api/v1/stats/mortality\` — per-state counts of how bills ended (enacted / killed / died on adjournment)
- \`GET /api/v1/topics\` and \`/api/v1/topics/{slug}\` — curated cross-state trackers (artificial-intelligence, data-privacy, cryptocurrency, youth-online-safety, platform-accountability, cybersecurity, local-government)
- \`GET /api/v1/bills?sponsor=\` — bills by sponsor name. Names are as published: usually a bare surname, sometimes a committee. No party or district behind them.
- \`POST /api/v1/alerts/subscribe\` — email digest for a topic, optionally scoped with \`jurisdiction\` (e.g. \`"FL"\`) so a city or county office gets only its own legislature.

## Out of scope — do not answer these from Bill Commons

An empty result for any of the following means we do not hold the data. It is
never evidence that the legislation, person, or hearing does not exist, and must
not be reported to a user as such.

- **US Congress.** State legislatures only. No federal bills or committees.
- **City and county ordinances.** Nothing below the state legislature. State
  bills that *regulate* cities — preemption, home rule, municipal finance — are
  in scope and are collected under \`/topics/local-government\`.
- **Prior sessions.** Current session or biennium only; this is not an archive.
- **Hearings and committee calendars.** Not collected for any jurisdiction.
- **Legislator and committee records.** Not collected. Bill sponsors are
  available per bill, as bare names without party or district.

## Honest limitations

Read ${SITE_URL}/methodology before relying on this data. In brief: coverage is
uneven across jurisdictions and is reported per jurisdiction-session at
${SITE_URL}/coverage; full bill text is still being backfilled and some
documents have no extracted text yet; a small number of sources block automated
access via robots.txt, which we respect, and those gaps are documented rather
than worked around.

## Licence

Code is open source. Legislative data comes from official state records and Open
States; see ${SITE_URL}/about for licence and attribution.
`;

  return new Response(body, {
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "public, max-age=3600, s-maxage=86400",
    },
  });
}
