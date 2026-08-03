import type { Metadata } from "next";
import Link from "next/link";
import PageHeader from "@/components/PageHeader";

export const metadata: Metadata = {
  title: "Changelog & known limitations",
  description:
    "What changed in Bill Commons' status logic, coverage, and data semantics — and what this system still cannot tell you. Published so a consumer can tell a data change from a legislative one.",
  alternates: { canonical: "/changelog" },
};

interface Entry {
  date: string;
  title: string;
  body: React.ReactNode;
  impact: "semantics" | "coverage" | "fix" | "availability";
}

const IMPACT_STYLE: Record<Entry["impact"], { label: string; className: string }> = {
  semantics: {
    label: "changes what a field means",
    className: "bg-amber-100 text-amber-900",
  },
  coverage: { label: "coverage", className: "bg-blue-100 text-blue-900" },
  fix: { label: "correctness fix", className: "bg-emerald-100 text-emerald-800" },
  availability: { label: "availability", className: "bg-slate-200 text-slate-800" },
};

const ENTRIES: Entry[] = [
  {
    date: "2026-08-02",
    title: "Bills are no longer declared dead by a predicted adjournment date",
    impact: "semantics",
    body: (
      <>
        <code>died_on_adjournment</code> was applied once a session&apos;s end
        date passed. That date is the <strong>expected</strong> adjournment
        reported upstream — a prediction — and the rule never consulted the{" "}
        <code>active</code> flag in the same record. A chamber sitting longer
        than predicted therefore had its whole docket marked dead by the
        calendar. <strong>29,227 bills across 8 jurisdictions</strong> were
        affected, 18,343 in Massachusetts alone, and the national
        adjournment-death share falls from <strong>45.2% to 31.3%</strong>.
        <br />
        <br />
        The new rule: death requires the source to report the session closed.
        Where it still reports the session active, we publish the
        action-derived status if the chamber filed real legislative business in
        the last 30 days, and <strong>no status at all</strong> otherwise —
        7,861 bills in AZ, MS, SC and VA. Clerical filings do not count as
        evidence a chamber is sitting; South Carolina&apos;s were &quot;Act No.
        250&quot; and a scrivener&apos;s-error correction.{" "}
        <strong>
          If you compared adjournment mortality across dates or states before
          today, re-pull it.
        </strong>
      </>
    ),
  },
  {
    date: "2026-08-02",
    title: "Evidence packets are citable: permalink, snapshot id, downloadable JSON",
    impact: "fix",
    body: (
      <>
        A packet used to be a JSON blob in an agent transcript — nothing a
        reader could open, and no way to tell later whether the record had
        moved. Packets now carry a <code>how_to_cite</code> block: a one-line{" "}
        <code>cite_as</code> sentence, a human <strong>permalink</strong> at{" "}
        <code>/evidence/&#123;bill_id&#125;</code>, a JSON download, and a{" "}
        <code>snapshot_id</code> that changes if and only if a cited fact
        changes. <strong>Snapshots are not archived.</strong> The id is a
        change detector, not a way to retrieve the version you cited — so keep
        your own copy. The derived-status caveat rides inside the citation
        sentence itself, because a footnote is exactly where that distinction
        gets lost.
      </>
    ),
  },
  {
    date: "2026-08-02",
    title: "Offset pagination is bounded at 50,000 rows",
    impact: "fix",
    body: (
      <>
        <code>page</code> was bounded below and not above, so{" "}
        <code>?page=1000000</code> became <code>OFFSET 50,000,000</code> — and
        Postgres walks every one of those rows before discarding them. On a
        public, anonymous API that is a one-line denial of service that costs
        the caller nothing. The limit is on how <em>deep</em> one result set can
        be paged, not on how much data you can reach: filter by jurisdiction or
        session and page within that.
      </>
    ),
  },
  {
    date: "2026-08-02",
    title: "Usage figures no longer count our own uptime monitor",
    impact: "fix",
    body: (
      <>
        The read-path monitor calls a real MCP tool every two minutes on purpose
        — a handshake succeeds against a dead database, so nothing cheaper
        detects an outage. Within two hours of shipping usage telemetry, that
        monitor was <strong>65 of the 67 recorded tool calls</strong>, and{" "}
        <code>/stats/usage</code> was publishing the total while its own note
        claimed health probes were excluded. Monitor calls are now tagged at the
        MCP edge, excluded from every figure, and reported separately as{" "}
        <code>self_probe_calls</code> so the subtraction is checkable. The
        earlier rows were attributed rather than deleted: all 65 sit on an exact
        120-second cadence with no interleaved call.
      </>
    ),
  },
  {
    date: "2026-08-02",
    title: "Adjournment and clock-deaths are not comparable across states",
    impact: "semantics",
    body: (
      <>
        Whether a bill that ran out of time is recorded as <code>dead</code> or{" "}
        <code>died_on_adjournment</code> depends on whether the legislature files
        an action recording the death — not on what happened to the bill. Eleven
        jurisdictions report zero of one bucket; three report zero of the other.
        A new <code>did_not_pass</code> field (the sum) is the cross-state
        comparable figure, and{" "}
        <code>terminal_split_is_degenerate</code> flags states where the split
        carries no information. <strong>If you were comparing states on
        adjournment mortality, switch to <code>did_not_pass</code>.</strong> The
        headline 35% figure is unaffected.
      </>
    ),
  },
  {
    date: "2026-08-02",
    title: "Enrolled bills no longer wait forever",
    impact: "semantics",
    body: (
      <>
        A bill enrolled in a session that adjourned more than 180 days ago is no
        longer described as awaiting executive action — every state bounds that
        window at roughly 5–45 days from presentment. The status stays{" "}
        <code>enrolled</code>, but a new{" "}
        <code>enrolled_outcome_uncaptured</code> flag marks that the final
        signature or veto was never captured. This affected 3,274 of 4,918
        enrolled bills.
      </>
    ),
  },
  {
    date: "2026-08-02",
    title: "Ambiguous bill numbers are reported as ambiguous",
    impact: "fix",
    body: (
      <>
        <code>search_legislation</code>&apos;s bill-number path returned{" "}
        <code>match_type: bill_number_exact</code> however many sessions matched,
        in non-deterministic order, silently truncated. It now orders
        deterministically, limits in SQL, and returns{" "}
        <code>bill_number_ambiguous</code> with the candidate count when a number
        spans sessions. Texas HB 1 matches three.
      </>
    ),
  },
  {
    date: "2026-08-02",
    title: "Coverage warnings now fire for degraded jurisdictions",
    impact: "fix",
    body: (
      <>
        Coverage severity was ranked by lifecycle position, where the fault
        states sit after the healthy one — so a wholly degraded jurisdiction
        produced no warning at all, and a blocked session was hidden by any
        healthy sibling row. Every tool&apos;s empty-result path depended on
        that one function.
      </>
    ),
  },
  {
    date: "2026-08-02",
    title: "Evidence packets distinguish derived conclusions from official record",
    impact: "semantics",
    body: (
      <>
        <code>build_legislative_evidence_packet</code> labelled its record
        &quot;official&quot; over a payload containing the{" "}
        <em>derived</em> status — beside the state&apos;s own source URL, which
        does not contain that claim. Packets now name their{" "}
        <code>derived_fields</code>. An empty <code>hearings</code> section is
        labelled &quot;not collected&quot; rather than official.
      </>
    ),
  },
  {
    date: "2026-08-02",
    title: "Bill detail pages are no longer submitted for indexing",
    impact: "coverage",
    body: (
      <>
        Bill pages remain publicly addressable and are unchanged for API and
        direct-link use, but carry <code>noindex</code> and were removed from the
        sitemap. Search Console reported them as never reached; asking a search
        engine to evaluate 200,000 largely repetitive records was starving the
        pages worth citing.
      </>
    ),
  },
];

