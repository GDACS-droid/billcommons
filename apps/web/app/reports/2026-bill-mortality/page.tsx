import Link from "next/link";
import type { Metadata } from "next";
import DataUnavailable from "@/components/DataUnavailable";
import JsonLd from "@/components/JsonLd";
import { apiGet } from "@/lib/api";
import { API_DOCS_URL, SITE_URL } from "@/lib/config";
import PageHeader from "@/components/PageHeader";
import type { MortalityReport } from "@/lib/types";

// Aggregates over the whole corpus move slowly; six hours keeps the report
// fresh through a sine die while holding the query to four hits a day.
const REPORT_REVALIDATE = 21600;

export const metadata: Metadata = {
  title: "How State Bills Die: The 2026 Bill Mortality Report",
  description:
    "35% of all state bills this cycle died without a vote, a hearing, or any recorded action — the session simply ran out. Enactment and mortality rates for all 50 states + DC, from 209,000+ tracked bills.",
  alternates: { canonical: "/reports/2026-bill-mortality" },
};

async function getReport() {
  return apiGet<MortalityReport>(
    "/api/v1/stats/mortality",
    undefined,
    { revalidate: REPORT_REVALIDATE }
  );
}

const fmt = (n: number) => n.toLocaleString("en-US");

export default async function MortalityReportPage() {
  const result = await getReport();

  return (
    <div className="mx-auto max-w-6xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Bill Commons Report · 2026 cycle"
        title="How State Bills Die"
        description={
          <p className="max-w-3xl">
        The most common way a state bill ends is not a vote. It is the session
        adjourning with the bill still sitting in committee — nothing is filed,
        no action is recorded, the bill just stops. Trackers that read only the
        action record report those bills as alive indefinitely. This report
        measures that silence across every state legislature in the country.
          </p>
        }
      />

      <div className="mt-8 max-w-3xl rounded-lg border-l-[3px] border-rose-500 bg-rose-50 px-4 py-3">
        <p className="text-sm font-semibold text-slate-900">
          Correction in progress — read before citing these figures (2 August
          2026)
        </p>
        <p className="mt-2 text-sm text-slate-800">
          A bill is marked <code>died_on_adjournment</code> when its session&apos;s
          end date has passed. That end date is the{" "}
          <strong>expected</strong> adjournment reported by our upstream source,
          not a recorded sine die — so when a chamber sits past its estimate,
          the calendar alone marks every live bill in it dead.
        </p>
        <p className="mt-2 text-sm text-slate-800">
          <strong>
            29,227 bills across 8 jurisdictions are currently marked dead inside
            sessions that the same source still reports as active
          </strong>{" "}
          — 18,343 of them in Massachusetts, whose General Court runs to the end
          of the year. That is 30.8% of the national{" "}
          <code>died_on_adjournment</code> count on this page.
        </p>
        <p className="mt-2 text-sm text-slate-800">
          Removing all of them would move the adjournment-death share from{" "}
          <strong>45.2% to 31.3%</strong> of the corpus. The true figure is
          somewhere in that range: some of those sessions genuinely have
          adjourned and the upstream active flag is stale, and we are not yet
          able to say which. Until that is resolved,{" "}
          <strong>treat the per-state adjournment counts as an upper bound</strong>{" "}
          and the enactment counts, which are unaffected, as sound.
        </p>
      </div>

      {!result.ok ? (
        <div className="mt-10">
          <DataUnavailable message="Report data is temporarily unavailable." />
        </div>
      ) : (
        <ReportBody report={result.data} />
      )}

      <section className="mt-14 max-w-3xl border-t border-slate-200 pt-8">
        <h2 className="text-lg font-semibold text-slate-900">Methodology</h2>
        <div className="mt-3 space-y-3 text-sm text-slate-600">
          <p>
            Statuses are derived from each bill&apos;s official action record
            (via Open States / Plural and state sources), then a calendar rule
            is applied: a bill still at a live stage when its session&apos;s
            confirmed adjournment date passes is marked{" "}
            <code className="rounded bg-slate-100 px-1">died_on_adjournment</code>.
            The rule is deliberately conservative:
          </p>
          <ul className="list-disc space-y-1 pl-5">
            <li>
              Enrolled bills survive adjournment — governors sign for weeks
              after sine die, and calling those dead would be the same error
              in the other direction.
            </li>
            <li>
              A real outcome is never overwritten: &quot;died in
              committee&quot; stays as recorded rather than being flattened
              into the adjournment bucket, so <em>Killed</em> and{" "}
              <em>Died on adjournment</em> are disjoint.
            </li>
            <li>
              Where a session&apos;s end date is unconfirmed — mostly two-year
              carryover biennia where pending bills genuinely roll into year
              two — nothing is assumed and bills stay <em>Pending</em>.
            </li>
          </ul>
          <p>
            Full derivation rules are in the{" "}
            <Link href="/methodology" className="underline">
              methodology
            </Link>
            , and every underlying bill is public via the{" "}
            <Link href="/docs/api" className="underline">
              free API
            </Link>{" "}
            (<code className="rounded bg-slate-100 px-1">/api/v1/stats/mortality</code>{" "}
            reproduces this table). Cite as: Bill Commons, &quot;How State
            Bills Die,&quot; billcommons.org.
          </p>
        </div>
      </section>
    </div>
  );
}

