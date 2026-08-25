import type { Metadata } from "next";
import Link from "next/link";
import PageHeader from "@/components/PageHeader";

export const metadata: Metadata = {
  title: "Terms",
  description:
    "Bill Commons data terms: attribution, no-resale on snapshot files, refund policy, and 90-day price grandfathering.",
  alternates: { canonical: "/terms" },
};

export default function TermsPage() {
  return (
    <div className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Legal"
        title="Data terms"
        description={
          <p>
            The underlying legislative data is public record — Bill
            Commons claims no copyright over bill text, actions, or votes.
            These terms cover the service built on top of it: attribution,
            what a snapshot purchase does and doesn&apos;t let you do, and
            refunds. See{" "}
            <Link href="/methodology" className="underline">
              methodology
            </Link>{" "}
            for source attribution and the software license.
          </p>
        }
      />

      <section className="mt-10 space-y-8 text-sm leading-6 text-slate-700">
        <div>
          <h2 className="text-lg font-semibold text-slate-950">
            No copyright claim over public records
          </h2>
          <p className="mt-2">
            Legislative bill text, actions, sponsorships, and votes are, in
            general, public-domain works of state and federal government
            bodies in the United States. What a paid plan or a snapshot
            purchase pays for is <strong>access, normalization, and
            delivery</strong> — a maintained API, a documented schema, a
            working search index, a bulk export you don&apos;t have to build
            yourself. Attribution and no-resale below are contract terms of
            the service, not a copyright claim over the facts.
          </p>
        </div>

        <div>
          <h2 className="text-lg font-semibold text-slate-950">
            Attribution
          </h2>
          <p className="mt-2">
            Attribute Bill Commons wherever you display or publish data
            from this API or a snapshot: <em>Data via Bill Commons
            (billcommons.org)</em>. Where a record originated with Open
            States (Plural Policy), their own attribution obligation
            passes through.
          </p>
        </div>

        <div>
          <h2 className="text-lg font-semibold text-slate-950">
            Snapshot files: no resale, derived works fine
          </h2>
          <p className="mt-2">
            Snapshot files may not be redistributed or resold as-is.
            Building a product, analysis, or report on top of the data is
            fine — attribute it. Redistribution rights are available on an
            Enterprise agreement.
          </p>
        </div>

        <div>
          <h2 className="text-lg font-semibold text-slate-950">
            API keys
          </h2>
          <p className="mt-2">
            An API key is issued to one customer. Don&apos;t share a key
            across unrelated organizations or resell access to your
            key&apos;s quota. Unusually broad sharing gets flagged for a
            conversation, not an automatic block.
          </p>
        </div>

        <div>
          <h2 className="text-lg font-semibold text-slate-950">Refunds</h2>
          <p className="mt-2">
            <strong>Subscriptions</strong> (Builder, Scale): full refund
            within 7 days of the charge, prorated after. Use the
            &ldquo;Manage billing&rdquo; button on{" "}
            <Link href="/account" className="underline">
              your account page
            </Link>
            , which opens the Stripe customer portal.
          </p>
          <p className="mt-2">
            <strong>Snapshots</strong> (one-time state or full-corpus
            purchase): refundable until the download link is sent — once
            delivered, the purchase is final.
          </p>
        </div>

        <div>
          <h2 className="text-lg font-semibold text-slate-950">
            90-day price grandfathering
          </h2>
          <p className="mt-2">
            Anyone who becomes a paying customer within the first 90 days
            of Builder or Scale going on sale keeps their price for as
            long as their subscription stays active and in good standing.
          </p>
        </div>

        <div>
          <h2 className="text-lg font-semibold text-slate-950">
            Anonymous use
          </h2>
          <p className="mt-2">
            Anonymous (keyless) API access stays free, subject to a
            per-IP/per-network daily request cap — see{" "}
            <Link href="/docs/api-keys" className="underline">
              API keys
            </Link>{" "}
            for the current numbers. This is not a paywall on ordinary
            search or reasonable API use; it&apos;s an abuse ceiling.
          </p>
        </div>

        <div>
          <h2 className="text-lg font-semibold text-slate-950">Contact</h2>
          <p className="mt-2">
            Questions about these terms, a refund, or a licensing deal:{" "}
            <a href="mailto:sales@billcommons.org" className="underline">
              sales@billcommons.org
            </a>
            .
          </p>
        </div>
      </section>
    </div>
  );
}
