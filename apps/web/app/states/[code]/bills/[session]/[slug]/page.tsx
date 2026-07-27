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
