import Link from "next/link";
import type { Metadata } from "next";
import DataUnavailable from "@/components/DataUnavailable";
import BillListItem from "@/components/BillListItem";
import { apiGet } from "@/lib/api";
import type { Person } from "@/lib/types";

interface Props {
  params: Promise<{ id: string }>;
}

async function getPerson(id: string) {
  return apiGet<{ data: Person }>(`/api/v1/people/${id}`);
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { id } = await params;
  const result = await getPerson(id);
  const name = result.ok ? result.data.data.name : "Legislator";
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
      <div className="mx-auto max-w-3xl px-4 py-10 sm:px-6">
        <DataUnavailable message="This legislator's record is temporarily unavailable." />
      </div>
    );
  }

  const person = result.data.data;

  return (
    <div className="mx-auto max-w-3xl px-4 py-10 sm:px-6">
      {person.jurisdiction_code ? (
        <nav aria-label="Breadcrumb" className="text-sm text-slate-500">
          <Link
            href={`/states/${person.jurisdiction_code}`}
            className="hover:underline"
          >
            {person.jurisdiction_code}
          </Link>{" "}
          / {person.name}
        </nav>
      ) : null}

      <h1 className="mt-2 text-2xl font-semibold text-slate-900">
        {person.name}
      </h1>
      <p className="mt-1 text-sm text-slate-600">
        {person.party ? `${person.party} · ` : ""}
        {person.chamber ?? ""}
        {person.district ? ` · District ${person.district}` : ""}
        {person.current === false ? " · former member" : ""}
      </p>

      <section className="mt-8">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
          Sponsored bills
        </h2>
        {!person.sponsored_bills || person.sponsored_bills.length === 0 ? (
          <p className="mt-2 text-sm text-slate-600">
            No sponsored bills on record.
          </p>
        ) : (
          <ul className="mt-3 space-y-3">
            {person.sponsored_bills.map((bill) => (
              <BillListItem key={bill.id} bill={bill} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
