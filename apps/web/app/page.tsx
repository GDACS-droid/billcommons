import Link from "next/link";
import type { Metadata } from "next";
import SearchBox from "@/components/SearchBox";
import DataUnavailable from "@/components/DataUnavailable";
import JsonLd from "@/components/JsonLd";
import { apiGet } from "@/lib/api";
import { fetchAllPages } from "@/lib/collections";
import { API_DOCS_URL, MCP_URL, SITE_URL } from "@/lib/config";
import type { CoverageRow, ListEnvelope, Session } from "@/lib/types";

export const metadata: Metadata = {
  alternates: { canonical: "/" },
};

// The most-crawled page on the site; cached so a crawl spike never lands on
// the API as raw query load.
const HOME_REVALIDATE = 900;

async function getActiveSessions() {
  return apiGet<ListEnvelope<Session>>(
    "/api/v1/sessions",
    { active: "true", per_page: 8 },
    { revalidate: HOME_REVALIDATE }
  );
}

// Coverage rows are per (jurisdiction, SESSION) -- 77 of them, not 51 -- and
// per_page clamps at 50. Asking for 51 returned 50 rows and made this page
// advertise 150,276 bills against a real corpus of 209,612. Page properly.
async function getCoverageSummary() {
  return fetchAllPages<CoverageRow>("/api/v1/coverage", {}, HOME_REVALIDATE);
}

