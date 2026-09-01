import type { Metadata } from "next";
import Link from "next/link";
import CheckoutButton from "@/components/CheckoutButton";
import PageHeader from "@/components/PageHeader";
import SnapshotCheckoutButton from "@/components/SnapshotCheckoutButton";

export const metadata: Metadata = {
  title: "Bulk access",
  description:
    "API keys with higher-volume tiers and full-corpus snapshots for anyone who needs more than the free public API's per-request rate limits.",
  alternates: { canonical: "/docs/bulk" },
};

const TIERS: { name: string; price: string; reqDay: string; heavyDay: string; note?: string }[] = [
  // Item 18 fix: SPEC-LOCKED R1 defines the anonymous daily caps as ONLY
  // "2,000/IP, 5,000 per /24" -- there is no anonymous heavy DAILY limit in
  // the spec or the code (the only heavy enforcement for anonymous callers
  // is the per-MINUTE tier). "200/IP" here previously advertised a number
  // that was neither specified nor enforced.
  { name: "Anonymous", price: "$0", reqDay: "2,000/IP, 5,000/24", heavyDay: "—" },
  { name: "Developer", price: "$0 (free key)", reqDay: "5,000", heavyDay: "500" },
  { name: "Builder", price: "$49/mo", reqDay: "50,000", heavyDay: "5,000" },
  {
    name: "Scale",
    price: "$299/mo",
    reqDay: "500,000",
    heavyDay: "100,000",
    note: "nightly snapshots (manual delivery until the automated builder ships)",
  },
  { name: "Enterprise", price: "from $1,500/mo", reqDay: "custom", heavyDay: "custom" },
];

export default function BulkAccessPage() {
  return (
    <div className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Developer access"
        title="Bulk access"
        description={
          <p>
            The free public API is rate-limited per IP, which is the right
            default for occasional lookups and small monitors but the wrong
            fit for a bulk crawl or a full-corpus mirror. One download
            replaces hundreds of thousands of paginated requests.
          </p>
        }
      />

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-950">
          Full-corpus snapshots
        </h2>
        <p className="mt-2 text-sm text-slate-600">
          A one-time export of the entire dataset — <code>bills</code>,{" "}
          <code>bill_versions</code>, <code>bill_documents</code>,{" "}
          <code>document_text</code>, <code>bill_actions</code>,{" "}
          <code>sponsorships</code>, <code>vote_events</code>,{" "}
          <code>vote_records</code> — as Parquet files (zstd-compressed),
          full corpus plus a per-state breakdown, each with a manifest (row
          counts, sha256, generated-at) and an attribution file. Delivered
          within one business day by a signed download link.
        </p>
        <div className="code-block mt-4">
          <pre>
            <code>{`-- read a snapshot with DuckDB, no import step
SELECT identifier, title, status
FROM 'bills.parquet'
WHERE jurisdiction = 'FL'
ORDER BY updated_at DESC
LIMIT 20;`}</code>
          </pre>
        </div>
        <p className="mt-4 text-sm text-slate-600">
          <strong>State snapshot — $99 once.</strong> One jurisdiction, 30-day
          window including in-window refreshes.
          <br />
          <strong>Full corpus — $499 once.</strong> All 51 jurisdictions,
          same terms.
        </p>
        <p className="mt-4 text-sm text-slate-600">
          <strong>Attribution required:</strong> credit as{" "}
          <em>Data via Bill Commons (billcommons.org)</em>; Open States&apos;
          own attribution requirement passes through. Snapshot files may not
          be redistributed or resold as-is — derived works are fine;
          redistribution rights are available on Enterprise. See{" "}
          <Link href="/methodology" className="underline">
            methodology
          </Link>{" "}
          for the full data-terms.
        </p>
        <p className="mt-4 text-sm text-slate-600">
          <strong>Refunds:</strong> full refund until the download link is
          sent; not refundable after delivery. See{" "}
          <Link href="/terms" className="underline">
            data terms
          </Link>
          .
        </p>
        <div className="mt-6 flex flex-wrap gap-3">
          <SnapshotCheckoutButton scope="full" label="Full corpus — $499 once" />
          <SnapshotCheckoutButton
            scope="state"
            jurisdiction="FL"
            label="One state — $99 once"
            className="inline-block rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
          />
        </div>
        <p className="mt-2 text-xs text-slate-500">
          Need a state other than Florida?{" "}
          <Link href="/feedback" className="underline">
            Tell us which one
          </Link>
          .
        </p>
      </section>

      <section className="mt-12">
        <h2 className="text-lg font-semibold text-slate-950">API keys</h2>
        <p className="mt-2 text-sm text-slate-600">
          For ongoing programmatic access rather than a one-time export, get
          an{" "}
          <Link href="/docs/api-keys" className="underline">
            API key
          </Link>
          . A free Developer key already raises your limit past the
          anonymous per-IP ceiling; paid tiers go well beyond that.
        </p>
        <div className="mt-4 overflow-x-auto rounded-md border border-slate-200">
          <table className="w-full text-left text-sm">
            <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-4 py-2">Tier</th>
                <th className="px-4 py-2">Price</th>
                <th className="px-4 py-2">Requests/day</th>
                <th className="px-4 py-2">Heavy/day</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {TIERS.map((t) => (
                <tr key={t.name}>
                  <td className="px-4 py-2 font-medium text-slate-900">{t.name}</td>
                  <td className="px-4 py-2 text-slate-600">{t.price}</td>
                  <td className="px-4 py-2 text-slate-600">{t.reqDay}</td>
                  <td className="px-4 py-2 text-slate-600">{t.heavyDay}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {TIERS.filter((t) => t.note).map((t) => (
          <p key={t.name} className="mt-3 text-xs text-slate-500">
            {t.name}: {t.note}.
          </p>
        ))}
        <p className="mt-4 text-sm text-slate-600">
          First 90 days of paying customers are grandfathered at today&apos;s
          prices. Annual billing is 10× the monthly rate.
        </p>
      </section>

      <section className="mt-8">
        <h2 className="text-base font-semibold text-slate-900">
          Ready to start?
        </h2>
        <p className="mt-2 text-sm text-slate-600">
          Checkout is handled by Bill Commons and Stripe. No account is
          required to begin; purchases are linked to the email entered at
          checkout, and snapshot delivery follows by email within one business
          day.
        </p>
        <div className="mt-4 flex flex-wrap gap-3">
          <SnapshotCheckoutButton scope="full" label="Full corpus — $499 once" />
          <CheckoutButton
            plan="scale"
            interval="monthly"
            label="Scale API — $299/month"
            className="rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
          />
        </div>
      </section>

      <section className="mt-8">
        <p className="text-sm text-slate-600">
          Questions, or need something these tiers don&apos;t cover? Tell us
          via the{" "}
          <Link href="/feedback" className="underline">
            feedback form
          </Link>
          .
        </p>
      </section>
    </div>
  );
}
