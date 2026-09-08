"use client";

import { useMemo, useState } from "react";
import Link from "next/link";

interface Run {
  source_name: string;
  status: string;
  finished_at: string | null;
  observed_at: string | null;
}

interface JurisdictionHealth {
  jurisdiction: string;
  name: string;
  refresh_target: { cadence_tier: string | null; target_minutes: number | null };
  local_ingestion: { last_run: Run | null; last_successful_run: Run | null; last_successful_api_sync: Run | null };
  source_health: {
    dead_api_sync_jobs: number;
    queued_api_sync_jobs: number;
    running_api_sync_jobs: number;
  };
  parser_health: {
    bill_count: number;
    missing_parser_version: number;
    missing_source_name: number;
    missing_source_url: number;
    missing_retrieved_at: number;
  };
  coverage: { status: string | null; bill_count: number; full_text_count: number } | null;
  official_reconciliation: { state: string; reason: string };
}

interface Defect {
  severity: "critical" | "error" | "warning" | "info";
  code: string;
  jurisdiction: string;
  message: string;
  evidence: Record<string, unknown>;
}

export interface DataHealthData {
  report_version: number;
  generated_at: string;
  summary: { jurisdiction_count: number; defect_count: number };
  jurisdictions: JurisdictionHealth[];
  defects: Defect[];
}

function timestamp(value: string | null | undefined): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Not recorded"
    : `${date.toISOString().replace("T", " ").slice(0, 16)} UTC`;
}

function cadence(minutes: number | null) {
  if (minutes === null) return "No session target recorded";
  if (minutes >= 1440) return `${minutes / 1440} day${minutes === 1440 ? "" : "s"}`;
  if (minutes >= 60) return `${minutes / 60} hour${minutes === 60 ? "" : "s"}`;
  return `${minutes} minutes`;
}

