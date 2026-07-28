import Link from "next/link";
import type { Pagination } from "@/lib/types";

export default function PaginationNav({
  pagination,
  basePath,
  searchParams,
}: {
  pagination: Pagination;
  basePath: string;
  searchParams: Record<string, string | undefined>;
}) {
  const { page, total_pages } = pagination;
  if (total_pages <= 1) return null;

  const buildHref = (targetPage: number) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(searchParams)) {
      if (value) params.set(key, value);
    }
    params.set("page", String(targetPage));
    return `${basePath}?${params.toString()}`;
  };

  return (
    <nav
      aria-label="Pagination"
      className="mt-8 flex items-center justify-between border-t border-slate-200 pt-4 text-sm"
    >
      {page > 1 ? (
        <Link
          href={buildHref(page - 1)}
          className="rounded-md border border-slate-300 px-3 py-1.5 transition-colors hover:border-slate-400 hover:bg-slate-50"
        >
          ← Previous
        </Link>
      ) : (
        <span />
      )}
      <span className="tabular-nums text-slate-500">
        Page {page} of {total_pages}
      </span>
      {page < total_pages ? (
        <Link
          href={buildHref(page + 1)}
          className="rounded-md border border-slate-300 px-3 py-1.5 transition-colors hover:border-slate-400 hover:bg-slate-50"
        >
          Next →
        </Link>
      ) : (
        <span />
      )}
    </nav>
  );
}
