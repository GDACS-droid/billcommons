import Link from "next/link";
import type { Metadata } from "next";
import DataUnavailable from "@/components/DataUnavailable";
import BillListItem from "@/components/BillListItem";
import { apiGet } from "@/lib/api";
import type { Committee } from "@/lib/types";

interface Props {
  params: Promise<{ id: string }>;
}

async function getCommittee(id: string) {
  return apiGet<{ data: Committee }>(`/api/v1/committees/${id}`);
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { id } = await params;
  const result = await getCommittee(id);
  const name = result.ok ? result.data.data.name : "Committee";
  return {
    title: name,
    alternates: { canonical: `/committees/${id}` },
  };
}

export default async function CommitteePage({ params }: Props) {
  const { id } = await params;
  const result = await getCommittee(id);

  if (!result.ok) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-10 sm:px-6">
        <DataUnavailable message="This committee's record is temporarily unavailable." />
      </div>
    );
  }

  const committee = result.data.data;

  return (
    <div className="mx-auto max-w-3xl px-4 py-10 sm:px-6">
      {committee.jurisdiction_code ? (
        <nav aria-label="Breadcrumb" className="text-sm text-slate-500">
          <Link
            href={`/states/${committee.jurisdiction_code}`}
            className="hover:underline"
          >
            {committee.jurisdiction_code}
          </Link>{" "}
          / {committee.name}
        </nav>
      ) : null}

      <h1 className="mt-2 text-2xl font-semibold text-slate-900">
        {committee.name}
      </h1>
      {committee.chamber ? (
        <p className="mt-1 text-sm text-slate-600">{committee.chamber}</p>
      ) : null}

      {committee.members?.length ? (
        <section className="mt-8">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
            Members
          </h2>
          <ul className="mt-3 grid gap-2 sm:grid-cols-2">
            {committee.members.map((m) => (
              <li
                key={m.person_id}
                className="rounded-md border border-slate-200 px-3 py-2 text-sm"
              >
                <Link
                  href={`/people/${m.person_id}`}
                  className="font-medium hover:underline"
                >
                  {m.name}
                </Link>
                {m.role ? (
                  <span className="ml-2 text-xs text-slate-500">
                    {m.role}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className="mt-8">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
          Bills before this committee
        </h2>
        {!committee.bills || committee.bills.length === 0 ? (
          <p className="mt-2 text-sm text-slate-600">
            No bills on record for this committee.
          </p>
        ) : (
          <ul className="mt-3 space-y-3">
            {committee.bills.map((bill) => (
              <BillListItem key={bill.id} bill={bill} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