export default async function HomePage() {
  const [sessionsResult, coverageResult] = await Promise.all([
    getActiveSessions(),
    getCoverageSummary(),
  ]);

  const coverageRows = coverageResult.items;
  // Every stat below is stated in the unit it is actually measured in: bills
  // are summed over all rows, GREEN is a count of jurisdiction-SESSIONS (a
  // state can have a verified regular session and an unverified special one),
  // and jurisdictions are counted distinctly rather than by row.
  const greenCount = coverageRows.filter((r) => r.status === "GREEN").length;
  const jurisdictionCount = new Set(
    coverageRows.map((r) => r.jurisdiction_code)
  ).size;
  const totalBills = coverageRows.reduce(
    (sum, r) => sum + (r.bill_count ?? 0),
    0
  );

  return (
    <div>
      <JsonLd data={siteJsonLd()} />
      <JsonLd data={datasetJsonLd(totalBills, jurisdictionCount)} />
      <section className="border-b border-slate-200 bg-gradient-to-b from-slate-50 to-white">
        <div className="mx-auto max-w-4xl px-4 py-16 text-center sm:px-6 sm:py-24">
          <h1 className="text-3xl font-semibold tracking-tight text-slate-900 sm:text-5xl">
            Free, open, nonpartisan legislative search
          </h1>
          <p className="mx-auto mt-4 max-w-2xl text-base text-slate-600 sm:text-lg">
            Track bills, sponsors, votes, and full text across all 50 states
            and DC — sourced from official legislative records, with full
            attribution.
          </p>
          <div className="mx-auto mt-8 max-w-2xl">
            <SearchBox autoFocus />
          </div>
          <p className="mt-4 text-sm text-slate-500">
            Try{" "}
            <Link
              href="/search?q=HB+123"
              className="underline underline-offset-2 hover:text-slate-700"
            >
              &ldquo;HB 123&rdquo;
            </Link>{" "}
            or{" "}
            <Link
              href="/search?q=paid+family+leave"
              className="underline underline-offset-2 hover:text-slate-700"
            >
              &ldquo;paid family leave&rdquo;
            </Link>
          </p>
        </div>
      </section>

      <section className="mx-auto max-w-6xl px-4 py-12 sm:px-6">
        <div className="grid gap-6 sm:grid-cols-3">
          <StatCard
            label="Jurisdictions tracked"
            value={jurisdictionCount ? `${jurisdictionCount} / 51` : "—"}
          />
          <StatCard
            label="Sessions fully verified"
            value={
              coverageResult.ok && coverageRows.length
                ? `${greenCount} / ${coverageRows.length}`
                : "—"
            }
          />
          <StatCard
            label="Bills indexed"
            value={coverageResult.ok ? totalBills.toLocaleString() : "—"}
          />
        </div>
      </section>

      <section className="mx-auto max-w-6xl px-4 pb-4 sm:px-6">
        <div className="rounded-lg border border-slate-200 bg-slate-50 px-5 py-4 text-sm text-slate-700">
          <span className="font-semibold text-slate-900">
            Building something with this?
          </span>{" "}
          Every bill, sponsor, action and vote here is available from a free
          public REST API — no key, no licence fee.{" "}
          <Link href="/docs/api" className="underline hover:text-slate-900">
            API docs
          </Link>
          {" · "}
          <Link href="/docs/mcp" className="underline hover:text-slate-900">
            MCP server for AI assistants
          </Link>
          {" · "}
          <a
            href="https://github.com/GDACS-droid/billcommons"
            className="underline hover:text-slate-900"
          >
            Source on GitHub
          </a>
        </div>
      </section>

      <section className="mx-auto max-w-6xl px-4 pb-16 pt-8 sm:px-6">
        <div className="flex items-baseline justify-between">
          <h2 className="text-xl font-semibold text-slate-900">
            Active sessions
          </h2>
          <Link
            href="/states"
            className="text-sm font-medium text-slate-600 hover:underline"
          >
            Browse all states →
          </Link>
        </div>

        {!sessionsResult.ok ? (
          <div className="mt-4">
            <DataUnavailable message="Active-session data is temporarily unavailable." />
          </div>
        ) : sessionsResult.data.data.length === 0 ? (
          <p className="mt-4 text-sm text-slate-600">
            No active sessions reported right now.
          </p>
        ) : (
          <ul className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {sessionsResult.data.data.map((session) => (
              <li
                key={session.id}
                className="rounded-lg border border-slate-200 p-4"
              >
                <Link
                  href={`/states/${
                    session.jurisdiction_abbreviation ?? ""
                  }/sessions/${encodeURIComponent(session.identifier)}`}
                  className="font-medium text-slate-900 hover:underline"
                >
                  {session.jurisdiction_abbreviation ?? ""} —{" "}
                  {session.name ?? session.identifier}
                </Link>
                <p className="mt-1 text-xs text-slate-500">
                  {session.classification ?? "session"}
                  {session.start_date ? ` · started ${session.start_date}` : ""}
                </p>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="border-t border-slate-200 bg-slate-50">
        <div className="mx-auto max-w-6xl px-4 py-12 sm:px-6">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div>
              <h2 className="text-xl font-semibold text-slate-900">
                Coverage dashboard
              </h2>
              <p className="mt-1 max-w-2xl text-sm text-slate-600">
                Every jurisdiction&rsquo;s ingestion status, bill counts, and
                validation results are public — no black boxes.
              </p>
            </div>
            <Link
              href="/coverage"
              className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
            >
              View full coverage matrix
            </Link>
          </div>
        </div>
      </section>
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-6 text-center">
      <p className="text-3xl font-semibold text-slate-900">{value}</p>
      <p className="mt-1 text-sm text-slate-500">{label}</p>
    </div>
  );
}

/**
 * WebSite + SearchAction: tells search engines the site has its own search, and
 * is the entry point that ties the domain to the "Bill Commons" name.
 */
function siteJsonLd() {
  return {
    "@context": "https://schema.org",
    "@type": "WebSite",
    "@id": `${SITE_URL}/#website`,
    url: SITE_URL,
    name: "Bill Commons",
    description:
      "Free, open-source, nonpartisan search over US state legislation — bill text, sponsors, votes and status from official records.",
    inLanguage: "en",
    isAccessibleForFree: true,
    potentialAction: {
      "@type": "SearchAction",
      target: {
        "@type": "EntryPoint",
        urlTemplate: `${SITE_URL}/search?q={search_term_string}`,
      },
      "query-input": "required name=search_term_string",
    },
  };
}

/**
 * Dataset + DataDownload. This is what makes the corpus discoverable as DATA
 * (Google Dataset Search, AI crawlers looking for a machine-readable source)
 * rather than as a pile of web pages, and it advertises the API endpoint that
 * an external reviewer could not find by browsing.
 */
function datasetJsonLd(totalBills: number, jurisdictions: number) {
  return {
    "@context": "https://schema.org",
    "@type": "Dataset",
    "@id": `${SITE_URL}/#dataset`,
    name: "US State Legislation Corpus",
    description:
      `Bills, sponsors, actions, roll-call votes and full text for US state legislatures, ` +
      `covering ${jurisdictions || 51} jurisdictions (50 states and the District of Columbia)` +
      (totalBills ? ` and ${totalBills.toLocaleString("en-US")} bills` : "") +
      ", compiled from official legislative records.",
    url: SITE_URL,
    isAccessibleForFree: true,
    license: "https://github.com/GDACS-droid/billcommons/blob/main/LICENSE",
    creator: { "@type": "Organization", name: "Bill Commons", url: SITE_URL },
    keywords: [
      "state legislation",
      "bill tracking",
      "roll call votes",
      "legislative records",
      "open government data",
    ],
    spatialCoverage: { "@type": "Place", name: "United States" },
    distribution: [
      {
        "@type": "DataDownload",
        encodingFormat: "application/json",
        contentUrl: `${API_DOCS_URL}/api/v1/bills`,
        description: "Public REST API. No key required.",
      },
      {
        "@type": "DataDownload",
        encodingFormat: "application/json",
        contentUrl: `${API_DOCS_URL}/openapi.json`,
        description: "OpenAPI schema.",
      },
      {
        "@type": "DataDownload",
        encodingFormat: "application/json",
        contentUrl: MCP_URL,
        description: "Model Context Protocol server for AI assistants.",
      },
    ],
  };
}
