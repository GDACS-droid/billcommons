import type { Metadata } from "next";
import { notFound } from "next/navigation";
import PageHeader from "@/components/PageHeader";
import { apiGet } from "@/lib/api";

/**
 * The human end of a citation.
 *
 * An agent that builds an evidence packet hands back a `permalink`. If that
 * link resolves to nothing, the packet is a UUID stranded in a transcript and
 * the citation cannot be checked by the person reading the report. This page
 * is what makes the packet quotable: it renders the same payload, computed
 * from the same shared digest, at a URL a human can open.
 *
 * Deliberately NOT indexed. There is one of these per bill, which is the same
 * 200k-page crawl-budget problem that got bill pages removed from the sitemap
 * on 2026-08-02 — and unlike a bill page, nobody arrives here from a search.
 * They arrive from a footnote.
 */

interface Props {
  params: Promise<{ id: string }>;
}

interface EvidencePayload {
  bill_id: string;
  how_to_cite: {
    snapshot_id: string;
    permalink: string;
    download_url: string;
    retrieved_at: string;
    cite_as: string;
    reproducibility: string;
  };
  request: {
    question: string | null;
    jurisdiction_scope: string;
    scope_note: string;
  };
  record: {
    label: string;
    derived_fields: string[];
    derived_note: string;
    data: {
      identifier: string | null;
      title: string | null;
      status: string | null;
      chamber: string | null;
      jurisdiction: string | null;
      jurisdiction_code: string | null;
      session: string | null;
      session_end_date: string | null;
      introduced_date: string | null;
      source_url: string | null;
      enrolled_outcome_uncaptured: boolean;
    };
  };
  counts: {
    actions: number;
    versions: number;
    vote_events: number;
    hearings: number | null;
  };
  hearings_note: string;
  full_packet_note: string;
}

export const metadata: Metadata = {
  title: "Evidence packet",
  robots: { index: false, follow: true },
};

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  if (value === null || value === undefined || value === "") return null;
  return (
    <div className="flex flex-wrap gap-x-2 border-b border-slate-100 py-1.5 text-sm last:border-0">
      <dt className="w-40 shrink-0 text-slate-500">{label}</dt>
      <dd className="min-w-0 flex-1 text-slate-900">{value}</dd>
    </div>
  );
}

export default async function EvidencePage({ params }: Props) {
  const { id } = await params;
  // No revalidate: a citation check must reflect the record now, not a cached
  // copy from an hour ago. The whole value of the page is telling a reader
  // whether what they cited still holds.
  // Full path including /api/v1 -- API_BASE is the origin only, and every
  // other caller passes the version prefix. Omitting it 404s the upstream
  // request, which this page then renders as a missing bill.
  const result = await apiGet<EvidencePayload>(`/api/v1/bills/${id}/evidence`);
  if (!result.ok) {
    if (result.status === 404) notFound();
    return (
      <div className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
        <PageHeader
          eyebrow="Evidence packet"
          title="This packet could not be loaded"
          description={
            <p>
              The record exists, but Bill Commons could not reach it right now.
              This page deliberately does not serve a cached copy — a citation
              check that quietly shows you stale data is worse than one that
              fails.
            </p>
          }
        />
      </div>
    );
  }

  const p = result.data;
  const d = p.record.data;

  return (
    <div className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Evidence packet"
        title={d.identifier ?? "Bill record"}
        description={<p>{d.title}</p>}
      />

      <section className="mt-8 surface-card p-5">
        <h2 className="text-sm font-semibold text-slate-900">Cite as</h2>
        <p className="mt-2 rounded bg-slate-50 p-3 font-mono text-[13px] leading-relaxed text-slate-800">
          {p.how_to_cite.cite_as}
        </p>
        <dl className="mt-4">
          <Field
            label="Snapshot"
            value={<code>{p.how_to_cite.snapshot_id}</code>}
          />
          <Field label="Retrieved" value={p.how_to_cite.retrieved_at} />
          <Field
            label="Download"
            value={
              <a className="underline" href={p.how_to_cite.download_url}>
                JSON
              </a>
            }
          />
        </dl>
        <p className="mt-3 text-xs text-slate-600">
          {p.how_to_cite.reproducibility}
        </p>
      </section>

      <section className="mt-6 rounded-lg border-l-[3px] border-amber-500 bg-amber-50 px-4 py-3">
        <p className="text-sm text-slate-800">
          <strong>{p.record.label}.</strong> {p.record.derived_note}
        </p>
      </section>

      <section className="mt-6 surface-card p-5">
        <h2 className="text-sm font-semibold text-slate-900">The record</h2>
        <dl className="mt-2">
          <Field label="Status" value={d.status?.replace(/_/g, " ")} />
          <Field label="Jurisdiction" value={d.jurisdiction} />
          <Field label="Session" value={d.session} />
          <Field label="Session ended" value={d.session_end_date} />
          <Field label="Chamber" value={d.chamber} />
          <Field label="Introduced" value={d.introduced_date} />
          <Field
            label="Official source"
            value={
              d.source_url ? (
                <a className="break-all underline" href={d.source_url}>
                  {d.source_url}
                </a>
              ) : null
            }
          />
        </dl>
        {d.enrolled_outcome_uncaptured ? (
          <p className="mt-3 text-sm text-amber-900">
            This bill is recorded as enrolled in a session that adjourned long
            ago. The final signature or veto was never captured — do not read
            this as “awaiting executive action”.
          </p>
        ) : null}
      </section>

      <section className="mt-6 surface-card p-5">
        <h2 className="text-sm font-semibold text-slate-900">What is behind it</h2>
        <dl className="mt-2">
          <Field label="Actions" value={p.counts.actions} />
          <Field label="Versions" value={p.counts.versions} />
          <Field label="Vote events" value={p.counts.vote_events} />
          <Field label="Hearings" value={<em>not collected</em>} />
        </dl>
        <p className="mt-3 text-xs text-slate-600">{p.hearings_note}</p>
      </section>

      <section className="mt-6 surface-card p-5">
        <h2 className="text-sm font-semibold text-slate-900">Scope</h2>
        <p className="mt-2 text-sm text-slate-700">
          {p.request.jurisdiction_scope}. {p.request.scope_note}
        </p>
        <p className="mt-3 text-xs text-slate-600">{p.full_packet_note}</p>
      </section>
    </div>
  );
}
