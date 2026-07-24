import Link from "next/link";
import type { Metadata } from "next";
import DataUnavailable from "@/components/DataUnavailable";
import { apiGet } from "@/lib/api";
import type { Jurisdiction, ListEnvelope } from "@/lib/types";

export const metadata: Metadata = {
  title: "States & jurisdictions",
  description:
    "Browse legislative coverage for all 50 states and DC on Bill Commons.",
  alternates: { canonical: "/states" },
};

async function getJurisdictions() {
  return apiGet<ListEnvelope<Jurisdiction>>("/api/v1/jurisdictions", {
    per_page: 51,
  });
}

export default async function StatesPage() {
  const result = await getJurisdictions();

  return (
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
      <h1 className="text-2xl font-semibold text-slate-900">
        States &amp; jurisdictions
      </h1>
      <p className="mt-2 max-w-2xl text-sm text-slate-600">
        All 50 states plus the District of Columbia. Select a jurisdiction to
        see its current legislative session, bills, and coverage status.
      </p>

      <div className="mt-8">
        {!result.ok ? (
          <DataUnavailable message="The jurisdiction directory is temporarily unavailable." />
        ) : result.data.data.length === 0 ? (
          <p className="text-sm text-slate-600">No jurisdictions found.</p>
        ) : (
          <ul className="grid gap-3 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4">
            {result.data.data
              .slice()
              .sort((a, b) => a.name.localeCompare(b.name))
              .map((j) => (
                <li
                  key={j.id}
                  className="rounded-lg border border-slate-200 p-4"
                >
                  <Link
                    href={`/states/${j.abbreviation}`}
                    className="font-medium text-slate-900 hover:underline"
                  >
                    {j.name}
                  </Link>
                </li>
              ))}
          </ul>
        )}
      </div>
    </div>
  );
}