function ReportBody({ report }: { report: MortalityReport }) {
  const { totals } = report;
  // Ranked by the CROSS-STATE COMPARABLE figure. Ranking by adjournment
  // mortality alone sorted states by their clerks' filing habits: California,
  // Wisconsin and New York record a death action for every bill and so score
  // 0%, while Massachusetts, Missouri and Iowa record none and score ~100%.
  // Same underlying fate, opposite ends of the table.
  const rows = report.data
    .slice()
    .sort((a, b) => (b.did_not_pass_pct ?? -1) - (a.did_not_pass_pct ?? -1));
  const degenerateCount = report.data.filter(
    (r) => r.terminal_split_is_degenerate
  ).length;

  return (
    <>
      <JsonLd
        data={{
          "@context": "https://schema.org",
          "@type": "Dataset",
          name: "How State Bills Die: 2026 Bill Mortality Report",
          description:
            "Per-state counts of enacted, killed, adjournment-died, and pending bills across all 50 US states and DC.",
          url: `${SITE_URL}/reports/2026-bill-mortality`,
          license: "https://creativecommons.org/licenses/by/4.0/",
          creator: { "@type": "Organization", name: "Bill Commons" },
          distribution: {
            "@type": "DataDownload",
            encodingFormat: "application/json",
            contentUrl: `${API_DOCS_URL}/api/v1/stats/mortality`,
          },
        }}
      />

      <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="Died on adjournment"
          value={`${totals.died_on_adjournment_pct ?? 0}%`}
          detail={`${fmt(totals.died_on_adjournment)} bills ran out of clock`}
          emphasis
        />
        <Stat
          label="Enacted"
          value={`${totals.enacted_pct ?? 0}%`}
          detail={`${fmt(totals.enacted)} bills became law`}
        />
        <Stat
          label="Actively killed"
          value={`${Math.round((1000 * totals.killed) / totals.total) / 10}%`}
          detail={`${fmt(totals.killed)} voted down, vetoed, or withdrawn`}
        />
        <Stat
          label="Tracked bills"
          value={fmt(totals.total)}
          detail="All 50 states + DC"
        />
      </div>

      <div className="mt-10 rounded-lg border-l-[3px] border-amber-500 bg-amber-50 px-4 py-3">
        <p className="text-sm text-slate-800">
          <strong>Read the last two columns together, not separately.</strong>{" "}
          Whether a bill that ran out of time lands in{" "}
          <em>Died on adjournment</em> or <em>Killed</em> depends on whether the
          legislature <em>files an action</em> recording the death — not on what
          happened to the bill. A clerk who files “Died in Committee” produces a
          recorded kill; a clerk who files nothing leaves it to the calendar.
          Both bills met the same end.
        </p>
        <p className="mt-2 text-sm text-slate-800">
          {degenerateCount} of the {report.data.length} jurisdictions here report{" "}
          <strong>zero</strong> bills in one of those two columns, which is a
          reporting convention rather than a legislative one. Compare states
          using <em>Did not pass</em>, which is unaffected; the split is
          meaningful only within a single state.
        </p>
      </div>

      <div className="surface-card mt-4 overflow-x-auto">
        <table className="data-table min-w-[820px]">
          <thead>
            <tr>
              <th>State</th>
              <th className="text-right">Bills</th>
              <th className="text-right">Enacted</th>
              <th className="text-right">Died on adjournment</th>
              <th className="text-right">Killed</th>
              <th className="text-right">Pending</th>
              <th className="text-right">Did not pass</th>
              <th className="text-right">Enact rate</th>
              <th className="text-right">Adjournment mortality</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.jurisdiction_code}>
                <td className="font-medium">
                  <Link
                    href={`/states/${row.jurisdiction_code}`}
                    className="text-blue-800 hover:text-blue-700 hover:underline"
                  >
                    {row.jurisdiction_name}
                  </Link>
                  {row.has_active_session ? (
                    <span className="ml-2 rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-medium text-emerald-800">
                      in session
                    </span>
                  ) : null}
                </td>
                <td className="text-right tabular-nums">{fmt(row.total)}</td>
                <td className="text-right tabular-nums">{fmt(row.enacted)}</td>
                <td className="text-right tabular-nums">
                  {fmt(row.died_on_adjournment)}
                </td>
                <td className="text-right tabular-nums">{fmt(row.killed)}</td>
                <td className="text-right tabular-nums">
                  {fmt(row.pending + row.unknown)}
                </td>
                <td className="text-right tabular-nums font-medium">
                  {row.did_not_pass_pct != null ? `${row.did_not_pass_pct}%` : "—"}
                </td>
                <td className="text-right tabular-nums">
                  {row.enacted_pct != null ? `${row.enacted_pct}%` : "—"}
                </td>
                <td className="text-right tabular-nums text-slate-600">
                  {row.died_on_adjournment_pct != null
                    ? `${row.died_on_adjournment_pct}%`
                    : "—"}
                  {row.terminal_split_is_degenerate ? (
                    <span
                      title="This jurisdiction reports zero bills in one of the two terminal buckets — the split reflects its filing convention, not its legislative behaviour."
                      className="ml-1 cursor-help text-amber-600"
                    >
                      *
                    </span>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="mt-4 max-w-3xl text-xs text-slate-500">
        States still in session (or whose adjournment date is unconfirmed)
        naturally show low adjournment mortality — their bills are counted as
        pending until the gavel falls. Expect these numbers to rise through
        the fall as more 2026 sessions adjourn.
      </p>
    </>
  );
}

function Stat({
  label,
  value,
  detail,
  emphasis,
}: {
  label: string;
  value: string;
  detail: string;
  emphasis?: boolean;
}) {
  return (
    <div
      className={`rounded-md border p-4 ${
        emphasis ? "border-amber-300 bg-amber-50" : "border-slate-200 bg-white"
      }`}
    >
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
        {label}
      </p>
      <p className="mt-1 text-2xl font-semibold tabular-nums text-slate-900">
        {value}
      </p>
      <p className="mt-1 text-xs text-slate-500">{detail}</p>
    </div>
  );
}
