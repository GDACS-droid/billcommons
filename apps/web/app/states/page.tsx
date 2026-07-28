import Link from "next/link";
import type { Metadata } from "next";
import DataUnavailable from "@/components/DataUnavailable";
import PageHeader from "@/components/PageHeader";
import { fetchAllPages } from "@/lib/collections";
import type { Jurisdiction } from "@/lib/types";

export const metadata: Metadata = {
  title: "States & jurisdictions",
  description:
    "Browse legislative coverage for all 50 states and DC on Bill Commons.",
  alternates: { canonical: "/states" },
};

// There are 51 jurisdictions and the endpoint clamps per_page to 50, so this
// MUST page -- asking for 51 silently returned 50 and dropped a state off the
// directory (and off the crawl path into that state's bills).
async function getJurisdictions() {
  return fetchAllPages<Jurisdiction>("/api/v1/jurisdictions", {}, 1800);
}

export default async function StatesPage() {
  const result = await getJurisdictions();

  return (
    <div className="mx-auto max-w-6xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Directory"
        title="States & jurisdictions"
        description={
          <p>
            All 50 states plus the District of Columbia. Select a jurisdiction
            to see its current legislative session, bills, and coverage status.
          </p>
        }
      />

      <div className="mt-8">
        {!result.ok ? (
          <DataUnavailable message="The jurisdiction directory is temporarily unavailable." />
        ) : result.items.length === 0 ? (
          <p className="text-sm text-slate-600">No jurisdictions found.</p>
        ) : (
          <ul className="grid gap-3 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4">
            {result.items
              .slice()
              .sort((a, b) => a.name.localeCompare(b.name))
              .map((j) => (
                <li
                  key={j.id}
                  className="rounded-md border border-slate-200 bg-white p-4 transition-colors hover:border-slate-300 hover:bg-slate-50"
                >
                  <Link
                    href={`/states/${j.abbreviation}`}
                    className="font-medium text-blue-800 hover:text-blue-700 hover:underline"
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
