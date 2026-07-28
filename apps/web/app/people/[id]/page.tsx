import type { Metadata } from "next";
import DataUnavailable from "@/components/DataUnavailable";
import BillListItem from "@/components/BillListItem";
import PageHeader from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import type { BillSummary, ListEnvelope, Person } from "@/lib/types";

interface Props {
  params: Promise<{ id: string }>;
}

// GET /people/{id} returns the bare PersonDetail object -- no {data, meta}
// envelope (unlike list endpoints). See
// apps/api/billcommons_api/routers/people.py.
async function getPerson(id: string) {
  return apiGet<Person>(`/api/v1/people/${id}`);
}

// There's no /people/{id}/sponsored-bills sub-resource yet; the closest real
// signal is /search's `sponsor` filter, which does an ILIKE substring match
// on the sponsorship name (not a person_id join) -- so results here are a
// best-effort, name-based approximation, not a precise sponsorship record.
async function getSponsoredBills(name: string) {
  return apiGet<ListEnvelope<BillSummary>>("/api/v1/search", {
    sponsor: name,
    per_page: 20,
  });
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { id } = await params;
  const result = await getPerson(id);
  const name = result.ok ? result.data.name : "Legislator";
  return {
    title: name,
    alternates: { canonical: `/people/${id}` },
  };
}

export default async function PersonPage({ params }: Props) {
  const { id } = await params;
  const result = await getPerson(id);

  if (!result.ok) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
        <DataUnavailable message="This legislator's record is temporarily unavailable." />
      </div>
    );
  }

  const person = result.data;
  const sponsoredBills = await getSponsoredBills(person.name);

  return (
    <div className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Legislator"
        title={person.name}
        description={person.party ? <p>{person.party}</p> : undefined}
      />

      <section className="border-t border-slate-200 pt-8">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
          Bills sponsored by this name
        </h2>
        <p className="mt-1 text-xs text-slate-400">
          Matched by sponsor name, not a verified sponsorship record — a
          precise per-person sponsorship link is not available yet.
        </p>
        {!sponsoredBills.ok ? (
          <DataUnavailable message="Sponsored-bill data is temporarily unavailable." />
        ) : sponsoredBills.data.data.length === 0 ? (
          <p className="mt-2 text-sm text-slate-600">
            No matching sponsored bills found.
          </p>
        ) : (
          <ul className="mt-4 space-y-3">
            {sponsoredBills.data.data.map((bill) => (
              <BillListItem key={bill.id} bill={bill} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
