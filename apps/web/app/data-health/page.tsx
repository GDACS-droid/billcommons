import type { Metadata } from "next";
import Link from "next/link";
import PageHeader from "@/components/PageHeader";
import DataUnavailable from "@/components/DataUnavailable";
import DataHealthReport, { type DataHealthData } from "@/components/DataHealthReport";
import { apiGet } from "@/lib/api";
import { API_BASE } from "@/lib/config";

export const metadata: Metadata = {
  title: "Data health",
  description: "Inspect Bill Commons ingestion recency, sync backlog, and source-provenance gaps by jurisdiction, with explicit verification limits.",
  alternates: { canonical: "/data-health" },
};

// The API bounds report generation with a five-minute cache. Do not add a
// second website cache that could conceal an API failure or extend its age.
export const dynamic = "force-dynamic";

export default async function DataHealthPage() {
  const result = await apiGet<DataHealthData>("/api/v1/data-health");
  const supported = result.ok && result.data.report_version === 1;
  return <div className="mx-auto max-w-6xl px-4 py-12 sm:px-6">
    <PageHeader title="Data health" description={<p>
      See when the ingestion pipeline last succeeded, where sync work is waiting,
      and which source records need attention.
    </p>} />
    <div className="mb-8 max-w-3xl text-sm leading-6 text-slate-700">
      <p><strong className="font-semibold text-slate-950">Local activity does not prove official freshness.</strong>{" "}
        This report reads Bill Commons pipeline records. It does not yet compare every jurisdiction
        with its latest official record. A recent ingest or an empty issue list is not a completeness guarantee.</p>
      <nav aria-label="Related data resources" className="mt-4 flex flex-wrap gap-x-5 gap-y-2 text-blue-800">
        <Link href="/coverage" className="underline underline-offset-2">Corpus coverage</Link>
        <Link href="/status" className="underline underline-offset-2">Service status</Link>
        <a href={new URL("/api/v1/data-health", API_BASE).toString()} className="underline underline-offset-2">Read the JSON report</a>
      </nav>
    </div>
    {supported && result.ok ? <DataHealthReport report={result.data} /> : <DataUnavailable
      message="The data-health report is unavailable."
      detail="No health verdict is available right now. Try reloading shortly, or check service status above." />}
  </div>;
}
