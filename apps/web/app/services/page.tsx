import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: "Custom tracking & consulting",
  description:
    "Bill Commons is free and open source. The team behind it builds custom legislative tracking, integrations, alerting pipelines, and private instances for policy teams, government-affairs shops, and lobbying firms.",
  alternates: { canonical: "/services" },
};

const OFFERINGS: { title: string; body: string }[] = [
  {
    title: "Custom watchlist pipelines",
    body: "Your bill list, wired into your tools: hourly status monitoring over the change feed, alerts into Slack/email/your CRM, weekly briefing documents generated from real legislative movement.",
  },
  {
    title: "Custom topic trackers",
    body: "The public trackers cover AI, privacy, and crypto. We build private ones for your issue area — healthcare, energy, housing, licensing, anything — tuned with you for precision, refreshed nightly.",
  },
  {
    title: "Integrations & data feeds",
    body: "Bulk exports, direct database feeds, custom API endpoints, or a private instance with your own SLAs. If your team already has engineers, we get them productive against the API in a day.",
  },
  {
    title: "AI agent setups",
    body: "We connect Claude or your agent stack to legislative data via MCP and build the workflows on top — automated audits of your tracker, draft testimony research, reintroduction-candidate scans.",
  },
];

export default function ServicesPage() {
  return (
    <div className="mx-auto max-w-4xl px-4 py-10 sm:px-6">
      <p className="text-xs font-semibold uppercase tracking-wide text-amber-600">
        Services
      </p>
      <h1 className="mt-1 text-2xl font-semibold text-slate-900">
        Custom tracking &amp; consulting
      </h1>
      <p className="mt-3 max-w-2xl text-slate-600">
        Bill Commons is free and stays free. It&apos;s built and operated by{" "}
        <a href="https://gdacs.net" className="underline">
          GDACS
        </a>
        , a data consulting shop — and the same team takes on custom work for
        policy teams, government-affairs shops, and lobbying firms that need
        more than the public site.
      </p>

      <div className="mt-8 grid gap-4 sm:grid-cols-2">
        {OFFERINGS.map((o) => (
          <div key={o.title} className="rounded-lg border border-slate-200 p-5">
            <h2 className="font-semibold text-slate-900">{o.title}</h2>
            <p className="mt-2 text-sm text-slate-600">{o.body}</p>
          </div>
        ))}
      </div>

      <section className="mt-10 rounded-lg border border-slate-200 bg-slate-50 p-6">
        <h2 className="text-lg font-semibold text-slate-900">
          Case study: the 139-bill audit
        </h2>
        <p className="mt-2 text-sm text-slate-600">
          A tech-policy monitoring team ran their full 139-bill watchlist
          through Bill Commons. One batch request resolved 130 of them;
          the comparison surfaced that three bills their previous tracking
          had marked &quot;advancing&quot; were actually dead — the sessions
          had adjourned. That finding led us to build{" "}
          <Link href="/reports/2026-bill-mortality" className="underline">
            adjournment-aware status
          </Link>{" "}
          into the public dataset, and led them to an automated hourly
          monitor over the change feed instead of manual re-checking.
        </p>
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-900">Talk to us</h2>
        <p className="mt-2 text-sm text-slate-600">
          Email{" "}
          <a href="mailto:alberto@gdacs.net?subject=Bill%20Commons%20consulting" className="font-medium underline">
            alberto@gdacs.net
          </a>{" "}
          with a couple of lines about what you&apos;re tracking and what
          should happen when it moves. We reply fast, and the first
          conversation is free.
        </p>
      </section>
    </div>
  );
}
