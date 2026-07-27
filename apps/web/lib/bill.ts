import { apiGet } from "./api";
import { billSlugToIdentifier, slugify } from "./billUrl";
import type {
  Bill,
  BillAction,
  BillDocument,
  BillSummary,
  BillVersion,
  Jurisdiction,
  ListEnvelope,
  Session,
  Sponsor,
  VoteEvent,
} from "./types";

/**
 * Bill pages are the crawlable surface -- ~200k of them are listed in the
 * sitemaps -- so every read here is cached. An hour keeps a bill's action
 * timeline fresh enough to be useful while collapsing a crawl of one page from
 * seven API round trips to zero.
 */
const BILL_REVALIDATE = 3600;

export interface BillPageData {
  bill: Awaited<ReturnType<typeof getBill>>;
  versions: Awaited<ReturnType<typeof apiGet<BillVersion[]>>>;
  actions: Awaited<ReturnType<typeof apiGet<BillAction[]>>>;
  sponsors: Awaited<ReturnType<typeof apiGet<Sponsor[]>>>;
  votes: Awaited<ReturnType<typeof apiGet<VoteEvent[]>>>;
  documents: Awaited<ReturnType<typeof apiGet<BillDocument[]>>>;
  jurisdiction: Awaited<ReturnType<typeof apiGet<Jurisdiction>>> | null;
}

// GET /bills/{id} returns the bare BillDetail object -- no {data, meta}
// envelope (unlike list endpoints). See apps/api/billcommons_api/routers/bills.py.
export async function getBill(id: string) {
  return apiGet<Bill>(`/api/v1/bills/${id}`, undefined, {
    revalidate: BILL_REVALIDATE,
  });
}

export async function getBillData(id: string): Promise<BillPageData> {
  const opts = { revalidate: BILL_REVALIDATE };
  const [bill, versions, actions, sponsors, votes, documents] = await Promise.all([
    getBill(id),
    apiGet<BillVersion[]>(`/api/v1/bills/${id}/versions`, undefined, opts),
    apiGet<BillAction[]>(`/api/v1/bills/${id}/actions`, undefined, opts),
    apiGet<Sponsor[]>(`/api/v1/bills/${id}/sponsors`, undefined, opts),
    apiGet<VoteEvent[]>(`/api/v1/bills/${id}/votes`, undefined, opts),
    apiGet<BillDocument[]>(`/api/v1/bills/${id}/documents`, undefined, opts),
  ]);

  const jurisdiction = bill.ok
    ? await apiGet<Jurisdiction>(
        `/api/v1/jurisdictions/${bill.data.jurisdiction_id}`,
        undefined,
        opts
      )
    : null;

  return { bill, versions, actions, sponsors, votes, documents, jurisdiction };
}

export async function getJurisdiction(idOrCode: string) {
  return apiGet<Jurisdiction>(`/api/v1/jurisdictions/${idOrCode}`, undefined, {
    revalidate: BILL_REVALIDATE,
  });
}

/**
 * Sessions for one jurisdiction. There are 77 sessions across all 51
 * jurisdictions (at most 4 for any one of them), so a single page covers every
 * state and no pagination loop is needed.
 */
export async function getSessions(jurisdictionCode: string) {
  return apiGet<ListEnvelope<Session>>(
    "/api/v1/sessions",
    { jurisdiction: jurisdictionCode.toUpperCase(), per_page: 50 },
    { revalidate: BILL_REVALIDATE }
  );
}

/** The session identifier behind a slugified URL segment, or null. */
export async function resolveSessionSlug(
  jurisdictionCode: string,
  sessionSlug: string
): Promise<Session | null> {
  const sessions = await getSessions(jurisdictionCode);
  if (!sessions.ok) return null;
  return (
    sessions.data.data.find((s) => slugify(s.identifier) === sessionSlug) ?? null
  );
}

/** The slugified session segment for a bill's session_id, or null. */
export async function sessionSlugForBill(
  jurisdictionCode: string,
  sessionId: string
): Promise<string | null> {
  const sessions = await getSessions(jurisdictionCode);
  if (!sessions.ok) return null;
  const match = sessions.data.data.find((s) => s.id === sessionId);
  return match ? slugify(match.identifier) : null;
}

/**
 * Resolve /states/{code}/bills/{sessionSlug}/{billSlug} to a bill id.
 *
 * Returns null when the triple names no bill -- the caller renders a 404, which
 * is the honest answer for a URL a crawler guessed or a session that has since
 * been renamed upstream.
 */
export async function resolveBillSlug(
  jurisdictionCode: string,
  sessionSlug: string,
  billSlug: string
): Promise<BillSummary | null> {
  const session = await resolveSessionSlug(jurisdictionCode, sessionSlug);
  if (!session) return null;

  const result = await apiGet<ListEnvelope<BillSummary>>(
    "/api/v1/bills",
    {
      jurisdiction: jurisdictionCode.toUpperCase(),
      session: session.identifier,
      identifier: billSlugToIdentifier(billSlug),
      per_page: 2,
    },
    { revalidate: BILL_REVALIDATE }
  );
  if (!result.ok) return null;
  // (jurisdiction, session, identifier_norm) is unique across the corpus, so
  // more than one row here means the data changed shape and the URL no longer
  // names a single bill -- better to 404 than to pick one arbitrarily.
  const rows = result.data.data;
  return rows.length === 1 ? rows[0] : null;
}
