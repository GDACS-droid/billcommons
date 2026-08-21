import type { Metadata } from "next";
import Link from "next/link";
import PageHeader from "@/components/PageHeader";

export const metadata: Metadata = {
  title: "Bulk access",
  description:
    "API keys with higher-volume tiers and full-corpus snapshots for anyone who needs more than the free public API's per-request rate limits.",
  alternates: { canonical: "/docs/bulk" },
};

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
            fit for a bulk crawl or a full-corpus mirror.
          </p>
        }
      />

      <section className="mt-10">
        <p className="text-sm text-slate-600">
          API keys with higher-volume tiers are rolling out: a key raises
          your rate limit well beyond the anonymous per-IP ceiling and lets
          us tell your traffic apart from anonymous scraping instead of
          throttling both the same way.
        </p>
      </section>

      <section className="mt-8">
        <p className="text-sm text-slate-600">
          For a one-time or infrequent need, a full-corpus snapshot — the
          entire dataset as a single export — is usually a better fit than
          paginating the live API at all, and is available now, on request:
          it&apos;s delivered within one business day as a signed download
          link.
        </p>
      </section>

      <section className="mt-8">
        <h2 className="text-base font-semibold text-slate-900">Buy now</h2>
        <ul className="mt-3 space-y-2 text-sm text-slate-700">
          <li>
            <a
              href="https://checkout.frontlinehq.vote/b/3cIdRbbp620l4wT3qJco005"
              className="underline font-medium"
            >
              Full-corpus snapshot — $499 one-time
            </a>{" "}
            — all 50 states + DC: bills, versions, full text, actions, votes,
            as Parquet with a manifest. Signed download link within one
            business day. Refundable until the link is sent.
          </li>
          <li>
            <a
              href="https://checkout.frontlinehq.vote/b/3cI6oJ50IcEZ5AXf9rco006"
              className="underline font-medium"
            >
              Scale API access — $299/month
            </a>{" "}
            — 500,000 requests/day, 100,000 heavy (full-text/diff) requests/day,
            nightly snapshots included. API key delivered within one business
            day.
          </li>
        </ul>
        <p className="mt-3 text-sm text-slate-600">
          Need a single state, a different volume, or redistribution rights?
          Use the{" "}
          <Link href="/feedback" className="underline">
            feedback form
          </Link>{" "}
          and say what you&apos;re building.
        </p>
      </section>
    </div>
  );
}
