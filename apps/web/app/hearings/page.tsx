import Link from "next/link";
import type { Metadata } from "next";
import DataUnavailable from "@/components/DataUnavailable";
import PaginationNav from "@/components/PaginationNav";
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
    <div className="mx-auto max-w-4xl px-4 py-10 sm:px-6">
      <h1 className="text-2xl font-semibold text-slate-900">
        Upcoming hearings
      </h1>
      <p className="mt-2 text-sm text-slate-600">
        Committee hearings and legislative events, sourced from official
        calendars where available.
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
                  className="rounded-lg border border-slate-200 p-4"
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
                        className="rounded-full border border-slate-300 px-2 py-0.5 hover:bg-slate-50"
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