export default function ChangelogPage() {
  return (
    <div className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Changelog"
        title="What changed, and what we still can’t tell you"
        description={
          <p>
            Data semantics change. When they do, a consumer needs to tell{" "}
            <em>&quot;this bill moved&quot;</em> from{" "}
            <em>&quot;we changed how we read this bill&quot;</em> — those look
            identical in a diff and mean opposite things. Anything marked{" "}
            <strong>changes what a field means</strong> below is worth reading
            before you trust a comparison across that date.
          </p>
        }
      />

      <div className="mt-8 space-y-5">
        {ENTRIES.map((e, i) => {
          const style = IMPACT_STYLE[e.impact];
          return (
            <article key={i} className="surface-card p-5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="text-[15px] font-semibold text-slate-900">
                  {e.title}
                </h2>
                <span
                  className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ${style.className}`}
                >
                  {style.label}
                </span>
              </div>
              <time className="mt-0.5 block text-xs text-slate-500" dateTime={e.date}>
                {e.date}
              </time>
              <p className="mt-2 text-sm text-slate-700">{e.body}</p>
            </article>
          );
        })}
      </div>

      <section className="mt-12">
        <h2 className="text-lg font-semibold text-slate-900">
          Known limitations
        </h2>
        <p className="mt-2 text-sm text-slate-700">
          Standing gaps, stated plainly. These are not bugs awaiting a fix; they
          are the current edges of the system.
        </p>
        <ul className="mt-4 list-disc space-y-2 pl-5 text-sm text-slate-700">
          <li>
            <strong>No hearing data at all.</strong> A hearings tool exists and
            returns an empty list. That means &quot;not collected&quot;, never
            &quot;none scheduled&quot;.
          </li>
          <li>
            <strong>No federal legislation.</strong> 50 states and DC only.
          </li>
          <li>
            <strong>No historical sessions.</strong> Current session or biennium
            only — this is not an archive.
          </li>
          <li>
            <strong>Status is derived, not reported.</strong>{" "}
            <code>died_on_adjournment</code> in particular has no filed action
            behind it; the session simply ended. Cite it as our conclusion.
          </li>
          <li>
            <strong>Roughly 5% of bills carry no status</strong>, deliberately.
            States classify identical wording differently, so the derivation
            returns nothing rather than guess. <code>passed_both</code> is never
            assigned at all — no evidence source supports it.
          </li>
          <li>
            <strong>
              <code>status_date</code> is unpopulated
            </strong>{" "}
            for every bill. Date a status from the session end date, not from
            this field.
          </li>
          <li>
            <strong>Ambiguity is common.</strong> 981 (jurisdiction, identifier)
            pairs match more than one bill, 726 of them in Texas. Ambiguous is
            never reported as not-found.
          </li>
          <li>
            <strong>The change feed omits derivation changes.</strong> Real
            transitions emit events; a wholesale re-derivation of the status
            logic deliberately does not, so one maintenance run cannot flood
            every watchlist with changes that did not happen.
          </li>
        </ul>
        <p className="mt-4 text-sm text-slate-700">
          How these are tested is published as the{" "}
          <Link href="/quality" className="underline">
            data-integrity contract
          </Link>
          .
        </p>
      </section>
    </div>
  );
}
