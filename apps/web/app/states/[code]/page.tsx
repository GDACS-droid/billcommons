import Link from "next/link";
import type { Metadata } from "next";
import DataUnavailable from "@/components/DataUnavailable";
import BillListItem from "@/components/BillListItem";
import { CoverageBadge } from "@/components/StatusBadge";
import { apiGet } from "@/lib/api";
import { billPath, slugify } from "@/lib/billUrl";
import type {
  BillSummary,
  CoverageRow,
  Jurisdiction,
  ListEnvelope,
  Session,
} from "@/lib/types";

interface Props {
  params: Promise<{ code: string }>;
}

// This page is in the sitemap and is a hub a crawler follows into 200k bill
// pages, so its reads are cached rather than no-store.
const STATE_REVALIDATE = 1800;

async function getJurisdiction(code: string) {
  return apiGet<Jurisdiction>(`/api/v1/jurisdictions/${code}`, undefined, {
    revalidate: STATE_REVALIDATE,
  });
}

async function getSessions(code: string) {
  return apiGet<ListEnvelope<Session>>(
    "/api/v1/sessions",
    { jurisdiction: code, per_page: 20 },
    { revalidate: STATE_REVALIDATE }
  );
}

async function getRecentBills(code: string) {
  return apiGet<ListEnvelope<BillSummary>>(
    "/api/v1/bills",
    { jurisdiction: code, sort: "latest_action", per_page: 10 },
    { revalidate: STATE_REVALIDATE }
  );
}

async function getCoverage(code: string) {
  return apiGet<ListEnvelope<CoverageRow>>(
    "/api/v1/coverage",
    { jurisdiction: code, per_page: 1 },
    { revalidate: STATE_REVALIDATE }
  );
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { code } = await params;
  const result = await getJurisdiction(code);
  const name = result.ok ? result.data.name : code.toUpperCase();
  return {
    title: `${name} legislation`,
    description: `Bills, sessions, and legislative coverage for ${name} on Bill Commons.`,
    alternates: { canonical: `/states/${code.toUpperCase()}` },
  };
}

export default async function StatePage({ params }: Props) {
  const { code } = await params;
  const upperCode = code.toUpperCase();

  const [jurisdictionResult, sessionsResult, billsResult, coverageResult] =
    await Promise.all([
      getJurisdiction(upperCode),
      getSessions(upperCode),
      getRecentBills(upperCode),
      getCoverage(upperCode),
    ]);

  // Bills here span every session in the state, so map session_id -> slug once
  // and link each row at its canonical URL instead of routing through /bills/<uuid>.
  const sessionSlugById = new Map<string, string>(
    sessionsResult.ok
      ? sessionsResult.data.data.map((s) => [s.id, slugify(s.identifier)])
      : []
  );

  const coverageStatus = coverageResult.ok
    ? coverageResult.data.data[0]?.status
    : undefined;

  return (
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
      <nav aria-label="Breadcrumb" className="text-sm text-slate-500">
        <Link href="/states" className="hover:underline">
          States
        </Link>{" "}
        / {upperCode}
      </nav>

      <div className="mt-2 flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-semibold text-slate-900">
          {jurisdictionResult.ok ? jurisdictionResult.data.name : upperCode}
        </h1>
        {coverageStatus ? <CoverageBadge status={coverageStatus} /> : null}
      </div>

      {!jurisdictionResult.ok ? (
        <div className="mt-6">
          <DataUnavailable message="Jurisdiction details are temporarily unavailable." />
        </div>
      ) : null}

      <section className="mt-8">
        <h2 className="text-lg font-semibold text-slate-900">Sessions</h2>
        {!sessionsResult.ok ? (
          <div className="mt-3">
            <DataUnavailable message="Session data is temporarily unavailable." />
          </div>
        ) : sessionsResult.data.data.length === 0 ? (
          <p className="mt-3 text-sm text-slate-600">
            No sessions on record for {upperCode} yet.
          </p>
        ) : (
          <ul className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {sessionsResult.data.data.map((session) => (
              <li
                key={session.id}
                className="rounded-lg border border-slate-200 p-4"
              >
                <Link
                  href={`/states/${upperCode}/sessions/${encodeURIComponent(
                    session.identifier
                  )}`}
                  className="font-medium text-slate-900 hover:underline"
                >
                  {session.name ?? session.identifier}
                </Link>
                <p className="mt-1 text-xs text-slate-500">
                  {session.classification ?? "session"}
                  {session.active ? " · active" : ""}
                </p>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-900">
          Recent bill activity
        </h2>
        {!billsResult.ok ? (
          <div className="mt-3">
            <DataUnavailable message="Bill data is temporarily unavailable." />
          </div>
        ) : billsResult.data.data.length === 0 ? (
          <p className="mt-3 text-sm text-slate-600">
            No bills on record for {upperCode} yet.
          </p>
        ) : (
          <ul className="mt-3 space-y-3">
            {billsResult.data.data.map((bill) => (
              <BillListItem
                key={bill.id}
                bill={bill}
                href={
                  sessionSlugById.get(bill.session_id)
                    ? billPath(
                        upperCode,
                        sessionSlugById.get(bill.session_id)!,
                        bill.identifier_norm
                      )
                    : undefined
                }
              />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
