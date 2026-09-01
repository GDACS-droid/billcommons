/**
 * Browser client and defensive contract adapter for the authenticated Scout
 * endpoints. Scout responses contain evidence from an external system, so the
 * UI only renders normalized text and only turns HTTPS source URLs into links.
 */

export type ScoutTerminalStatus = "complete" | "partial" | "failed" | "canceled";
export type ScoutStatus = ScoutTerminalStatus | "queued" | "running" | "unknown";

export interface ScoutEvent {
  id: string;
  stage: string;
  message: string;
  createdAt?: string;
}

export interface ScoutSource {
  id: string;
  title: string;
  canonicalUrl?: string;
  officialDomain?: string;
  official?: boolean;
  sourceType?: string;
  retrievalMechanism?: string;
  retrievedAt?: string;
  contentHash?: string;
  status?: string;
  priorSourceId?: string;
  changeKind?: string;
  changeSummary?: string;
  priorSource?: {
    jobId: string;
    canonicalUrl?: string;
    retrievedAt?: string;
    contentHash?: string;
  };
}

export interface ScoutFinding {
  id: string;
  title: string;
  whatHappened: string;
  whyItMatters: string;
  relevantDate?: string;
  evidenceExcerpt?: string;
  confidence?: string;
  sourceId?: string;
  sourceUrl?: string;
  billId?: string;
}

export interface ScoutBrowserSession {
  id: string;
  status: string;
  replayAvailable: boolean;
  pages?: number;
  actions?: number;
  runtimeMs?: number;
  routedRequests?: number;
}

export interface ScoutUsage {
  externalRequests?: number;
  browserPages?: number;
  browserActions?: number;
  browserRuntimeMs?: number;
  browserRoutedRequests?: number;
}

export interface ScoutReplay {
  available: boolean;
  url?: string;
}

export interface ScoutAnalyticsFact {
  /** Client-only deduplication key. It is never sent to analytics. */
  key: string;
  event: string;
  properties: Record<string, string | number | boolean>;
}

export interface ScoutJob {
  id: string;
  query: string;
  normalizedQuery?: string;
  jurisdiction: string;
  status: ScoutStatus;
  strategy?: string;
  cacheStatus?: string;
  cacheHit?: boolean;
  partialSuccess?: boolean;
  createdAt?: string;
  startedAt?: string;
  completedAt?: string;
  usage: ScoutUsage;
  events: ScoutEvent[];
  sources: ScoutSource[];
  findings: ScoutFinding[];
  browserSessions: ScoutBrowserSession[];
  errors: string[];
}

export class ScoutApiError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
    this.name = "ScoutApiError";
  }
}

type UnknownRecord = Record<string, unknown>;

function record(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as UnknownRecord)
    : null;
}

