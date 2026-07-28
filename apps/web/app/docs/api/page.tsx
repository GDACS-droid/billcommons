import type { Metadata } from "next";
import Link from "next/link";
import { API_DOCS_URL } from "@/lib/config";
import PageHeader from "@/components/PageHeader";

export const metadata: Metadata = {
  title: "API documentation",
  description:
    "REST API reference for Bill Commons: endpoints, authentication, rate limits, and code examples in curl, Python, and JavaScript.",
  alternates: { canonical: "/docs/api" },
};

function CodeBlock({ children, label }: { children: string; label?: string }) {
  return (
    <div className="code-block mt-4">
      {label ? (
        <div className="border-b border-slate-700 bg-slate-800 px-4 py-1.5 text-xs font-medium text-slate-400">
          {label}
        </div>
      ) : null}
      <pre>
        <code>{children}</code>
      </pre>
    </div>
  );
}

const ENDPOINTS: { method: string; path: string; description: string }[] = [
  { method: "GET", path: "/api/v1/jurisdictions", description: "List all 51 jurisdictions." },
  { method: "GET", path: "/api/v1/jurisdictions/{id}", description: "Get one jurisdiction." },
  { method: "GET", path: "/api/v1/sessions", description: "List legislative sessions (filter by jurisdiction, active)." },
  { method: "GET", path: "/api/v1/bills", description: "List bills (filter by jurisdiction, session, chamber, status, sponsor, subject, committee, date range)." },
  { method: "GET", path: "/api/v1/bills/{id}", description: "Get a bill with full detail (sponsors, actions, versions, votes, sources)." },
  { method: "GET", path: "/api/v1/bills/{id}/versions", description: "List a bill's text versions." },
  { method: "GET", path: "/api/v1/bills/{id}/actions", description: "List a bill's full action history." },
  { method: "GET", path: "/api/v1/bills/{id}/sponsors", description: "List a bill's sponsors and cosponsors." },
  { method: "GET", path: "/api/v1/bills/{id}/votes", description: "List recorded votes, including member-level detail." },
  { method: "GET", path: "/api/v1/bills/{id}/documents", description: "List associated documents (fiscal notes, amendments, etc)." },
  { method: "GET", path: "/api/v1/people", description: "List legislators." },
  { method: "GET", path: "/api/v1/people/{id}", description: "Get a legislator and their sponsored bills." },
  { method: "GET", path: "/api/v1/committees", description: "List committees." },
  { method: "GET", path: "/api/v1/committees/{id}", description: "Get a committee, its members, and pending bills." },
  { method: "GET", path: "/api/v1/events", description: "List hearings and legislative calendar events." },
  { method: "GET", path: "/api/v1/search", description: "Full-text and structured search across all jurisdictions." },
  { method: "GET", path: "/api/v1/sources", description: "List source registry entries per jurisdiction." },
  { method: "GET", path: "/api/v1/coverage", description: "Public per-jurisdiction coverage matrix." },
  { method: "GET", path: "/api/v1/health", description: "Liveness check (DB ping)." },
  { method: "GET", path: "/api/v1/ready", description: "Readiness check." },
];

export default function ApiDocsPage() {
  return (
    <div className="mx-auto max-w-4xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Developer access"
        title="REST API documentation"
        description={
          <p>
        The Bill Commons API is free, public, and read-only. It serves
        JSON over HTTPS with a consistent pagination envelope, ETags, and
        request IDs. Interactive OpenAPI 3.1 docs are available at{" "}
        <a
          className="underline"
          href={`${API_DOCS_URL}/docs`}
        >
          {API_DOCS_URL}/docs
        </a>
        .
          </p>
        }
      />

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-900">Base URL</h2>
        <CodeBlock>{API_DOCS_URL}</CodeBlock>
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-900">
          Authentication &amp; rate limits
        </h2>
        <p className="mt-2 text-sm text-slate-600">
          No API key is required for the public anonymous tier: 60
          requests/minute per IP. API keys for higher-volume tiers are
          planned; see the{" "}
          <Link href="/about" className="underline">
            about page
          </Link>{" "}
          for status.
        </p>
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-900">
          Pagination envelope
        </h2>
        <p className="mt-2 text-sm text-slate-600">
          Every list endpoint returns the same shape:
        </p>
        <CodeBlock label="Response shape">{`{
  "data": [ ... ],
  "pagination": {
    "page": 1,
    "per_page": 20,
    "total": 431,
    "total_pages": 22
  },
  "meta": {
    "source_freshness": "2026-07-23T14:02:00Z",
    "api_version": "v1",
    "request_id": "a1b2c3d4"
  }
}`}</CodeBlock>
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-900">Endpoints</h2>
        <div className="surface-card mt-4 overflow-x-auto">
          <table className="data-table min-w-[640px]">
            <thead>
              <tr>
                <th>Method</th>
                <th>Path</th>
                <th>Description</th>
              </tr>
            </thead>
            <tbody>
              {ENDPOINTS.map((e) => (
                <tr key={e.path}>
                  <td className="font-mono text-xs text-slate-500">
                    {e.method}
                  </td>
                  <td className="font-mono text-xs text-slate-800">
                    {e.path}
                  </td>
                  <td className="text-slate-600">
                    {e.description}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-900">
          Example: search for a bill
        </h2>

        <CodeBlock label="curl">{`curl "${API_DOCS_URL}/api/v1/search?q=HB+123&jurisdiction=NC"`}</CodeBlock>

        <CodeBlock label="Python">{`import requests

resp = requests.get(
    "${API_DOCS_URL}/api/v1/search",
    params={"q": "HB 123", "jurisdiction": "NC"},
)
resp.raise_for_status()
data = resp.json()
for bill in data["data"]:
    print(bill["identifier"], bill["title"])`}</CodeBlock>

        <CodeBlock label="JavaScript">{`const url = new URL("${API_DOCS_URL}/api/v1/search");
url.searchParams.set("q", "HB 123");
url.searchParams.set("jurisdiction", "NC");

const res = await fetch(url);
const { data } = await res.json();
for (const bill of data) {
  console.log(bill.identifier, bill.title);
}`}</CodeBlock>
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-900">
          Example: get a bill&apos;s full record
        </h2>
        <CodeBlock label="curl">{`curl "${API_DOCS_URL}/api/v1/bills/{bill_id}"`}</CodeBlock>
      </section>

      <p className="mt-8 text-xs text-slate-500">
        All bill and document text is sourced from official legislative
        records or Open States and is provided as-is; see{" "}
        <Link href="/methodology" className="underline">
          methodology
        </Link>{" "}
        for attribution and known limitations.
      </p>
    </div>
  );
}
