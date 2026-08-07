import Link from "next/link";
import type { Metadata } from "next";
import DataUnavailable from "@/components/DataUnavailable";
import { apiGet } from "@/lib/api";
import type { BillVersion } from "@/lib/types";

interface Props {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ from?: string; to?: string }>;
}

interface DiffLine {
  type: "add" | "remove" | "context" | "meta";
  text: string;
}

interface CompareResponse {
  data: {
    bill_id: string;
    from_version_id: string;
    to_version_id: string;
    diff_lines: DiffLine[];
  };
  meta: unknown;
}

// GET /bills/{id}/versions returns a bare array (see
// billcommons_api.routers.bills.list_bill_versions), not an envelope.
// A diff between two FIXED version ids is immutable -- the underlying texts
// never change -- so this caches for a day rather than an hour. It is also the
// most expensive page on the site: /compare runs difflib over two full bill
// texts, and it is linked from every bill page, so crawlers reach it.
const COMPARE_REVALIDATE = 86_400;

// NOTE: apiGet's AbortSignal timeout only ever attaches to the UNCACHED
// (no-store) fetch path -- see lib/api.ts. Both calls below pass
// `revalidate`, so they take the cached path and get NO signal on their
// primary request; a `timeoutMs` option here would be dead code (caught in
// cross-family verify -- an earlier version of this diff set one and
// claimed a 25s bound that did not actually apply). /compare's own difflib
// run over two full bill texts is flagged by the comment above as "the most
// expensive page on the site," and a cold-cache request that hangs past the
// platform's function limit is still unbounded -- that is PRE-EXISTING
// behavior, unchanged by this fix batch, and a known follow-up (needs its
// own no-store-with-timeout path, or a real content-length/complexity guard
// on the API side, not a same-shaped timeoutMs option that silently does
// nothing on a cached call).
async function getVersions(id: string) {
  return apiGet<BillVersion[]>(`/api/v1/bills/${id}/versions`, undefined, {
    revalidate: COMPARE_REVALIDATE,
  });
}

async function getCompare(id: string, from?: string, to?: string) {
  if (!from || !to) return null;
  return apiGet<CompareResponse>(
    `/api/v1/bills/${id}/compare`,
    { from, to },
    { revalidate: COMPARE_REVALIDATE }
  );
}

export const metadata: Metadata = {
  title: "Compare bill versions",
};

export default async function ComparePage({ params, searchParams }: Props) {
  const { id } = await params;
  const sp = await searchParams;

  const versionsResult = await getVersions(id);
  const versions = versionsResult.ok ? versionsResult.data : [];

  const from = sp.from ?? versions[versions.length - 2]?.id;
  const to = sp.to ?? versions[versions.length - 1]?.id;

  const compareResult = from && to ? await getCompare(id, from, to) : null;

  return (
    <div className="mx-auto max-w-4xl px-4 py-12 sm:px-6">
      <nav aria-label="Breadcrumb" className="text-xs text-slate-500">
        <Link href={`/bills/${id}`} className="text-blue-800 hover:text-blue-700 hover:underline">
          Back to bill
        </Link>
      </nav>

      <header className="page-header mt-4">
        <p className="page-eyebrow">Bill text</p>
        <h1 className="page-title">Compare versions</h1>
      </header>

      {!versionsResult.ok ? (
        <div className="mt-6">
          <DataUnavailable message="Bill version data is temporarily unavailable." />
        </div>
      ) : versions.length < 2 ? (
        <p className="mt-6 text-sm text-slate-600">
          This bill does not have at least two versions with extracted text
          to compare.
        </p>
      ) : (
        <>
          <form className="mt-6 flex flex-wrap items-end gap-4" method="GET">
            <VersionSelect
              name="from"
              label="From version"
              versions={versions}
              selected={from}
            />
            <VersionSelect
              name="to"
              label="To version"
              versions={versions}
              selected={to}
            />
            <button
              type="submit"
              className="rounded-md bg-blue-700 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-800"
            >
              Compare
            </button>
          </form>

          <div className="mt-8">
            {!compareResult ? (
              <p className="text-sm text-slate-600">
                Select two versions to compare.
              </p>
            ) : !compareResult.ok ? (
              <DataUnavailable message="Version comparison is temporarily unavailable." />
            ) : (
              <pre className="overflow-x-auto rounded-md border border-slate-800 bg-slate-900 p-4 font-mono text-xs leading-relaxed">
                {compareResult.data.data.diff_lines.map((line, i) => (
                  <div
                    key={i}
                    className={
                      line.type === "add"
                        ? "bg-emerald-900/60 text-emerald-100"
                        : line.type === "remove"
                        ? "bg-red-900/60 text-red-100"
                        : line.type === "meta"
                        ? "text-slate-400"
                        : "text-slate-300"
                    }
                  >
                    {line.text}
                  </div>
                ))}
              </pre>
            )}
          </div>
        </>
      )}

      <p className="mt-8 text-xs text-slate-500">
        Diffs are computed deterministically from extracted bill text and
        show additions, deletions, and section moves. Scanned-PDF text
        extracted via OCR is flagged where confidence is low; see{" "}
        <Link href="/methodology" className="underline">
          methodology
        </Link>
        .
      </p>
    </div>
  );
}

function VersionSelect({
  name,
  label,
  versions,
  selected,
}: {
  name: string;
  label: string;
  versions: BillVersion[];
  selected?: string;
}) {
  return (
    <label className="text-sm">
      <span className="mb-1 block font-medium text-slate-700">{label}</span>
      <select
        name={name}
        defaultValue={selected}
        className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm transition-colors focus:border-blue-700"
      >
        {versions.map((v) => (
          <option key={v.id} value={v.id}>
            {v.note ?? v.id} {v.date ? `(${v.date})` : ""}
          </option>
        ))}
      </select>
    </label>
  );
}