function string(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function optionalString(value: unknown): string | undefined {
  const result = string(value).trim();
  return result || undefined;
}

function number(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function text(value: unknown): string | undefined {
  return optionalString(value) ?? (typeof value === "number" && Number.isFinite(value) ? String(value) : undefined);
}

function boolean(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function status(value: unknown): ScoutStatus {
  switch (value) {
    case "queued":
    case "running":
      return value;
    case "complete":
    // Early P0 workers persisted `completed`; accept it during the API
    // transition, but expose one stable terminal vocabulary to the page.
    case "completed":
      return "complete";
    case "partial":
    case "failed":
    case "canceled":
      return value;
    default:
      return "unknown";
  }
}

function normalizeEvent(value: unknown, index: number): ScoutEvent {
  const item = record(value) ?? {};
  const detail = record(item.detail);
  return {
    id: optionalString(item.id) ?? `event-${index}`,
    stage: optionalString(item.stage) ?? optionalString(item.kind) ?? "update",
    message:
      optionalString(item.message) ??
      optionalString(detail?.message) ??
      optionalString(detail?.detail) ??
      (typeof item.detail === "string" ? item.detail : "Scout recorded an update."),
    createdAt: optionalString(item.created_at),
  };
}

function normalizeSource(value: unknown, index: number): ScoutSource {
  const item = record(value) ?? {};
  const prior = record(item.prior_source);
  const officialDomain = optionalString(item.official_domain) ?? optionalString(item.domain) ??
    (boolean(item.official) === true ? "Official source" : undefined);
  return {
    id: optionalString(item.id) ?? `source-${index}`,
    title: optionalString(item.title) ?? "Untitled source",
    canonicalUrl: safeHttpsUrl(optionalString(item.canonical_url) ?? optionalString(item.url)),
    officialDomain,
    official: boolean(item.official),
    sourceType: optionalString(item.source_type) ?? optionalString(item.mime_type),
    retrievalMechanism: optionalString(item.retrieval_mechanism) ?? optionalString(item.mechanism),
    retrievedAt: optionalString(item.retrieved_at),
    contentHash: optionalString(item.content_hash),
    status: text(item.status),
    priorSourceId: optionalString(item.prior_source_id),
    changeKind: optionalString(item.change_kind),
    changeSummary: optionalString(item.change_summary),
    priorSource: prior ? {
      jobId: optionalString(prior.job_id) ?? "",
      canonicalUrl: safeHttpsUrl(optionalString(prior.canonical_url)),
      retrievedAt: optionalString(prior.retrieved_at),
      contentHash: optionalString(prior.content_hash),
    } : undefined,
  };
}

function normalizeFinding(value: unknown, index: number): ScoutFinding {
  const item = record(value) ?? {};
  return {
    id: optionalString(item.id) ?? `finding-${index}`,
    title: optionalString(item.title) ?? "Untitled finding",
    whatHappened: optionalString(item.what_happened) ?? "No summary was returned.",
    whyItMatters: optionalString(item.why_it_matters) ?? "No significance statement was returned.",
    relevantDate: optionalString(item.relevant_date),
    evidenceExcerpt: optionalString(item.evidence_excerpt) ?? optionalString(item.excerpt),
    confidence: optionalString(item.confidence),
    sourceId: optionalString(item.source_id),
    sourceUrl: safeHttpsUrl(optionalString(item.source_url)),
    billId: optionalString(item.bill_id),
  };
}

function normalizeBrowserSession(value: unknown, index: number): ScoutBrowserSession {
  const item = record(value) ?? {};
  return {
    id: optionalString(item.id) ?? `browser-session-${index}`,
    status: optionalString(item.status) ?? "unknown",
    replayAvailable: boolean(item.replay_available) ?? false,
    pages: number(item.pages),
    actions: number(item.actions),
    runtimeMs: number(item.runtime_ms),
    routedRequests: number(item.routed_requests),
  };
}

/** Accept either the direct API object or a conventional { data } / { job } envelope. */
export function normalizeScoutJob(payload: unknown): ScoutJob {
  const outer = record(payload) ?? {};
  const item = record(outer.job) ?? record(outer.data) ?? outer;
  const usage = record(item.usage) ?? {};
  const strategy = record(item.strategy);
  const errors = list(item.errors)
    .map((error) => {
      if (typeof error === "string") return error;
      const detail = record(error);
      return optionalString(detail?.message) ?? optionalString(detail?.detail) ?? "Scout reported an error.";
    })
    .filter(Boolean);
  const errorClass = optionalString(item.error_class);
  if (errorClass && !errors.includes(errorClass)) errors.push(errorClass);
  const browserSessions = list(item.browser_sessions).map(normalizeBrowserSession);
  const reportedBrowserPages = number(usage.browser_pages);
  const reportedBrowserActions = number(usage.browser_actions);

  return {
    id: optionalString(item.id) ?? "",
    query: optionalString(item.query) ?? "",
    normalizedQuery: optionalString(item.normalized_query),
    jurisdiction: optionalString(item.jurisdiction) ?? "FL",
    status: status(item.status),
    strategy:
      optionalString(item.strategy) ??
      optionalString(strategy?.mode) ??
      optionalString(strategy?.adapter),
    cacheStatus: optionalString(item.cache_status),
    cacheHit: boolean(item.cache_hit) ?? boolean(outer.cache_hit) ?? boolean(outer.coalesced),
    partialSuccess: boolean(item.partial_success),
    createdAt: optionalString(item.created_at),
    startedAt: optionalString(item.started_at),
    completedAt: optionalString(item.completed_at),
    usage: {
      externalRequests: number(usage.external_requests),
      browserPages: reportedBrowserPages ?? (browserSessions.length ? browserSessions.reduce((total, session) => total + (session.pages ?? 0), 0) : undefined),
      browserActions: reportedBrowserActions ?? (browserSessions.length ? browserSessions.reduce((total, session) => total + (session.actions ?? 0), 0) : undefined),
      browserRuntimeMs: number(usage.browser_runtime_ms) ?? (browserSessions.length ? browserSessions.reduce((total, session) => total + (session.runtimeMs ?? 0), 0) : undefined),
      browserRoutedRequests: number(usage.browser_routed_requests) ?? (browserSessions.length ? browserSessions.reduce((total, session) => total + (session.routedRequests ?? 0), 0) : undefined),
    },
    events: list(item.events).map(normalizeEvent),
    sources: list(item.sources).map(normalizeSource),
    findings: list(item.findings).map(normalizeFinding),
    browserSessions,
    errors,
  };
}

export function isScoutTerminal(status: ScoutStatus): status is ScoutTerminalStatus {
  return ["complete", "partial", "failed", "canceled"].includes(status);
}

/**
 * Return aggregate, privacy-safe product facts observed in a persisted job.
 * Keys may contain opaque local row ids solely to avoid duplicate calls while
 * polling; properties intentionally exclude job/customer/session ids, query
 * text, URLs, titles, excerpts, hashes, and replay links.
 */
export function scoutAnalyticsFacts(job: ScoutJob): ScoutAnalyticsFact[] {
  // Polling snapshots are mutable. Emit discovery/runtime facts only after the
  // durable job reaches a terminal state so counts and browser runtime cannot
  // be frozen at their initial zero values.
  if (!isScoutTerminal(job.status)) return [];
  const facts: ScoutAnalyticsFact[] = [];
  const base = { jurisdiction: job.jurisdiction };
  const stages = new Set(job.events.map((event) => event.stage));
  const directUsed = job.sources.some((source) => source.retrievalMechanism === "direct") ||
    stages.has("direct_retrieval");
  const browserUsed = job.browserSessions.length > 0 ||
    job.sources.some((source) => source.retrievalMechanism === "browser");

  if (stages.has("structured_candidates")) {
    facts.push({ key: `${job.id}:existing-data`, event: "scout_existing_data_used", properties: base });
  }
  if (directUsed) {
    facts.push({ key: `${job.id}:direct`, event: "scout_direct_retrieval_used", properties: base });
  }
  if (browserUsed) {
    facts.push({
      key: `${job.id}:solari`,
      event: "scout_solari_used",
      properties: {
        ...base,
        sessions: job.browserSessions.length,
        runtime_seconds: Math.round((job.usage.browserRuntimeMs ?? 0) / 1000),
      },
    });
  }
  for (const source of job.sources) {
    const properties = {
      ...base,
      mechanism: source.retrievalMechanism ?? "unknown",
      official: source.official === true,
    };
    facts.push({ key: `${job.id}:source:${source.id}`, event: "scout_source_discovered", properties });
    if (source.contentHash) {
      facts.push({ key: `${job.id}:document:${source.id}`, event: "scout_document_discovered", properties });
    }
  }
  for (const finding of job.findings) {
    facts.push({
      key: `${job.id}:finding:${finding.id}`,
      event: "scout_finding_generated",
      properties: { ...base, confidence: finding.confidence ?? "unknown" },
    });
  }
  return facts;
}

/** Presentational copy is centralized so terminal states cannot be overstated. */
export function scoutStatusSummary(job: ScoutJob): string {
  switch (job.status) {
    case "complete":
      return job.findings.length
        ? "Research completed. Findings below are linked to retained source metadata."
        : "Research completed without a verifiable finding to show.";
    case "partial":
      return "Research completed with partial results. Review the source and error details below.";
    case "failed":
      return "Research did not complete. No unverified result is being presented as a finding.";
    case "canceled":
      return "Research was canceled. Any retained findings below are only the evidence completed before cancellation.";
    case "queued":
      return "Research is queued. Updates appear only when the service records them.";
    case "running":
      return "Research is running. Updates appear only when the service records them.";
    default:
      return "Scout returned an unrecognized status. The page will keep checking for a current record.";
  }
}

/** Never make an untrusted response value clickable unless it is a plain HTTPS URL. */
export function safeHttpsUrl(value?: string): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password ? url.toString() : undefined;
  } catch {
    return undefined;
  }
}

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

async function responseJson(response: Response): Promise<unknown> {
  return response.json().catch(() => null);
}

function apiError(response: Response, payload: unknown): ScoutApiError {
  const body = record(payload);
  const detail = optionalString(body?.detail) ?? optionalString(body?.message);
  if (response.status === 401 || response.status === 403) {
    return new ScoutApiError("Sign in is required to start or view Scout research.", response.status);
  }
  return new ScoutApiError(detail ?? `Scout request failed (${response.status}).`, response.status);
}

export async function createScoutJob(query: string, jurisdiction: string): Promise<ScoutJob> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/v1/scout/jobs`, {
      method: "POST",
      credentials: "include",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ query, jurisdiction }),
    });
  } catch {
    throw new ScoutApiError("Scout could not reach the service. Please try again.");
  }

  const payload = await responseJson(response);
  if (!response.ok) throw apiError(response, payload);
  const job = normalizeScoutJob(payload);
  if (!job.id) throw new ScoutApiError("Scout returned a response without a job identifier.");
  return job;
}

export async function getScoutJob(id: string, signal?: AbortSignal): Promise<ScoutJob> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/v1/scout/jobs/${encodeURIComponent(id)}`, {
      credentials: "include",
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ScoutApiError("Scout could not refresh this job. Retrying shortly.");
  }

  const payload = await responseJson(response);
  if (!response.ok) throw apiError(response, payload);
  const job = normalizeScoutJob(payload);
  if (!job.id) throw new ScoutApiError("Scout returned a response without a job identifier.");
  return job;
}

