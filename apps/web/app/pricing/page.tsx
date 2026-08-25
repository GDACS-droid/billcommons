import type { Metadata } from "next";
import Link from "next/link";
import PageHeader from "@/components/PageHeader";
import PricingTiersTable from "@/components/PricingTiersTable";
import SnapshotCheckoutButton from "@/components/SnapshotCheckoutButton";

export const metadata: Metadata = {
  title: "Pricing",
  description:
    "Bill Commons pricing: a free public API and MCP server, paid Builder/Scale API tiers, and one-time bulk data snapshots.",
  alternates: { canonical: "/pricing" },
};

const FAQ: { q: string; a: string }[] = [
  {
    q: "What counts as a request?",
    a: "Any non-exempt API request or MCP tool call. A \"heavy\" request (bill list, full bill, versions, compare, search) also counts against a separate, smaller daily heavy-route limit.",
  },
  {
    q: "What happens when I hit the cap?",
    a: "A 429 with a Retry-After header (seconds to the next UTC midnight for daily quota, or to the next minute for burst) and an error.code of quota_exceeded or rate_limited. There's also a silent 10% grace above the stated limit so an in-progress job finishes instead of half-failing.",
  },
  {
    q: "Refunds?",
    a: "Subscriptions: full refund within 7 days of the charge, prorated after. Snapshots: refundable until the download link is sent, not after. See /terms.",
  },
  {
    q: "Can I redistribute a snapshot?",
    a: "Not as-is. Building a product or analysis on top of the data is fine and encouraged (with attribution) — reselling the raw files needs an Enterprise agreement. See /terms.",
  },
];

export default function PricingPage() {
  return (
    <div className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Pricing"
        title="Pricing"
        description={
          <p>
            Anonymous access to the public API and MCP server stays free —
            volume and full-corpus bulk data are what fund the ingest
            pipeline. One snapshot download replaces roughly 720,000 paginated
            requests, which is the actual pitch for anyone running a crawl.
          </p>
        }
      />

      <PricingTiersTable />

      <p className="mt-4 text-xs text-slate-500">
        Annual billing is 10× the monthly rate. Anyone who becomes a paying
        customer in the first 90 days is grandfathered at today&apos;s
        prices — see{" "}
        <Link href="/terms" className="underline">
          data terms
        </Link>
        .
      </p>

      <section className="mt-12">
        <h2 className="text-lg font-semibold text-slate-950">
          One-time bulk snapshots
        </h2>
        <p className="mt-2 text-sm text-slate-600">
          A full export of the dataset as Parquet files, delivered by a
          signed link within one business day — see{" "}
          <Link href="/docs/bulk" className="underline">
            /docs/bulk
          </Link>{" "}
          for the file list and schema.
        </p>
        <div className="mt-4 flex flex-wrap gap-3">
          <SnapshotCheckoutButton scope="full" label="Full corpus — $499 once" />
        </div>
        <p className="mt-2 text-xs text-slate-500">
          Need a single state ($99)? Pick a state from{" "}
          <Link href="/docs/bulk" className="underline">
            /docs/bulk
          </Link>
          .
        </p>
      </section>

      <section className="mt-12">
        <h2 className="text-lg font-semibold text-slate-950">
          Why we charge
        </h2>
        <p className="mt-2 text-sm text-slate-600">
          Anonymous access stays free — that&apos;s not changing. Volume
          traffic and bulk exports are what pay for the ingest pipeline
          that keeps the free tier free. If you&apos;re doing occasional
          lookups or building a small tool, you&apos;ll likely never pay
          us anything.
        </p>
      </section>

      <section className="mt-12">
        <h2 className="text-lg font-semibold text-slate-950">FAQ</h2>
        <dl className="mt-4 space-y-5">
          {FAQ.map((item) => (
            <div key={item.q}>
              <dt className="text-sm font-medium text-slate-900">{item.q}</dt>
              <dd className="mt-1 text-sm text-slate-600">{item.a}</dd>
            </div>
          ))}
        </dl>
      </section>
    </div>
  );
}
