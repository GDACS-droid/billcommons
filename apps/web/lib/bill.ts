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
  RelatedBill,
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
  related: Awaited<ReturnType<typeof apiGet<RelatedBill[]>>>;
  subjects: Awaited<ReturnType<typeof apiGet<string[]>>>;
  jurisdiction: Awaited<ReturnType<typeof apiGet<Jurisdiction>>> | null;
}

// GET /bills/{id} returns the bare BillDetail object -- no {data, meta}
// envelope (unlike list endpoints). See apps/api/billcommons_api/routers/bills.py.
export async function getBill(id: string) {
  return apiGet<Bill>(`/api/v1/bills/${id}`, undefined, {
    revalidate: BILL_REVALIDATE,
  });
}

interface BillFullEnvelope {
  bill: Bill;
  versions: BillVersion[];
  actions: BillAction[];
  sponsors: Sponsor[];
  votes: VoteEvent[];
  documents: BillDocument[];
  related: RelatedBill[];
  subjects: string[];
  jurisdiction: Jurisdiction | null;
}

/**
 * One request instead of nine.
 *
 * This page used to fetch the detail, seven sub-resources and the jurisdiction
 * separately. With ~200k crawlable bill pages that made a single crawl well
 * over a million API requests, each checking out its own pooled connection --
 * the load half of the 2026-08-02 outage.
 *
 * The per-section {ok, data} shape is preserved so callers keep rendering each
 * block independently. It is now all-or-nothing in practice, which is honest:
 * with one upstream request there is no longer a case where the sponsors
 * arrived and the votes did not, and pretending otherwise would show an empty
 * "no votes recorded" section during an outage -- absence read as ignorance,
 * the exact confusion this project exists to prevent.
 */
export async function getBillData(id: string): Promise<BillPageData> {
  const full = await apiGet<BillFullEnvelope>(`/api/v1/bills/${id}/full`, undefined, {
    revalidate: BILL_REVALIDATE,
  });

  if (!full.ok) {
    const failed = { ok: false as const, error: full.error, status: full.status };
    return {
      bill: failed,
      versions: failed,
      actions: failed,
      sponsors: failed,
      votes: failed,
      documents: failed,
      related: failed,
      subjects: failed,
      jurisdiction: failed,
    };
  }

  const d = full.data;
  return {
    bill: { ok: true, data: d.bill },
    versions: { ok: true, data: d.versions },
    actions: { ok: true, data: d.actions },
    sponsors: { ok: true, data: d.sponsors },
    votes: { ok: true, data: d.votes },
    documents: { ok: true, data: d.documents },
    related: { ok: true, data: d.related },
    subjects: { ok: true, data: d.subjects },
    jurisdiction: d.jurisdiction ? { ok: true, data: d.jurisdiction } : null,
  };
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
