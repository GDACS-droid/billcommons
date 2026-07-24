import type { Metadata } from "next";
import SearchBox from "@/components/SearchBox";
import DataUnavailable from "@/components/DataUnavailable";
import BillListItem from "@/components/BillListItem";
import PaginationNav from "@/components/PaginationNav";
import { apiGet } from "@/lib/api";
import type { ListEnvelope, SearchResult } from "@/lib/types";

interface Props {
  searchParams: Promise<{
    q?: string;
    jurisdiction?: string;
    session?: string;
    chamber?: string;
    status?: string;
    sponsor?: string;
    subject?: string;
    committee?: string;
    date_from?: string;
    date_to?: string;
    sort?: string;
    page?: string;
  }>;
}

export async function generateMetadata({ searchParams }: Props): Promise<Metadata> {
  const sp = await searchParams;
  return {
    title: sp.q ? `Search: ${sp.q}` : "Search",
    robots: sp.q ? { index: false, follow: true } : undefined,
  };
}

async function search(sp: Awaited<Props["searchParams"]>) {
  const page = Number(sp.page ?? "1") || 1;
  return apiGet<ListEnvelope<SearchResult>>("/api/v1/search", {
    q: sp.q,
    jurisdiction: sp.jurisdiction,
    session: sp.session,
    chamber: sp.chamber,
    status: sp.status,
    sponsor: sp.sponsor,
    subject: sp.subject,
    committee: sp.committee,
    date_from: sp.date_from,
    date_to: sp.date_to,
    sort: sp.sort,
    page,
    per_page: 20,
  });
}

export default async function SearchPage({ searchParams }: Props) {
  const sp = await searchParams;
  const hasQuery = Boolean(
    sp.q ||
      sp.jurisdiction ||
      sp.session ||
      sp.chamber ||
      sp.status ||
      sp.sponsor ||
      sp.subject ||
      sp.committee ||
      sp.date_from ||
      sp.date_to
  );

  const result = hasQuery ? await search(sp) : null;

  return (
    <div className="mx-auto max-w-4xl px-4 py-10 sm:px-6">
      <h1 className="text-2xl font-semibold text-slate-900">
        Search legislation
      </h1>
      <div className="mt-4">
        <SearchBox defaultValue={sp.q ?? ""} autoFocus={!hasQuery} />
      </div>

      <div className="mt-8">
        {!hasQuery ? (
          <p className="text-sm text-slate-600">
            Search by bill number (e.g. &ldquo;HB 123&rdquo;), keyword, phrase, sponsor
            name, subject, or committee. Add <code>&amp;jurisdiction=NC</code>{" "}
            or other filters to the URL to share a filtered search.
          </p>
        ) : !result ? null : !result.ok ? (
          <DataUnavailable message="Search is temporarily unavailable." />
        ) : result.data.data.length === 0 ? (
          <p className="text-sm text-slate-600">
            No results found for this search.
          </p>
        ) : (
          <>
            <p className="text-sm text-slate-500">
              {result.data.pagination.total.toLocaleString()} results
            </p>
            <ul className="mt-3 space-y-3">
              {result.data.data.map((bill) => (
                <BillListItem key={bill.id} bill={bill} />
              ))}
            </ul>
            <PaginationNav
              pagination={result.data.pagination}
              basePath="/search"
              searchParams={{
                q: sp.q,
                jurisdiction: sp.jurisdiction,
                session: sp.session,
                chamber: sp.chamber,
                status: sp.status,
                sponsor: sp.sponsor,
                subject: sp.subject,
                committee: sp.committee,
                date_from: sp.date_from,
                date_to: sp.date_to,
                sort: sp.sort,
              }}
            />
          </>
        )}
      </div>
    </div>
  );
}
