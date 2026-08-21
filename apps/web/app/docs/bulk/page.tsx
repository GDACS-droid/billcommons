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
        <p className="text-sm text-slate-600">
          Request a snapshot, or early access to API keys, via the{" "}
          <Link href="/feedback" className="underline">
            feedback form
          </Link>{" "}
          — tell us what you&apos;re building and how much data you need;
          that also shapes which tier we roll out first.
        </p>
      </section>
    </div>
  );
}
