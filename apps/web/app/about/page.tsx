import type { Metadata } from "next";
import Link from "next/link";
import PageHeader from "@/components/PageHeader";

export const metadata: Metadata = {
  title: "About",
  description:
    "Bill Commons is a free, open-source, nonpartisan project to make state legislation searchable and accountable.",
  alternates: { canonical: "/about" },
};

export default function AboutPage() {
  return (
    <div className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="About the project"
        title="About Bill Commons"
        description={
          <p>
        Bill Commons is a free, public, nonpartisan search engine for state
        legislation across all 50 states and the District of Columbia. It
        exists because finding out what your state legislature is actually
        doing — which bills are moving, who sponsored them, how they voted,
        what changed between drafts — is harder than it should be, and
        often locked behind inconsistent or paywalled state systems.
          </p>
        }
      />

      <p className="text-[0.9375rem] leading-7 text-slate-700">
        The project provides a public website, a free REST API (60
        requests/minute, no key required for the anonymous tier), and a
        Model Context Protocol (MCP) server so AI assistants can search and
        cite legislation directly and honestly — with real source links,
        freshness timestamps, and explicit warnings when coverage for a
        jurisdiction is still thin.
      </p>

      <section className="mt-12 border-t border-slate-200 pt-8">
        <h2 className="text-lg font-semibold text-slate-900">
          Open source
        </h2>
        <p className="mt-2 text-slate-700">
          All original Bill Commons code is licensed under the{" "}
          <a
            href="https://www.apache.org/licenses/LICENSE-2.0"
            className="underline"
          >
            Apache License 2.0
          </a>
          . Third-party data licenses and attribution notices (including
          Open States&rsquo; public-domain data and, where used,
          LegiScan&rsquo;s CC BY 4.0 data) are preserved in full — see{" "}
          <Link href="/methodology" className="underline">
            methodology
          </Link>
          . No GPL-licensed scraper code is vendored into this repository.
        </p>
        <p className="mt-3 text-slate-700">
          Repository:{" "}
          <a
            href="https://github.com/GDACS-droid/billcommons"
            className="underline"
          >
            github.com/GDACS-droid/billcommons
          </a>{" "}
          <span className="text-sm text-slate-500">(placeholder — repo publishing in progress)</span>
        </p>
      </section>

      <section className="mt-10 border-t border-slate-200 pt-8">
        <h2 className="text-lg font-semibold text-slate-900">Contact</h2>
        <p className="mt-2 text-slate-700">
          For questions, corrections, or to report a data issue, see the
          contribution and security policies in the repository (
          <code className="rounded bg-slate-100 px-1 py-0.5 text-xs">
            CONTRIBUTING.md
          </code>
          ,{" "}
          <code className="rounded bg-slate-100 px-1 py-0.5 text-xs">
            SECURITY.md
          </code>
          ).
        </p>
      </section>
    </div>
  );
}