export async function cancelScoutJob(id: string): Promise<ScoutJob> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/v1/scout/jobs/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
      credentials: "include",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
    });
  } catch {
    throw new ScoutApiError("Scout could not send the cancellation request. Please try again.");
  }
  const payload = await responseJson(response);
  if (!response.ok) throw apiError(response, payload);
  const job = normalizeScoutJob(payload);
  if (!job.id) throw new ScoutApiError("Scout returned a response without a job identifier.");
  return job;
}

/**
 * A replay URL is requested only after an explicit user action. The API is
 * owner-scoped; the client keeps a returned third-party URL only in memory for
 * the current page and never logs it. It rejects anything other than a
 * non-credential-bearing HTTPS URL.
 */
export async function getScoutReplay(jobId: string, sessionId: string): Promise<ScoutReplay> {
  let response: Response;
  try {
    response = await fetch(
      `${API_BASE}/api/v1/scout/jobs/${encodeURIComponent(jobId)}/browser-sessions/${encodeURIComponent(sessionId)}/replay`,
      { credentials: "include", headers: { Accept: "application/json" }, cache: "no-store" }
    );
  } catch {
    throw new ScoutApiError("Scout could not check replay availability. Please try again.");
  }
  const payload = await responseJson(response);
  if (!response.ok) throw apiError(response, payload);
  const replay = record(payload) ?? {};
  return {
    available: boolean(replay.available) ?? false,
    url: safeHttpsUrl(optionalString(replay.replay_url)),
  };
}
