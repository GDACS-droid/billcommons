import Link from "next/link";
import type { Metadata } from "next";
import DataUnavailable from "@/components/DataUnavailable";
import PaginationNav from "@/components/PaginationNav";
import PageHeader from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import type { HearingEvent, ListEnvelope } from "@/lib/types";

export const metadata: Metadata = {
  title: "Upcoming hearings",
  description:
    "Upcoming legislative committee hearings across all 50 states and DC.",
  alternates: { canonical: "/hearings" },
};

interface Props {
  searchParams: Promise<{ page?: string; jurisdiction?: string }>;
}

async function getHearings(page: number, jurisdiction?: string) {
  return apiGet<ListEnvelope<HearingEvent>>("/api/v1/events", {
    jurisdiction,
    page,
    per_page: 25,
  });
}

export default async function HearingsPage({ searchParams }: Props) {
  const sp = await searchParams;
  const page = Number(sp.page ?? "1") || 1;
  const result = await getHearings(page, sp.jurisdiction);

  return (
    <div className="mx-auto max-w-4xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Calendar"
        title="Upcoming hearings"
        description={
          <p>
        Committee hearings and legislative events, sourced from official
        calendars where available.
          </p>
        }
      />
      {/* Said plainly rather than shown as an empty list: no calendar source is
          ingested yet, so this page holds zero events. An empty table with no
          explanation reads as "no hearings scheduled anywhere", which is a
          much stronger and completely false claim. */}
      <p className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
        <strong>Not yet populated.</strong> Hearing and committee-calendar
        ingestion is not built yet, so this page is empty — that is a gap in
        Bill Commons, not an absence of scheduled hearings. Bill action
        timelines, sponsors, votes and full text are unaffected; see{" "}
        <Link href="/methodology" className="underline">
          methodology
        </Link>{" "}
        for the current limitations.
      </p>

      <div className="mt-8">
        {!result.ok ? (
          <DataUnavailable message="Hearing data is temporarily unavailable." />
        ) : result.data.data.length === 0 ? (
          <p className="text-sm text-slate-600">
            No upcoming hearings on record.
          </p>
        ) : (
          <>
            <ul className="space-y-3">
              {result.data.data.map((h) => (
                <li
                  key={h.id}
                  className="rounded-md border border-slate-200 bg-white p-4 transition-colors hover:border-slate-300 hover:bg-slate-50"
                >
                  <p className="font-medium text-slate-900">{h.name}</p>
                  <p className="mt-1 text-sm text-slate-600">
                    {h.start_date ?? "Date not provided"}
                    {h.jurisdiction_abbreviation ? ` · ${h.jurisdiction_abbreviation}` : ""}
                    {h.location ? ` · ${h.location}` : ""}
                  </p>
                  {h.bill_id ? (
                    <p className="mt-2 text-xs">
                      <Link
                        href={`/bills/${h.bill_id}`}
                        className="rounded-md border border-slate-300 px-2 py-0.5 text-blue-800 transition-colors hover:border-slate-400 hover:bg-slate-50"
                      >
                        View related bill
                      </Link>
                    </p>
                  ) : null}
                </li>
              ))}
            </ul>
            <PaginationNav
              pagination={result.data.pagination}
              basePath="/hearings"
              searchParams={{ jurisdiction: sp.jurisdiction }}
            />
          </>
        )}
      </div>
    </div>
  );
}
