import type { Metadata } from "next";
import DataUnavailable from "@/components/DataUnavailable";
import PageHeader from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import type { Committee } from "@/lib/types";

interface Props {
  params: Promise<{ id: string }>;
}

// GET /committees/{id} returns the bare CommitteeOut object -- no {data,
// meta} envelope (unlike list endpoints). See
// apps/api/billcommons_api/routers/committees.py.
async function getCommittee(id: string) {
  return apiGet<Committee>(`/api/v1/committees/${id}`);
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { id } = await params;
  const result = await getCommittee(id);
  const name = result.ok ? result.data.name : "Committee";
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
      <div className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
        <DataUnavailable message="This committee's record is temporarily unavailable." />
      </div>
    );
  }

  const committee = result.data;

  return (
    <div className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Committee"
        title={committee.name}
        description={
          committee.classification ? <p>{committee.classification}</p> : undefined
        }
      />

      <section className="border-t border-slate-200 pt-8">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
          Members
        </h2>
        <p className="mt-2 text-sm text-slate-500">
          Committee membership is not available yet.
        </p>
      </section>

      <section className="mt-10 border-t border-slate-200 pt-8">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
          Bills before this committee
        </h2>
        <p className="mt-2 text-sm text-slate-500">
          Bills-by-committee is not available yet.
        </p>
      </section>
    </div>
  );
}
