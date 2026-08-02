import { notFound } from "next/navigation";
import type { Metadata } from "next";
import BillDetailView from "@/components/BillDetailView";
import { getBillData, resolveBillSlug, resolveSessionSlug } from "@/lib/bill";
import { billPath } from "@/lib/billUrl";

interface Props {
  params: Promise<{ code: string; session: string; slug: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { code, session, slug } = await params;
  const upperCode = code.toUpperCase();
  const bill = await resolveBillSlug(upperCode, session, slug);
  if (!bill) {
    return { title: "Bill not found" };
  }

  // Lead with "GA SB 160" because that -- not the UUID, and not the title -- is
  // the phrase people actually search for.
  const title = `${upperCode} ${bill.identifier} — ${bill.title}`;
  const latest = bill.latest_action_date
    ? ` Latest action ${bill.latest_action_date}${
        bill.latest_action_text ? `: ${bill.latest_action_text}` : ""
      }.`
    : "";

  return {
    title,
    description:
      `${upperCode} ${bill.identifier}: ${bill.title}.${latest} ` +
      "Full text, sponsors, votes and action history from official legislative records.",
    alternates: {
      canonical: billPath(upperCode, session, bill.identifier_norm),
    },
    // Addressable but not indexed.
    //
    // ~200k bill-detail pages were submitted to Google and it declined to
    // index essentially all of them -- URL Inspection reports the canonical
    // bill URL as "URL is unknown to Google" despite being in the sitemap,
    // while the pages Google DID pick up sit at "Discovered - currently not
    // indexed". Nothing is blocked: robots allows them, the canonical is
    // self-referential, the fetch succeeds. Google is simply choosing not to
    // spend crawl budget on a new domain's enormous, largely repetitive
    // inventory.
    //
    // Asking for less indexation is the point. The facts on these pages
    // originate at the state legislature, whose own page should win the
    // bill-number query; competing for it costs crawl budget that the pages
    // worth citing -- the mortality report, methodology, coverage -- are
    // currently being denied.
    //
    // `follow: true` matters: the crawler should still traverse to sponsors,
    // sessions and related bills. And these must NOT be added to robots.txt --
    // a disallowed page can never be crawled, so Google would never see this
    // directive at all.
    robots: { index: false, follow: true },
    openGraph: {
      type: "article",
      title,
      description: bill.title,
    },
  };
}

export default async function BillBySlugPage({ params }: Props) {
  const { code, session, slug } = await params;
  const upperCode = code.toUpperCase();

  const summary = await resolveBillSlug(upperCode, session, slug);
  if (!summary) {
    notFound();
  }

  const [data, sessionRow] = await Promise.all([
    getBillData(summary.id),
    resolveSessionSlug(upperCode, session),
  ]);

  return (
    <BillDetailView
      data={data}
      canonicalPath={`/states/${upperCode}/bills/${session}/${slug}`}
      sessionSlug={session}
      sessionLabel={sessionRow?.identifier ?? null}
    />
  );
}
