// Shapes mirror packages/schema (ARCHITECTURE.md) and the REST API contract
// (SPEC.md "REST API" + BRIEF-wave2.md "api" section). Fields are optional
// where the API may legitimately omit them (missing data = null upstream,
// never fabricated).

export interface Pagination {
  page: number;
  per_page: number;
  total: number;
  total_pages: number;
}

export interface ResponseMeta {
  source_freshness?: string | null;
  api_version?: string;
  request_id?: string;
}

export interface ListEnvelope<T> {
  data: T[];
  pagination: Pagination;
  meta: ResponseMeta;
}

export interface DetailEnvelope<T> {
  data: T;
  meta: ResponseMeta;
}

export interface Jurisdiction {
  id: string;
  name: string;
  code: string; // e.g. "NC", "DC"
  classification?: string;
  current_session_id?: string | null;
  current_session_name?: string | null;
  coverage_status?: CoverageStatus;
  updated_at?: string;
}

export interface Session {
  id: string;
  jurisdiction_id: string;
  jurisdiction_code?: string;
  name: string;
  identifier: string;
  classification?: string; // regular | special
  start_date?: string | null;
  end_date?: string | null;
  active?: boolean;
}

// Shape of GET /bills/{id}/sponsors (billcommons_api.schemas.SponsorshipOut).
// The API does not join in party/chamber for sponsors today.
export interface Sponsor {
  id: string;
  bill_id: string;
  person_id?: string | null;
  name?: string | null;
  classification?: string | null; // primary | cosponsor
  primary: boolean;
}

export interface BillAction {
  id: string;
  bill_id: string;
  description: string;
  action_date?: string | null;
  classification?: string | null;
  order?: number | null;
}

export interface BillVersion {
  id: string;
  bill_id: string;
  note?: string | null;
  date?: string | null;
}

export interface BillDocument {
  id: string;
  bill_version_id: string;
  media_type?: string | null;
  url?: string | null;
  has_extracted_text?: boolean;
}

export interface VoteRecord {
  id: string;
  option: string; // yes | no | abstain | absent | excused
  voter_name?: string | null;
  person_id?: string | null;
}

export interface VoteEvent {
  id: string;
  bill_id?: string | null;
  motion_text?: string | null;
  motion_classification?: string | null;
  start_date?: string | null;
  result?: string | null;
  yes_count?: number | null;
  no_count?: number | null;
  other_count?: number | null;
  votes: VoteRecord[];
}

export interface RelatedBill {
  id: string;
  identifier: string;
  title: string;
  jurisdiction_code?: string;
  relation_type?: string | null;
}

export interface SourceRecord {
  source_name: string;
  source_url: string;
  retrieved_at?: string | null;
  license_note?: string | null;
}

// Shape of GET /bills/{id} -- a bare object, NOT a {data, meta} envelope
// (unlike list endpoints). Only carries fields billcommons_api.schemas
// .BillDetail actually returns; sponsors/actions/versions/documents/votes
// are separate subresource endpoints the web page fetches in parallel.
export interface Bill {
  id: string;
  jurisdiction_id: string;
  session_id: string;
  chamber?: string | null;
  identifier: string; // as-published, e.g. "H.B. 123"
  identifier_norm: string; // "HB 123"
  title: string;
  short_title?: string | null;
  bill_type?: string | null;
  status?: string | null;
  status_date?: string | null;
  introduced_date?: string | null;
  latest_action_text?: string | null;
  latest_action_date?: string | null;
  source_url?: string | null;
  description?: string | null;
  source_name?: string | null;
  retrieved_at?: string | null;
  upstream_updated_at?: string | null;
}

export interface BillSummary {
  id: string;
  jurisdiction_id: string;
  session_id: string;
  chamber?: string | null;
  identifier: string;
  identifier_norm: string;
  title: string;
  short_title?: string | null;
  bill_type?: string | null;
  status?: string | null;
  status_date?: string | null;
  introduced_date?: string | null;
  latest_action_text?: string | null;
  latest_action_date?: string | null;
  source_url?: string | null;
  highlight?: string | null;
}

export interface Person {
  id: string;
  name: string;
  party?: string | null;
  chamber?: string | null;
  jurisdiction_code?: string;
  district?: string | null;
  current?: boolean;
  sponsored_bills?: BillSummary[];
}

export interface Committee {
  id: string;
  name: string;
  chamber?: string | null;
  jurisdiction_code?: string;
  members?: { person_id: string; name: string; role?: string | null }[];
  bills?: BillSummary[];
}

export interface HearingEvent {
  id: string;
  name: string;
  start_date: string;
  jurisdiction_code?: string;
  committee_name?: string | null;
  location?: string | null;
  status?: string | null;
  bills?: { id: string; identifier: string; title: string }[];
}

export type CoverageStatus =
  | "NOT_STARTED"
  | "SOURCE_IDENTIFIED"
  | "BOOTSTRAPPED"
  | "METADATA_SEARCHABLE"
  | "FULL_TEXT_SEARCHABLE"
  | "VALIDATING"
  | "GREEN"
  | "DEGRADED"
  | "BLOCKED";

export interface CoverageRow {
  jurisdiction_code: string;
  jurisdiction_name: string;
  session_identifier?: string | null;
  session_status?: string | null; // active | adjourned | special
  bill_count?: number;
  full_text_count?: number;
  full_text_pct?: number | null;
  last_update?: string | null;
  source_name?: string | null;
  validation_sample?: number | null;
  validation_pass_rate?: number | null;
  status: CoverageStatus;
  known_gaps?: string[];
}

export type SearchResult = BillSummary;
