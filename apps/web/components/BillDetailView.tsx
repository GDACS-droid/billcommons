import Link from "next/link";
import DataUnavailable from "@/components/DataUnavailable";
import JsonLd from "@/components/JsonLd";
import { BillStatusBadge } from "@/components/StatusBadge";
import { SITE_URL } from "@/lib/config";
import type { BillPageData } from "@/lib/bill";

// Upstream relation vocabulary, rendered as something a reader understands.
const RELATION_LABELS: Record<string, string> = {
  "prior-session": "Prior session",
  companion: "Companion bill",
  replaces: "Replaces",
};

interface Props {
  data: BillPageData;
  /** Canonical path for this bill, used to anchor structured data. */
  canonicalPath: string;
  /** Slugified session segment, when known, so the breadcrumb can link it. */
  sessionSlug?: string | null;
  sessionLabel?: string | null;
}

export default function BillDetailView({
  data,
  canonicalPath,
  sessionSlug,
  sessionLabel,
}: Props) {
  const {
    bill: result,
    versions,
    actions,
    sponsors,
    votes,
    documents,
    related,
    subjects,
    jurisdiction,
  } = data;

  if (!result.ok) {
    return (
      <div className="mx-auto max-w-4xl px-4 py-10 sm:px-6">
        <DataUnavailable message="This bill is temporarily unavailable." />
      </div>
    );
  }

  const bill = result.data;
  const id = bill.id;
  const jurisdictionCode = jurisdiction?.ok ? jurisdiction.data.abbreviation : undefined;
  const jurisdictionName = jurisdiction?.ok ? jurisdiction.data.name : undefined;

  return (
    <div className="mx-auto max-w-4xl px-4 py-10 sm:px-6">
      <JsonLd data={billJsonLd({ bill, jurisdictionName, jurisdictionCode, canonicalPath, sessionLabel })} />
      <JsonLd
        data={breadcrumbJsonLd({
          bill,
          jurisdictionCode,
          jurisdictionName,
          sessionSlug,
          sessionLabel,
          canonicalPath,
        })}
      />
      <nav aria-label="Breadcrumb" className="text-sm text-slate-500">
        <Link href="/states" className="hover:underline">
          States
        </Link>{" "}
        {jurisdictionCode ? (
          <>
            /{" "}
            <Link
              href={`/states/${jurisdictionCode}`}
              className="hover:underline"
            >
              {jurisdictionCode}
            </Link>{" "}
          </>
        ) : null}
        {jurisdictionCode && sessionSlug && sessionLabel ? (
          <>
            /{" "}
            <Link
              href={`/states/${jurisdictionCode}/sessions/${encodeURIComponent(
                sessionLabel
              )}`}
              className="hover:underline"
            >
              {sessionLabel}
            </Link>{" "}
          </>
        ) : null}
        / {bill.identifier}
      </nav>

      <header className="mt-2">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold text-slate-900">
            {jurisdictionCode ? `${jurisdictionCode} ` : ""}
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
          {jurisdictionName ?? jurisdictionCode ?? "Jurisdiction not provided by source"}
          {sessionLabel ? ` · ${sessionLabel}` : ""}
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
                  bill.latest_action_text ? ` — ${bill.latest_action_text}` : ""
                }`
              : null
          }
        />
        <Field label="Bill type" value={bill.bill_type} />
        <Field label="Last updated" value={bill.upstream_updated_at} />
      </dl>

      {subjects.ok && subjects.data.length ? (
        <Section title="Subjects">
          <ul className="flex flex-wrap gap-2">
            {subjects.data.map((subject) => (
              <li key={subject}>
                <Link
                  href={`/search?subject=${encodeURIComponent(subject)}`}
                  className="inline-flex rounded-full bg-slate-100 px-3 py-1 text-xs text-slate-700 hover:bg-slate-200"
                >
                  {subject}
                </Link>
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      <Section title="Sponsors">
        {!sponsors.ok ? (
          <DataUnavailable message="Sponsor data is temporarily unavailable." />
        ) : sponsors.data.length ? (
          <ul className="grid gap-2 sm:grid-cols-2">
            {sponsors.data.map((s) => (
              <li
                key={s.id}
                className="rounded-md border border-slate-200 px-3 py-2 text-sm"
              >
                {s.person_id ? (
                  <Link href={`/people/${s.person_id}`} className="font-medium hover:underline">
                    {s.name ?? "Unnamed sponsor"}
                  </Link>
                ) : (
                  <span className="font-medium">{s.name ?? "Unnamed sponsor"}</span>
                )}
                <span className="ml-2 text-xs text-slate-500">
                  {s.classification ?? (s.primary ? "primary" : "cosponsor")}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-slate-500">Not provided by source.</p>
        )}
      </Section>

      <Section title="Committees">
        <p className="text-sm text-slate-500">Not provided by source.</p>
      </Section>

      <Section title="Action timeline">
        {!actions.ok ? (
          <DataUnavailable message="Action timeline is temporarily unavailable." />
        ) : actions.data.length ? (
          <ol className="relative ml-3 space-y-4 border-l border-slate-200 pl-6">
            {actions.data.map((a) => (
              <li key={a.id} className="relative">
                <span
                  aria-hidden
                  className="absolute -left-[1.65rem] top-1.5 h-2 w-2 rounded-full bg-slate-400"
                />
                <p className="text-sm text-slate-500">{a.action_date ?? "Date not provided"}</p>
                <p className="text-sm text-slate-800">{a.description}</p>
                {a.classification ? (
                  <p className="text-xs text-slate-400">{a.classification}</p>
                ) : null}
              </li>
            ))}
          </ol>
        ) : (
          <p className="text-sm text-slate-500">Not provided by source.</p>
        )}
      </Section>

      <Section title="Versions">
        {!versions.ok ? (
          <DataUnavailable message="Version data is temporarily unavailable." />
        ) : versions.data.length ? (
          <ul className="space-y-2">
            {versions.data.map((v) => (
              <li
                key={v.id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-slate-200 px-3 py-2 text-sm"
              >
                <span>
                  {v.note ?? "Version"}
                  {v.date ? ` — ${v.date}` : ""}
                </span>
                <Link
                  href={`/bills/${id}/compare`}
                  className="text-slate-600 underline hover:text-slate-900"
                >
                  Compare
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-slate-500">Not provided by source.</p>
        )}
      </Section>

      <Section title="Documents">
        {!documents.ok ? (
          <DataUnavailable message="Document data is temporarily unavailable." />
        ) : documents.data.length ? (
          <ul className="space-y-2">
            {documents.data.map((d) => (
              <li key={d.id} className="text-sm">
                {d.url ? (
                  <a
                    href={d.url}
                    className="text-slate-700 underline hover:text-slate-900"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    {d.media_type ?? "Document"}
                  </a>
                ) : (
                  d.media_type ?? "Document"
                )}
                {!d.has_extracted_text ? (
                  <span className="ml-2 text-xs text-slate-400">
                    (no extracted text yet)
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-slate-500">Not provided by source.</p>
        )}
      </Section>

      <Section title="Votes">
        {!votes.ok ? (
          <DataUnavailable message="Vote data is temporarily unavailable." />
        ) : votes.data.length ? (
          <ul className="space-y-3">
            {votes.data.map((v) => (
              <li
                key={v.id}
                className="rounded-md border border-slate-200 p-3 text-sm"
              >
                <p className="font-medium text-slate-800">
                  {v.motion_text ?? "Motion text not provided"}
                </p>
                <p className="mt-1 text-xs text-slate-500">
                  {v.start_date ? `${v.start_date} · ` : ""}
                  {v.result ?? "result unknown"}
                  {typeof v.yes_count === "number"
                    ? ` · ${v.yes_count}-${v.no_count ?? 0}`
                    : ""}
                </p>
                {v.votes.length ? (
                  <details className="mt-2">
                    <summary className="cursor-pointer text-xs text-slate-500">
                      Member-level votes ({v.votes.length})
                    </summary>
                    <ul className="mt-2 grid grid-cols-2 gap-1 text-xs sm:grid-cols-3">
                      {v.votes.map((mv) => (
                        <li key={mv.id}>
                          {mv.voter_name ?? "Unnamed"}: {mv.option}
                        </li>
                      ))}
                    </ul>
                  </details>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-slate-500">Not provided by source.</p>
        )}
      </Section>

      <Section title="Related bills">
        {!related.ok ? (
          <DataUnavailable message="Related-bill data is temporarily unavailable." />
        ) : related.data.length ? (
          <ul className="space-y-2">
            {related.data.map((link) => (
              <li
                key={link.id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-slate-200 px-3 py-2 text-sm"
              >
                <span>
                  <span className="text-slate-500">
                    {RELATION_LABELS[link.relation_type ?? ""] ??
                      link.relation_type ??
                      "Related"}
                    :{" "}
                  </span>
                  {link.related_bill_id ? (
                    <Link
                      href={`/bills/${link.related_bill_id}`}
                      className="font-medium hover:underline"
                    >
                      {link.related_identifier ?? "View bill"}
                    </Link>
                  ) : (
                    <span className="font-medium">
                      {link.related_identifier ?? "Unnamed"}
                    </span>
                  )}
                </span>
                {!link.related_bill_id ? (
                  // Being explicit beats a dead-looking row: most prior-session
                  // targets sit in a session this corpus does not hold, and
                  // saying so is more useful than silently rendering plain text.
                  <span className="text-xs text-slate-400">
                    not in this corpus
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-slate-500">
            No related bills recorded for this bill.
          </p>
        )}
      </Section>

      <Section title="Official source">
        {bill.source_url ? (
          <a
            href={bill.source_url}
            className="text-sm text-slate-700 underline hover:text-slate-900"
            target="_blank"
            rel="noopener noreferrer"
          >
            {bill.source_url}
          </a>
        ) : (
          <p className="text-sm text-slate-500">
            No official source URL captured yet.
          </p>
        )}
      </Section>

      <Section title="Attribution">
        <p className="text-sm text-slate-600">
          {bill.source_name ? `Data from ${bill.source_name}` : "Source not provided."}
          {bill.retrieved_at ? `, retrieved ${bill.retrieved_at}` : ""}
        </p>
      </Section>

      <Section title="Use this data">
        <p className="text-sm text-slate-600">
          Every field on this page is available from the free public API — no key
          or licence required. Fetch this bill as JSON:{" "}
          <code className="rounded bg-slate-100 px-1 py-0.5 text-xs">
            GET /api/v1/bills?jurisdiction={jurisdictionCode ?? "XX"}&amp;identifier=
            {bill.identifier_norm}
          </code>
          . See the{" "}
          <Link href="/docs/api" className="underline">
            API docs
          </Link>{" "}
          or the{" "}
          <Link href="/docs/mcp" className="underline">
            MCP server
          </Link>{" "}
          for AI assistants.
        </p>
      </Section>

      <Section title="Known limitations">
        <ul className="list-inside list-disc space-y-1 text-sm text-amber-800">
          <li>Sponsor party and chamber affiliation are not yet captured by this API.</li>
          <li>Committee referrals are not yet captured.</li>
          {documents.ok && documents.data.some((d) => !d.has_extracted_text) ? (
            <li>Some documents have no extracted text yet, so version comparison may be limited.</li>
          ) : null}
        </ul>
      </Section>

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

/**
 * schema.org/Legislation. This is the vocabulary search engines and AI crawlers
 * read to understand that a page IS a bill rather than an article about one --
 * without it a bill page is indistinguishable from prose that mentions "SB 160".
 */
function billJsonLd({
  bill,
  jurisdictionName,
  jurisdictionCode,
  canonicalPath,
  sessionLabel,
}: {
  bill: NonNullable<BillPageData["bill"] & { ok: true }>["data"];
  jurisdictionName?: string;
  jurisdictionCode?: string;
  canonicalPath: string;
  sessionLabel?: string | null;
}) {
  return {
    "@context": "https://schema.org",
    "@type": "Legislation",
    "@id": `${SITE_URL}${canonicalPath}`,
    url: `${SITE_URL}${canonicalPath}`,
    name: `${jurisdictionCode ? `${jurisdictionCode} ` : ""}${bill.identifier}`,
    alternateName: bill.identifier_norm,
    legislationIdentifier: bill.identifier,
    headline: bill.title,
    description: bill.description ?? bill.title,
    legislationType: bill.bill_type ?? "Bill",
    ...(bill.introduced_date ? { legislationDate: bill.introduced_date } : {}),
    ...(bill.upstream_updated_at ? { dateModified: bill.upstream_updated_at } : {}),
    ...(sessionLabel ? { legislationLegislativeBody: sessionLabel } : {}),
    ...(jurisdictionName
      ? {
          legislationJurisdiction: {
            "@type": "AdministrativeArea",
            name: jurisdictionName,
          },
        }
      : {}),
    ...(bill.source_url ? { sameAs: bill.source_url } : {}),
    isAccessibleForFree: true,
    inLanguage: "en",
  };
}

function breadcrumbJsonLd({
  bill,
  jurisdictionCode,
  jurisdictionName,
  sessionSlug,
  sessionLabel,
  canonicalPath,
}: {
  bill: { identifier: string };
  jurisdictionCode?: string;
  jurisdictionName?: string;
  sessionSlug?: string | null;
  sessionLabel?: string | null;
  canonicalPath: string;
}) {
  const items: { name: string; item: string }[] = [
    { name: "States", item: `${SITE_URL}/states` },
  ];
  if (jurisdictionCode) {
    items.push({
      name: jurisdictionName ?? jurisdictionCode,
      item: `${SITE_URL}/states/${jurisdictionCode}`,
    });
  }
  if (jurisdictionCode && sessionSlug && sessionLabel) {
    items.push({
      name: sessionLabel,
      item: `${SITE_URL}/states/${jurisdictionCode}/sessions/${encodeURIComponent(
        sessionLabel
      )}`,
    });
  }
  items.push({ name: bill.identifier, item: `${SITE_URL}${canonicalPath}` });

  return {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: items.map((entry, index) => ({
      "@type": "ListItem",
      position: index + 1,
      name: entry.name,
      item: entry.item,
    })),
  };
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
