import Link from "next/link";
import type { Metadata } from "next";
import DataUnavailable from "@/components/DataUnavailable";
import BillListItem from "@/components/BillListItem";
import PaginationNav from "@/components/PaginationNav";
import { apiGet } from "@/lib/api";
import { billPath } from "@/lib/billUrl";
import type { BillSummary, ListEnvelope } from "@/lib/types";

interface Props {
  params: Promise<{ code: string; session: string }>;
  searchParams: Promise<{ page?: string; chamber?: string }>;
}

async function getBills(
  code: string,
  session: string,
  page: number,
  chamber?: string
) {
  return apiGet<ListEnvelope<BillSummary>>(
    "/api/v1/bills",
    { jurisdiction: code, session, chamber, page, per_page: 25 },
    // Crawlable listing page: cached so a bulk crawl of the session index does
    // not replay the same query per request.
    { revalidate: 1800 }
  );
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { code, session } = await params;
  const upperCode = code.toUpperCase();
  return {
    title: `${upperCode} — ${decodeURIComponent(session)}`,
    description: `Bills filed during the ${decodeURIComponent(
      session
    )} session in ${upperCode}.`,
    alternates: {
      canonical: `/states/${upperCode}/sessions/${session}`,
    },
  };
}

export default async function SessionPage({ params, searchParams }: Props) {
  const { code, session } = await params;
  const sp = await searchParams;
  const upperCode = code.toUpperCase();
  const page = Number(sp.page ?? "1") || 1;

  const billsResult = await getBills(upperCode, session, page, sp.chamber);

  return (
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
      <nav aria-label="Breadcrumb" className="text-sm text-slate-500">
        <Link href="/states" className="hover:underline">
          States
        </Link>{" "}
        /{" "}
        <Link href={`/states/${upperCode}`} className="hover:underline">
          {upperCode}
        </Link>{" "}
        / {decodeURIComponent(session)}
      </nav>

      <h1 className="mt-2 text-2xl font-semibold text-slate-900">
        {upperCode} — {decodeURIComponent(session)}
      </h1>

      <section className="mt-8">
        {!billsResult.ok ? (
          <DataUnavailable message="Session bill data is temporarily unavailable." />
        ) : billsResult.data.data.length === 0 ? (
          <p className="text-sm text-slate-600">
            No bills found for this session yet.
          </p>
        ) : (
          <>
            <p className="text-sm text-slate-500">
              {billsResult.data.pagination.total.toLocaleString()} bills
            </p>
            <ul className="mt-3 space-y-3">
              {billsResult.data.data.map((bill) => (
                <BillListItem
                  key={bill.id}
                  bill={bill}
                  href={billPath(upperCode, session, bill.identifier_norm)}
                />
              ))}
            </ul>
            <PaginationNav
              pagination={billsResult.data.pagination}
              basePath={`/states/${upperCode}/sessions/${session}`}
              searchParams={{ chamber: sp.chamber }}
            />
          </>
        )}
      </section>
    </div>
  );
}
