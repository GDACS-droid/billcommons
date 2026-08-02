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
      <div className="mx-auto max-w-4xl px-4 py-12 sm:px-6">
        <DataUnavailable message="This bill is temporarily unavailable." />
      </div>
    );
  }

  const bill = result.data;
  const id = bill.id;
  const jurisdictionCode = jurisdiction?.ok ? jurisdiction.data.abbreviation : undefined;
  const jurisdictionName = jurisdiction?.ok ? jurisdiction.data.name : undefined;

  return (
    <div className="mx-auto max-w-4xl px-4 py-12 sm:px-6">
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
      <nav aria-label="Breadcrumb" className="text-xs text-slate-500">
        <Link href="/states" className="text-blue-800 hover:text-blue-700 hover:underline">
          States
        </Link>{" "}
        {jurisdictionCode ? (
          <>
            /{" "}
            <Link
              href={`/states/${jurisdictionCode}`}
              className="text-blue-800 hover:text-blue-700 hover:underline"
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
              className="text-blue-800 hover:text-blue-700 hover:underline"
            >
              {sessionLabel}
            </Link>{" "}
          </>
        ) : null}
        / {bill.identifier}
      </nav>

      <header className="mt-5 border-b border-slate-200 pb-7">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-3xl font-semibold tracking-tight text-slate-950">
            {jurisdictionCode ? `${jurisdictionCode} ` : ""}
            {bill.identifier}
          </h1>
          <BillStatusBadge status={bill.status} />
        </div>
        <p className="mt-3 text-lg leading-7 text-slate-800">{bill.title}</p>
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

      <QuickAnswers
        bill={bill}
        jurisdictionCode={jurisdictionCode}
        sessionLabel={sessionLabel}
        sponsors={sponsors.ok ? sponsors.data : null}
      />

      {bill.description ? (
        <section className="mt-8">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
            Description
          </h2>
          <p className="mt-2 text-slate-800">{bill.description}</p>
        </section>
      ) : null}

      <dl className="surface-card mt-8 grid grid-cols-2 gap-x-5 gap-y-6 p-5 text-sm sm:grid-cols-3">
        <Field label="Introduced" value={bill.introduced_date} />
        {/* No "Status date" field: bills.status_date is NULL for all 209,814
            rows -- the column is declared and read in several places but never
            written by any ingest or status job, so this rendered an
            always-blank cell. See the note on BillDetail.status_date. */}
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
                    className="inline-flex rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs text-slate-700 transition-colors hover:border-blue-200 hover:bg-blue-50 hover:text-blue-800"
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
                className="rounded-md border border-slate-200 bg-white px-3 py-2 text-sm transition-colors hover:border-slate-300 hover:bg-slate-50"
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
          <ol className="relative ml-3 space-y-5 border-l border-slate-200 pl-6">
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
                className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-slate-200 bg-white px-3 py-2 text-sm transition-colors hover:border-slate-300 hover:bg-slate-50"
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
                className="rounded-md border border-slate-200 bg-white p-3 text-sm transition-colors hover:border-slate-300 hover:bg-slate-50"
              >
                <p className="font-medium text-slate-800">
                  {v.motion_text ?? "Motion text not provided"}
                </p>
                <p className="mt-1 text-xs tabular-nums text-slate-500">
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
                className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-slate-200 bg-white px-3 py-2 text-sm transition-colors hover:border-slate-300 hover:bg-slate-50"
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
 * Question-shaped answer block. AI search retrieval chunks pages into ~500-token
 * blocks keyed by their nearest headings, so the answer to "did HB 571 pass?"
 * must live as a self-contained question + 2-3 sentence answer near the top of
 * the page -- the status badge alone is not extractable prose. Answers are
 * derived deterministically from the controlled status vocabulary
 * (workers/ingest/billcommons_ingest/status.py); an unknown status says so
 * rather than guessing.
 */
function QuickAnswers({
  bill,
  jurisdictionCode,
  sessionLabel,
  sponsors,
}: {
  bill: NonNullable<BillPageData["bill"] & { ok: true }>["data"];
  jurisdictionCode?: string;
  sessionLabel?: string | null;
  sponsors: { name?: string | null; primary?: boolean | null; classification?: string | null }[] | null;
}) {
  const ref = `${jurisdictionCode ? `${jurisdictionCode} ` : ""}${bill.identifier}`;
  // In practice this is always latest_action_date: status_date is unpopulated
  // corpus-wide (see BillDetail.status_date). The fallback is kept so the
  // prose starts using the real value the moment that column is populated,
  // rather than needing to be found and changed again.
  const asOf = bill.status_date ?? bill.latest_action_date;

  const passAnswers: Record<string, string> = {
    enacted: `Yes. ${ref} has been enacted into law${asOf ? ` as of ${asOf}` : ""}.`,
    vetoed: `No. ${ref} passed the legislature but was vetoed${asOf ? ` on ${asOf}` : ""}.`,
    dead: `No. ${ref} did not pass — it was defeated or died in the legislative process${asOf ? ` (${asOf})` : ""}.`,
    withdrawn: `No. ${ref} was withdrawn${asOf ? ` on ${asOf}` : ""} and is no longer under consideration.`,
    died_on_adjournment: `No. ${ref} died when ${
      sessionLabel
        ? // Some upstream session identifiers are machine labels ("2025_26");
          // make sure the sentence still reads as English.
          `the ${sessionLabel.replaceAll("_", "-")}${
            /session/i.test(sessionLabel) ? "" : " session"
          }`
        : "its legislative session"
    } adjourned without final action on it. Bills that die this way are sometimes reintroduced in a later session.`,
    enrolled: bill.enrolled_outcome_uncaptured
      ? // The session adjourned long enough ago that the executive-action
        // window has certainly closed -- every state bounds it, typically at
        // 5-45 days from presentment. Saying "awaiting signature" here was
        // asserting a wait that ended months ago: 2,192 Texas bills from a
        // session that closed in June 2025 all read that way.
        `${ref} passed both chambers and was enrolled, but its session adjourned long enough ago that the executive-action window has closed. Bill Commons did not capture the final signature or veto, so the outcome is unknown here — check the ${
          jurisdictionCode ?? "state"
        } legislature's own record.`
      : `Not yet law. ${ref} has passed both chambers and is enrolled, awaiting executive action (signature or veto)${asOf ? ` as of ${asOf}` : ""}.`,
    passed_both: `Not yet law. ${ref} has passed both chambers but has not been enacted${asOf ? ` as of ${asOf}` : ""}.`,
    passed_one_chamber: `Not yet. ${ref} has passed one chamber and awaits action in the other${asOf ? ` as of ${asOf}` : ""}.`,
    in_committee: `Not yet. ${ref} is in committee${asOf ? ` as of ${asOf}` : ""} and has not come to a final vote.`,
    introduced: `Not yet. ${ref} has been introduced${asOf ? ` as of ${asOf}` : ""} but has not advanced to a vote.`,
  };
  const passAnswer =
    (bill.status && passAnswers[bill.status]) ??
    `The current status of ${ref} is not known from the official record.`;
  const latest =
    bill.latest_action_date && bill.latest_action_text
      ? ` Latest recorded action (${bill.latest_action_date}): ${bill.latest_action_text}`
      : "";

  const primaries = (sponsors ?? []).filter((s) => s.primary && s.name);
  const cosponsorCount = (sponsors ?? []).length - primaries.length;
  const sponsorAnswer = primaries.length
    ? `${primaries
        .slice(0, 3)
        .map((s) => s.name)
        .join(", ")}${primaries.length > 3 ? ` and ${primaries.length - 3} others` : ""} ${
        primaries.length === 1 ? "is the primary sponsor" : "are the primary sponsors"
      } of ${ref}${cosponsorCount > 0 ? `, joined by ${cosponsorCount} cosponsor${cosponsorCount === 1 ? "" : "s"}` : ""}.`
    : null;

  return (
    <section className="mt-8">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
        Quick answers
      </h2>
      <div className="surface-card mt-2 space-y-4 p-5">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">Did {ref} pass?</h3>
          <p className="mt-1 text-sm leading-6 text-slate-800">
            {passAnswer}
            {latest}
          </p>
        </div>
        <div>
          <h3 className="text-sm font-semibold text-slate-900">What is {ref} about?</h3>
          <p className="mt-1 text-sm leading-6 text-slate-800">
            {bill.description && bill.description !== bill.title
              ? bill.description
              : `${ref} is a ${bill.bill_type ?? "bill"}${
                  sessionLabel ? ` in the ${sessionLabel}` : ""
                } titled “${bill.title}”.`}
          </p>
        </div>
        {sponsorAnswer ? (
          <div>
            <h3 className="text-sm font-semibold text-slate-900">Who sponsors {ref}?</h3>
            <p className="mt-1 text-sm leading-6 text-slate-800">{sponsorAnswer}</p>
          </div>
        ) : null}
      </div>
    </section>
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
    <section className="mt-10 border-t border-slate-200 pt-8">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
        {title}
      </h2>
      <div className="mt-2">{children}</div>
    </section>
  );
}
