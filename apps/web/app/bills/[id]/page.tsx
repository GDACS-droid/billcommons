import Link from "next/link";
import type { Metadata } from "next";
import DataUnavailable from "@/components/DataUnavailable";
import { BillStatusBadge } from "@/components/StatusBadge";
import { apiGet } from "@/lib/api";
import type { Bill } from "@/lib/types";

interface Props {
  params: Promise<{ id: string }>;
}

async function getBill(id: string) {
  return apiGet<{ data: Bill; meta: unknown }>(`/api/v1/bills/${id}`);
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { id } = await params;
  const result = await getBill(id);
  if (!result.ok) {
    return { title: "Bill" };
  }
  const bill = result.data.data;
  const title = `${bill.identifier} — ${bill.title}`;
  return {
    title,
    description: bill.description ?? bill.title,
    alternates: { canonical: `/bills/${id}` },
  };
}

export default async function BillPage({ params }: Props) {
  const { id } = await params;
  const result = await getBill(id);

  if (!result.ok) {
    return (
      <div className="mx-auto max-w-4xl px-4 py-10 sm:px-6">
        <DataUnavailable message="This bill is temporarily unavailable." />
      </div>
    );
  }

  const bill = result.data.data;

  return (
    <div className="mx-auto max-w-4xl px-4 py-10 sm:px-6">
      <nav aria-label="Breadcrumb" className="text-sm text-slate-500">
        <Link href="/states" className="hover:underline">
          States
        </Link>{" "}
        {bill.jurisdiction_code ? (
          <>
            /{" "}
            <Link
              href={`/states/${bill.jurisdiction_code}`}
              className="hover:underline"
            >
              {bill.jurisdiction_code}
            </Link>{" "}
          </>
        ) : null}
        / {bill.identifier}
      </nav>

      <header className="mt-2">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold text-slate-900">
            {bill.identifier}
          </h1>
          <BillStatusBadge status={bill.status} />
        </div>
        <p className="mt-2 text-lg text-slate-800">{bill.title}</p>
        {bill.short_title && bill.short_title !== bill.title ? (
          <p className="mt-1 text-sm text-slate-500">
            Also known as: {bill.short_title}
          </p>
        ) : null}
        <p className="mt-2 text-sm text-slate-500">
          {bill.jurisdiction_name ?? bill.jurisdiction_code}
          {bill.session_identifier ? ` · ${bill.session_identifier}` : ""}
          {bill.chamber ? ` · ${bill.chamber}` : ""}
        </p>
      </header>

      {bill.description ? (
        <section className="mt-6">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
            Description
          </h2>
          <p className="mt-2 text-slate-800">{bill.description}</p>
        </section>
      ) : null}

      <dl className="mt-6 grid grid-cols-2 gap-4 rounded-lg border border-slate-200 p-4 text-sm sm:grid-cols-3">
        <Field label="Introduced" value={bill.introduced_date} />
        <Field label="Status date" value={bill.status_date} />
        <Field
          label="Latest action"
          value={
            bill.latest_action_date
              ? `${bill.latest_action_date}${
                  bill.latest_action_description
                    ? ` — ${bill.latest_action_description}`
                    : ""
                }`
              : null
          }
        />
        <Field
          label="Subjects"
          value={bill.subjects?.length ? bill.subjects.join(", ") : null}
        />
        <Field label="Bill type" value={bill.bill_type} />
        <Field label="Last updated" value={bill.updated_at} />
      </dl>

      {bill.sponsors?.length ? (
        <Section title="Sponsors">
          <ul className="grid gap-2 sm:grid-cols-2">
            {bill.sponsors.map((s) => (
              <li
                key={s.id}
                className="rounded-md border border-slate-200 px-3 py-2 text-sm"
              >
                {s.person_id ? (
                  <Link href={`/people/${s.person_id}`} className="font-medium hover:underline">
                    {s.name}
                  </Link>
                ) : (
                  <span className="font-medium">{s.name}</span>
                )}
                <span className="ml-2 text-xs text-slate-500">
                  {s.classification ?? "sponsor"}
                  {s.party ? ` · ${s.party}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      {bill.committees?.length ? (
        <Section title="Committees">
          <ul className="flex flex-wrap gap-2">
            {bill.committees.map((c) => (
              <li key={c.id}>
                <Link
                  href={`/committees/${c.id}`}
                  className="rounded-full border border-slate-300 px-3 py-1 text-sm hover:bg-slate-50"
                >
                  {c.name}
                </Link>
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      {bill.actions?.length ? (
        <Section title="Action timeline">
          <ol className="relative ml-3 space-y-4 border-l border-slate-200 pl-6">
            {bill.actions.map((a) => (
              <li key={a.id} className="relative">
                <span
                  aria-hidden
                  className="absolute -left-[1.65rem] top-1.5 h-2 w-2 rounded-full bg-slate-400"
                />
                <p className="text-sm text-slate-500">{a.date}</p>
                <p className="text-sm text-slate-800">{a.description}</p>
                {a.chamber ? (
                  <p className="text-xs text-slate-400">{a.chamber}</p>
                ) : null}
              </li>
            ))}
          </ol>
        </Section>
      ) : null}

      {bill.versions?.length ? (
        <Section title="Versions">
          <ul className="space-y-2">
            {bill.versions.map((v) => (
              <li
                key={v.id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-slate-200 px-3 py-2 text-sm"
              >
                <span>
                  {v.note ?? "Version"}
                  {v.date ? ` — ${v.date}` : ""}
                </span>
                <span className="flex gap-3">
                  {v.url ? (
                    <a
                      href={v.url}
                      className="text-slate-600 underline hover:text-slate-900"
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      Official source
                    </a>
                  ) : null}
                  <Link
                    href={`/bills/${id}/compare`}
                    className="text-slate-600 underline hover:text-slate-900"
                  >
                    Compare
                  </Link>
                </span>
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      {bill.documents?.length ? (
        <Section title="Documents">
          <ul className="space-y-2">
            {bill.documents.map((d) => (
              <li key={d.id} className="text-sm">
                {d.url ? (
                  <a
                    href={d.url}
                    className="text-slate-700 underline hover:text-slate-900"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    {d.note ?? "Document"}
                  </a>
                ) : (
                  d.note ?? "Document"
                )}
                {d.date ? (
                  <span className="ml-2 text-xs text-slate-400">
                    {d.date}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      {bill.votes?.length ? (
        <Section title="Votes">
          <ul className="space-y-3">
            {bill.votes.map((v) => (
              <li
                key={v.id}
                className="rounded-md border border-slate-200 p-3 text-sm"
              >
                <p className="font-medium text-slate-800">{v.motion_text}</p>
                <p className="mt-1 text-xs text-slate-500">
                  {v.date ? `${v.date} · ` : ""}
                  {v.chamber ? `${v.chamber} · ` : ""}
                  {v.result ?? "result unknown"}
                  {typeof v.yes_count === "number"
                    ? ` · ${v.yes_count}-${v.no_count ?? 0}`
                    : ""}
                </p>
                {v.votes?.length ? (
                  <details className="mt-2">
                    <summary className="cursor-pointer text-xs text-slate-500">
                      Member-level votes ({v.votes.length})
                    </summary>
                    <ul className="mt-2 grid grid-cols-2 gap-1 text-xs sm:grid-cols-3">
                      {v.votes.map((mv) => (
                        <li key={mv.id}>
                          {mv.voter_name}: {mv.option}
                        </li>
                      ))}
                    </ul>
                  </details>
                ) : null}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      {bill.related_bills?.length ? (
        <Section title="Related bills">
          <ul className="space-y-1 text-sm">
            {bill.related_bills.map((r) => (
              <li key={r.id}>
                <Link href={`/bills/${r.id}`} className="hover:underline">
                  {r.jurisdiction_code ? `${r.jurisdiction_code} ` : ""}
                  {r.identifier} — {r.title}
                </Link>
                {r.relation_type ? (
                  <span className="ml-2 text-xs text-slate-400">
                    ({r.relation_type})
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      <Section title="Official source">
        {bill.official_source_url ? (
          <a
            href={bill.official_source_url}
            className="text-sm text-slate-700 underline hover:text-slate-900"
            target="_blank"
            rel="noopener noreferrer"
          >
            {bill.official_source_url}
          </a>
        ) : (
          <p className="text-sm text-slate-500">
            No official source URL captured yet.
          </p>
        )}
      </Section>

      {bill.sources?.length ? (
        <Section title="Attribution">
          <ul className="space-y-1 text-sm text-slate-600">
            {bill.sources.map((s, i) => (
              <li key={i}>
                Data from{" "}
                <a
                  href={s.source_url}
                  className="underline hover:text-slate-900"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {s.source_name}
                </a>
                {s.retrieved_at ? `, retrieved ${s.retrieved_at}` : ""}
                {s.license_note ? ` — ${s.license_note}` : ""}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      {bill.known_limitations?.length ? (
        <Section title="Known limitations">
          <ul className="list-inside list-disc space-y-1 text-sm text-amber-800">
            {bill.known_limitations.map((l, i) => (
              <li key={i}>{l}</li>
            ))}
          </ul>
        </Section>
      ) : null}

      <p className="mt-8 text-xs text-slate-400">
        See the{" "}
        <Link href="/methodology" className="underline">
          methodology page
        </Link>{" "}
        for data sources and limitations.
      </p>
    </div>
  );
}

function Field({ label, value }: { label: string; value?: string | null }) {
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
        {label}
      </dt>
      <dd className="mt-0.5 text-slate-800">{value ?? "—"}</dd>
    </div>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="mt-8">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
        {title}
      </h2>
      <div className="mt-2">{children}</div>
    </section>
  );
}
