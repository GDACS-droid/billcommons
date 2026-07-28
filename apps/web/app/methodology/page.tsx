import type { Metadata } from "next";
import Link from "next/link";
import PageHeader from "@/components/PageHeader";

export const metadata: Metadata = {
  title: "Methodology",
  description:
    "How Bill Commons sources, verifies, and refreshes legislative data across all 50 states and DC.",
  alternates: { canonical: "/methodology" },
};

export default function MethodologyPage() {
  return (
    <div className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Data standards"
        title="Methodology"
        description={
          <p>
        Bill Commons aims to be the most complete, accurate, and honest
        public index of state legislation. This page explains where the
        data comes from, how it&rsquo;s verified, and where it currently
        falls short.
          </p>
        }
      />

      <section className="mt-10 border-t border-slate-200 pt-8">
        <h2 className="text-lg font-semibold text-slate-900">
          Data sources (tiered)
        </h2>
        <ul className="mt-3 space-y-3 text-sm text-slate-700">
          <li>
            <strong>Tier 1 — Official state sources.</strong> State
            legislature APIs, bulk data, and websites, used directly where
            technically and legally feasible.
          </li>
          <li>
            <strong>Tier 2 — Open States.</strong> The Open States v3 API
            and bulk CSV exports, our primary bootstrap source for all 51
            jurisdictions. Open States data is public domain; we preserve
            their attribution and source links on every record.
          </li>
          <li>
            <strong>Tier 3 — LegiScan (optional).</strong> Used only via
            LegiScan&rsquo;s authorized API/datasets under CC BY 4.0
            attribution. The platform is fully functional without it.
          </li>
          <li>
            <strong>Tier 4 — Compliant direct extraction.</strong> Used only
            where robots.txt, terms of service, and rate limits are
            respected, with an honest user agent and no CAPTCHA or
            authentication bypass. If a source blocks us, we document the
            blocker and fall back to Tier 2.
          </li>
        </ul>
      </section>

      <section className="mt-10 border-t border-slate-200 pt-8">
        <h2 className="text-lg font-semibold text-slate-900">Attribution</h2>
        <p className="mt-2 text-sm text-slate-700">
          Every bill record on Bill Commons carries a source name, source
          URL, and retrieval timestamp. Where data originates from Open
          States, we preserve their public-domain notice; where LegiScan
          data is used, we preserve CC BY 4.0 attribution. See each bill
          page&rsquo;s &ldquo;Attribution&rdquo; section for record-level
          sourcing.
        </p>
      </section>

      <section className="mt-10 border-t border-slate-200 pt-8">
        <h2 className="text-lg font-semibold text-slate-900">
          Coverage state machine
        </h2>
        <p className="mt-2 text-sm text-slate-700">
          Each jurisdiction moves through a tracked pipeline:
        </p>
        <p className="mt-2 font-mono text-xs text-slate-600">
          NOT_STARTED → SOURCE_IDENTIFIED → BOOTSTRAPPED → METADATA_SEARCHABLE
          → FULL_TEXT_SEARCHABLE → VALIDATING → GREEN | DEGRADED | BLOCKED
        </p>
        <p className="mt-3 text-sm text-slate-700">
          A jurisdiction reaches <strong>GREEN</strong> only when all of the
          following hold: its current session is identified from an
          authoritative source; all discoverable bills for that session are
          imported; bills are searchable by number and title; description,
          subjects, sponsors, and actions are searchable wherever supplied;
          full text is searchable wherever technically available from the
          source; official source URLs are retained on every record;
          incremental refresh succeeds; validation samples pass; the search
          index count matches the database count; and there are no
          unexplained zero counts. For states without a 2026 regular
          session, GREEN requires the current legislative cycle to be
          explicitly verified with no new session pending.
        </p>
        <p className="mt-3 text-sm text-slate-700">
          Live status for every jurisdiction is public on the{" "}
          <Link href="/coverage" className="underline">
            coverage page
          </Link>
          , including bill counts, full-text percentage, last update time,
          validation sample size and pass rate, and any known gaps.
        </p>
      </section>

      <section className="mt-10 border-t border-slate-200 pt-8">
        <h2 className="text-lg font-semibold text-slate-900">
          Refresh policy
        </h2>
        <ul className="mt-2 space-y-1 text-sm text-slate-700">
          <li>Active regular/special sessions: every 15–30 minutes</li>
          <li>Year-round legislatures: hourly</li>
          <li>Recently adjourned sessions: daily</li>
          <li>Dormant sessions: weekly status check</li>
          <li>Calendars and special-session notices: daily</li>
        </ul>
        <p className="mt-3 text-sm text-slate-700">
          Refreshes use conditional requests, exponential backoff, circuit
          breakers, and per-source concurrency limits. Unexpected bill-count
          drops or schema changes trigger alerts rather than silent data
          loss.
        </p>
      </section>

      <section className="mt-10 border-t border-slate-200 pt-8">
        <h2 className="text-lg font-semibold text-slate-900">
          Version diffing
        </h2>
        <p className="mt-2 text-sm text-slate-700">
          Bill text is normalized from HTML, XML, plain text, and text-layer
          PDFs; original source files are preserved. Scanned PDFs that
          require OCR are flagged with a confidence indicator and are never
          presented as authoritative without that warning. Version-to-version
          diffs are computed deterministically (line anchors, adds, deletes,
          moves, and section headings) — never by a language model.
        </p>
      </section>

      <section className="mt-10 border-t border-slate-200 pt-8">
        <h2 className="text-lg font-semibold text-slate-900">
          Quality assurance
        </h2>
        <p className="mt-2 text-sm text-slate-700">
          Each jurisdiction is spot-checked against at least five random
          bills compared to the official source (number, title, session,
          sponsor, latest action), at least one full bill-text document
          verified, and bill-number and keyword search verified against
          known official text. Results are recorded and summarized on the{" "}
          <Link href="/coverage" className="underline">
            coverage page
          </Link>
          .
        </p>
      </section>

      <section className="mt-10 border-t border-slate-200 pt-8">
        <h2 className="text-lg font-semibold text-slate-900">
          Known limitations
        </h2>
        <ul className="mt-2 list-inside list-disc space-y-1 text-sm text-slate-700">
          <li>
            Coverage depth varies by jurisdiction and by how quickly each
            reaches GREEN status — check the coverage page before relying on
            completeness for a given state.
          </li>
          <li>
            Full text may be unavailable for some documents at the source
            (e.g. scanned-only historical filings).
          </li>
          <li>
            Member-level vote detail depends on what the underlying source
            publishes; some jurisdictions report only aggregate counts.
          </li>
          <li>
            AI-generated summaries, if ever introduced, will always be
            labeled as generated analysis with linked sources — never
            presented as an official record.
          </li>
        </ul>
      </section>
    </div>
  );
}
