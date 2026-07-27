import { permanentRedirect } from "next/navigation";
import type { Metadata } from "next";
import BillDetailView from "@/components/BillDetailView";
import {
  getBill,
  getBillData,
  getJurisdiction,
  sessionSlugForBill,
} from "@/lib/bill";
import { billSlug } from "@/lib/billUrl";

interface Props {
  params: Promise<{ id: string }>;
}

/**
 * The original bill URL. It is no longer canonical: /bills/<uuid> carries no
 * keyword signal and nobody types or links a UUID, so bills now live at
 * /states/{code}/bills/{session}/{number} and this route permanently redirects
 * onto that. Keeping the redirect (rather than deleting the route) means every
 * UUID link already shared, and every id returned by the API, still resolves.
 *
 * If the canonical path cannot be built -- the jurisdiction or session lookup
 * failed -- the bill is rendered here instead. A working page at a stale URL
 * beats a redirect loop or a 500 during an API blip.
 */
async function canonicalPathFor(id: string): Promise<string | null> {
  const result = await getBill(id);
  if (!result.ok) return null;
  const bill = result.data;

  const jurisdiction = await getJurisdiction(bill.jurisdiction_id);
  if (!jurisdiction.ok) return null;

  const code = jurisdiction.data.abbreviation;
  const sessionSlug = await sessionSlugForBill(code, bill.session_id);
  if (!sessionSlug) return null;

  return `/states/${code.toUpperCase()}/bills/${sessionSlug}/${billSlug(
    bill.identifier_norm
  )}`;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { id } = await params;
  const result = await getBill(id);
  if (!result.ok) {
    return { title: "Bill" };
  }
  const bill = result.data;
  const canonical = await canonicalPathFor(id);
  return {
    title: `${bill.identifier} — ${bill.title}`,
    description: bill.description ?? bill.title,
    alternates: { canonical: canonical ?? `/bills/${id}` },
  };
}

export default async function BillPage({ params }: Props) {
  const { id } = await params;

  const canonical = await canonicalPathFor(id);
  if (canonical) {
    // Outside any try/catch: permanentRedirect signals by throwing.
    permanentRedirect(canonical);
  }

  const data = await getBillData(id);
  return <BillDetailView data={data} canonicalPath={`/bills/${id}`} />;
}