export default function DataHealthReport({ report }: { report: DataHealthData }) {
  const [query, setQuery] = useState("");
  const [attentionOnly, setAttentionOnly] = useState(false);
  const byJurisdiction = useMemo(() => {
    const grouped = new Map<string, Defect[]>();
    for (const defect of report.defects) {
      grouped.set(defect.jurisdiction, [...(grouped.get(defect.jurisdiction) ?? []), defect]);
    }
    return grouped;
  }, [report.defects]);
  const rows = report.jurisdictions.filter((row) => {
    const matches = `${row.jurisdiction} ${row.name}`.toLowerCase().includes(query.trim().toLowerCase());
    return matches && (!attentionOnly || (byJurisdiction.get(row.jurisdiction)?.length ?? 0) > 0);
  });
  const affectedCount = report.jurisdictions.filter((row) => byJurisdiction.has(row.jurisdiction)).length;

  return (
    <section aria-label="Jurisdiction data health">
      <div className="border-y border-slate-200 py-5 text-sm leading-6 text-slate-700">
        <p>
          <strong className="font-semibold text-slate-950">{report.summary.jurisdiction_count} jurisdictions observed.</strong>{" "}
          {affectedCount} have reported ingestion or provenance issues.
        </p>
        <p className="mt-1 text-xs text-slate-600">
          Observation: <time dateTime={report.generated_at}>{timestamp(report.generated_at)}</time>.
          {" "}Reports refresh at most once every five minutes.
        </p>
      </div>

      <div className="flex flex-col gap-4 py-6 sm:flex-row sm:items-end sm:justify-between">
        <div className="w-full sm:max-w-xs">
          <label htmlFor="health-jurisdiction" className="block text-sm font-medium text-slate-900">
            Find a jurisdiction
          </label>
          <input id="health-jurisdiction" type="search" value={query}
            onChange={(event) => setQuery(event.target.value)} placeholder="State name or abbreviation"
            className="mt-2 w-full rounded-md border border-slate-400 bg-white px-3 py-2 text-sm text-slate-950 placeholder:text-slate-600" />
        </div>
        <label className="flex min-h-11 cursor-pointer items-center gap-2 text-sm text-slate-700">
          <input type="checkbox" checked={attentionOnly} onChange={(event) => setAttentionOnly(event.target.checked)}
            className="h-4 w-4 accent-blue-700" />
          Only jurisdictions with reported issues
        </label>
      </div>
      <p role="status" className="mb-3 text-xs text-slate-600">Showing {rows.length} of {report.jurisdictions.length} jurisdictions</p>

      {report.jurisdictions.length === 0 ? (
        <p className="border-y border-slate-200 py-8 text-sm text-slate-700">
          No jurisdictions have been reported yet. This is not evidence that ingestion is healthy.
        </p>
      ) : rows.length === 0 ? (
        <div className="border-y border-slate-200 py-8 text-sm text-slate-700">
          <p>No jurisdictions match these filters.</p>
          <button type="button" onClick={() => { setQuery(""); setAttentionOnly(false); }}
            className="mt-3 min-h-11 text-blue-800 underline underline-offset-2">Clear filters</button>
        </div>
      ) : (
        <div className="divide-y divide-slate-200 border-y border-slate-200">
          {rows.map((row) => {
            const defects = byJurisdiction.get(row.jurisdiction) ?? [];
            const success = row.local_ingestion.last_successful_api_sync;
            return (
              <article key={row.jurisdiction} className="py-6">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <h2 className="text-lg font-semibold tracking-tight text-slate-950">
                    <Link href={`/states/${row.jurisdiction}`} className="hover:text-blue-800 hover:underline">
                      {row.name}
                    </Link>{" "}<span className="text-sm font-normal text-slate-600">{row.jurisdiction}</span>
                  </h2>
                  <p className={`text-sm font-medium ${defects.length ? "text-amber-900" : "text-slate-600"}`}>
                    {defects.length ? `${defects.length} reported issue${defects.length === 1 ? "" : "s"}` : "No local issues reported"}
                  </p>
                </div>
                <dl className="mt-4 grid gap-x-8 gap-y-4 text-sm sm:grid-cols-2 lg:grid-cols-4">
                  <div><dt className="text-xs text-slate-600">Last successful incremental sync</dt>
                    <dd className="mt-1 tabular-nums text-slate-900">{timestamp(success?.finished_at ?? success?.observed_at)}</dd></div>
                  <div><dt className="text-xs text-slate-600">Scheduling target</dt>
                    <dd className="mt-1 text-slate-900">{row.refresh_target.target_minutes === null ? "" : "Every "}{cadence(row.refresh_target.target_minutes)}</dd></div>
                  <div><dt className="text-xs text-slate-600">Sync queue</dt>
                    <dd className="mt-1 tabular-nums text-slate-900">{row.source_health.queued_api_sync_jobs} queued · {row.source_health.running_api_sync_jobs} running</dd></div>
                  <div><dt className="text-xs text-slate-600">Official-source agreement</dt>
                    <dd className="mt-1 text-slate-900">Not yet verified</dd></div>
                </dl>
                <details className="mt-4 text-sm">
                  <summary className="w-fit cursor-pointer py-2 font-medium text-blue-800 hover:underline">
                    Inspect evidence{defects.length > 0 ? ` and ${defects.length} issue${defects.length === 1 ? "" : "s"}` : ""}
                  </summary>
                  <div className="mt-3 max-w-3xl space-y-4 leading-6 text-slate-700">
                    <p>{row.official_reconciliation.reason}</p>
                    <p>Stored bills: <span className="tabular-nums">{row.parser_health.bill_count.toLocaleString("en-US")}</span>.
                      {" "}Missing source links: {row.parser_health.missing_source_url.toLocaleString("en-US")}.
                      {" "}Missing parser versions: {row.parser_health.missing_parser_version.toLocaleString("en-US")}.
                      {" "}Dead sync jobs: {row.source_health.dead_api_sync_jobs.toLocaleString("en-US")}.</p>
                    {defects.length > 0 && <ul className="space-y-4">
                      {defects.map((defect, index) => <li key={`${defect.code}-${index}`}>
                        <p className="font-medium text-slate-900">{defect.message}</p>
                        <p className="mt-1 text-xs text-slate-600">{defect.code} · {defect.severity}</p>
                      </li>)}
                    </ul>}
                  </div>
                </details>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
