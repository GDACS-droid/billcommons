import Link from "next/link";
import type { Metadata } from "next";
import SearchBox from "@/components/SearchBox";
import DataUnavailable from "@/components/DataUnavailable";
import { apiGet } from "@/lib/api";
import type { CoverageRow, ListEnvelope, Session } from "@/lib/types";

export const metadata: Metadata = {
  alternates: { canonical: "/" },
};

async function getActiveSessions() {
  return apiGet<ListEnvelope<Session>>("/api/v1/sessions", {
    active: "true",
    per_page: 8,
  });
}

async function getCoverageSummary() {
  return apiGet<ListEnvelope<CoverageRow>>("/api/v1/coverage", {
    per_page: 51,
  });
}

export default async function HomePage() {
  const [sessionsResult, coverageResult] = await Promise.all([
    getActiveSessions(),
    getCoverageSummary(),
  ]);

  const coverageRows = coverageResult.ok ? coverageResult.data.data : [];
  const greenCount = coverageRows.filter((r) => r.status === "GREEN").length;
  const totalBills = coverageRows.reduce(
    (sum, r) => sum + (r.bill_count ?? 0),
    0
  );

  return (
    <div>
      <section className="border-b border-slate-200 bg-gradient-to-b from-slate-50 to-white">
        <div className="mx-auto max-w-4xl px-4 py-16 text-center sm:px-6 sm:py-24">
          <h1 className="text-3xl font-semibold tracking-tight text-slate-900 sm:text-5xl">
            Free, open, nonpartisan legislative search
          </h1>
          <p className="mx-auto mt-4 max-w-2xl text-base text-slate-600 sm:text-lg">
            Track bills, sponsors, votes, and hearings across all 50 states
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
            value={coverageRows.length ? `${coverageRows.length} / 51` : "—"}
          />
          <StatCard
            label="Fully verified (GREEN)"
            value={coverageResult.ok ? String(greenCount) : "—"}
          />
          <StatCard
            label="Bills indexed"
            value={coverageResult.ok ? totalBills.toLocaleString() : "—"}
          />
        </div>
      </section>

      <section className="mx-auto max-w-6xl px-4 pb-16 sm:px-6">
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
                  href={`/states/${session.jurisdiction_code ?? ""}/sessions/${encodeURIComponent(
                    session.identifier
                  )}`}
                  className="font-medium text-slate-900 hover:underline"
                >
                  {session.jurisdiction_code} — {session.name}
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
