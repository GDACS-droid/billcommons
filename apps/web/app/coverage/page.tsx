import Link from "next/link";
import type { Metadata } from "next";
import DataUnavailable from "@/components/DataUnavailable";
import PageHeader from "@/components/PageHeader";
import { CoverageBadge } from "@/components/StatusBadge";
import { apiGet } from "@/lib/api";
import type { CoverageRow, ListEnvelope } from "@/lib/types";

export const metadata: Metadata = {
  title: "Coverage status",
  description:
    "Public coverage matrix for Bill Commons: per-jurisdiction ingestion status, bill counts, full-text rate, and validation results.",
  alternates: { canonical: "/coverage" },
};

async function getCoverage() {
  return apiGet<ListEnvelope<CoverageRow>>("/api/v1/coverage", {
    per_page: 200,
  });
}

export default async function CoveragePage() {
  const result = await getCoverage();

  return (
    <div className="mx-auto max-w-6xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Data quality"
        title="Coverage status"
        description={
          <p>
        Bill Commons publishes its ingestion status for every jurisdiction —
        no black boxes. A jurisdiction reaches <strong>GREEN</strong> only
        after its session is identified from an authoritative source, all
        discoverable bills are imported, full text is searchable wherever
        available, and validation samples pass. See{" "}
        <Link href="/methodology" className="underline">
          methodology
        </Link>{" "}
        for the full criteria.
          </p>
        }
      />

      <div className="surface-card overflow-x-auto">
        {!result.ok ? (
          <DataUnavailable message="Coverage data is temporarily unavailable." />
        ) : result.data.data.length === 0 ? (
          <p className="text-sm text-slate-600">
            No coverage data reported yet.
          </p>
        ) : (
          <table className="data-table min-w-[900px]">
            <thead>
              <tr>
                <th>Jurisdiction</th>
                <th>Session</th>
                <th className="text-right">Bills</th>
                <th className="text-right">Full text</th>
                <th className="text-right">Of obtainable</th>
                <th>Last update</th>
                <th>Source</th>
                <th>Validation</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {result.data.data
                .slice()
                .sort((a, b) =>
                  a.jurisdiction_name.localeCompare(b.jurisdiction_name)
                )
                .map((row) => (
                  <tr
                    key={row.jurisdiction_code}
                    className="transition-colors"
                  >
                    <td className="font-medium">
                      <Link
                        href={`/states/${row.jurisdiction_code}`}
                        className="hover:underline"
                      >
                        {row.jurisdiction_name}
                      </Link>
                    </td>
                    <td className="text-slate-600">
                      {row.session_identifier ?? "—"}
                      {row.session_status ? ` (${row.session_status})` : ""}
                    </td>
                    <td className="text-right tabular-nums text-slate-600">
                      {row.bill_count?.toLocaleString() ?? "—"}
                    </td>
                    <td className="text-right tabular-nums text-slate-600">
                      {row.full_text_pct != null
                        ? `${row.full_text_pct}%`
                        : "—"}
                    </td>
                    <td className="text-right tabular-nums text-slate-600">
                      {row.full_text_of_available_pct != null ? (
                        `${row.full_text_of_available_pct}%`
                      ) : row.full_text_available_count === 0 ? (
                        <span title="No full text is obtainable from this source; bills remain metadata-searchable.">
                          none published
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="text-slate-600">
                      {row.last_update ?? "—"}
                    </td>
                    <td className="text-slate-600">
                      {row.source_name ?? "—"}
                    </td>
                    <td className="tabular-nums text-slate-600">
                      {row.validation_sample
                        ? `${row.validation_sample} (${
                            row.validation_pass_rate != null
                              ? `${row.validation_pass_rate}%`
                              : "—"
                          })`
                        : "—"}
                    </td>
                    <td>
                      <CoverageBadge status={row.status} />
                      {row.known_gaps?.length ? (
                        <p className="mt-1 text-xs text-slate-400">
                          {row.known_gaps.join("; ")}
                        </p>
                      ) : null}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
