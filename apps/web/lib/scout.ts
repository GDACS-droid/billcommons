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
  message?: string;
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
  whyItMatters?: string;
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

export interface ScoutBrowserProviderUsage {
  sessions: number;
  runtimeSeconds: number;
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

function eventDetail(stage: string, value: unknown): string | undefined {
  const detail = record(value);
  if (!detail) return typeof value === "string" ? optionalString(value) : undefined;
  const explicit = optionalString(detail.message) ?? optionalString(detail.detail);
  if (explicit) return explicit;
  const count = number(detail.count);
  if (count !== undefined) {
    if (stage === "structured_candidates") return `${count} structured ${count === 1 ? "candidate" : "candidates"} identified.`;
    if (stage === "related_sources_discovered") return `${count} related official ${count === 1 ? "document" : "documents"} discovered.`;
    return `${count} ${count === 1 ? "item" : "items"} recorded.`;
  }
  const status = optionalString(detail.status);
  if (status) return `Status: ${status.replaceAll("_", " ")}.`;
  const mechanism = optionalString(detail.mechanism);
  const mimeType = optionalString(detail.mime_type);
  const fragments = [
    mechanism ? `via ${mechanism.replaceAll("_", " ")}` : undefined,
    mimeType ? mimeType.toUpperCase() : undefined,
  ].filter(Boolean);
  return fragments.length ? fragments.join(" · ") : undefined;
}

function normalizeEvent(value: unknown, index: number): ScoutEvent {
  const item = record(value) ?? {};
  const stage = optionalString(item.stage) ?? optionalString(item.kind) ?? "update";
  const message = optionalString(item.message);
  const genericMessage = stage.replaceAll("_", " ");
  return {
    id: optionalString(item.id) ?? `event-${index}`,
    stage,
    // The API's legacy fallback message is the normalized machine kind. It
    // adds no information under a human stage label, so prefer durable detail
    // and otherwise render no redundant subtitle.
    message: message && message !== genericMessage ? message : eventDetail(stage, item.detail),
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
    whyItMatters: optionalString(item.why_it_matters),
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

// `starting` is only a durable capacity reservation. The worker records
// `running` once the provider has returned an opaque session ID; every later
// cleanup lifecycle state likewise represents a provider session that started.
const PROVIDER_STARTED_BROWSER_SESSION_STATUSES = new Set([
  "running",
  "released",
  "cleanup_failed",
  "reaping",
]);

export function scoutBrowserProviderUsage(job: ScoutJob): ScoutBrowserProviderUsage {
  const sessions = job.browserSessions.filter((session) =>
    PROVIDER_STARTED_BROWSER_SESSION_STATUSES.has(session.status),
  );
  if (!sessions.length) return { sessions: 0, runtimeSeconds: 0 };
  // Session rows are the lifecycle authority. A future aggregate could also
  // include pre-provider reservations, so never attribute that wider number
  // to Solari/provider use.
  const runtimeMs = sessions.reduce(
    (total, session) => total + (session.runtimeMs ?? 0),
    0,
  );
  return { sessions: sessions.length, runtimeSeconds: Math.round(runtimeMs / 1000) };
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
  const providerStartedBrowserSessions = browserSessions.filter((session) =>
    PROVIDER_STARTED_BROWSER_SESSION_STATUSES.has(session.status),
  );
  const sessionTotal = (metric: (session: ScoutBrowserSession) => number | undefined) => {
    const values = providerStartedBrowserSessions.map(metric).filter(
      (value): value is number => value !== undefined,
    );
    return values.length ? values.reduce((total, value) => total + value, 0) : undefined;
  };
  const sessionBrowserPages = sessionTotal((session) => session.pages);
  const sessionBrowserActions = sessionTotal((session) => session.actions);
  const sessionBrowserRuntimeMs = sessionTotal((session) => session.runtimeMs);
  const sessionBrowserRoutedRequests = sessionTotal((session) => session.routedRequests);
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
      browserPages: sessionBrowserPages ?? reportedBrowserPages,
      browserActions: sessionBrowserActions ?? reportedBrowserActions,
      browserRuntimeMs: sessionBrowserRuntimeMs ?? number(usage.browser_runtime_ms),
      browserRoutedRequests: sessionBrowserRoutedRequests ?? number(usage.browser_routed_requests),
    },
    events: list(item.events).map(normalizeEvent),
    sources: list(item.sources).map(normalizeSource),
    findings: list(item.findings).map(normalizeFinding),
    browserSessions,
    errors: errors.map((error) => scoutServiceNote(error, optionalString(item.jurisdiction))),
  };
}

export function isScoutTerminal(status: ScoutStatus): status is ScoutTerminalStatus {
  return ["complete", "partial", "failed", "canceled"].includes(status);
}

export const SCOUT_POLL_INTERVAL_MS = 2_500;
export const SCOUT_MAX_UNKNOWN_POLLS = 3;

/** Terminal jobs never refresh; protocol-unknown snapshots get a small bound. */
export function scoutPollRetryDelay(status: ScoutStatus, unknownPolls = 0): number | undefined {
  if (isScoutTerminal(status)) return undefined;
  if (status === "unknown" && unknownPolls >= SCOUT_MAX_UNKNOWN_POLLS) return undefined;
  return SCOUT_POLL_INTERVAL_MS;
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
  const browserUsage = scoutBrowserProviderUsage(job);
  const browserUsed = browserUsage.sessions > 0;

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
        sessions: browserUsage.sessions,
        runtime_seconds: browserUsage.runtimeSeconds,
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
      return "Scout returned an unrecognized status. The page will retry briefly for a current record.";
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

/** Explain the bounded CA evidence outcomes without turning missing coverage into a factual claim. */
function scoutServiceNote(value: string, jurisdiction?: string): string {
  if (value === "unsupported_query" && jurisdiction === "CA") return "Scout could not match that California bill and session in Bill Commons. Check the bill number and session; this does not establish that the official bill is absent.";
  const notes: Record<string, string> = {
    retained_archive_exceeds_job_limit: "The retained California archive is larger than this research job can attach. No finding was created from that archive.",
    retained_archive_integrity_failed: "Scout could not verify the retained California archive. No finding was created from unverified bytes.",
    retained_archive_unavailable: "No usable matching action was found in the retained California archives checked. This does not mean the bill has no official actions.",
  };
  return notes[value] ?? value;
}

function apiError(response: Response, payload: unknown): ScoutApiError {
  const body = record(payload);
  const structuredDetail = record(body?.detail);
  if (response.status === 422 && structuredDetail?.message === "invalid_california_retained_query") {
    return new ScoutApiError("For California, enter a bill and session, such as AB 123 2025-2026. Add Special Session 1 only for that session.", response.status);
  }
  const detail = optionalString(body?.detail) ?? optionalString(body?.message);
  if (response.status === 401 || response.status === 403) {
    return new ScoutApiError("Sign in is required to start or view Scout research.", response.status);
  }
  return new ScoutApiError(detail ?? `Scout request failed (${response.status}).`, response.status);
}

export async function createScoutJob(query: string, jurisdiction: string, signal?: AbortSignal): Promise<ScoutJob> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/v1/scout/jobs`, {
      method: "POST",
      credentials: "include",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ query, jurisdiction }),
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
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
  if (!response.ok) {
    if (response.status === 404) {
      throw new ScoutApiError("This Scout research result is unavailable. It may no longer exist or you may not have access to it.", response.status);
    }
    throw apiError(response, payload);
  }
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

export interface ScoutMonitor {
  id: string;
  query: string;
  jurisdiction: string;
  cadenceSeconds: number;
  active: boolean;
  nextRunAt?: string;
  consecutiveDeferrals: number;
  lastCompletedRunId?: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface ScoutMonitorRun {
  id: string;
  jobId?: string;
  baselineRunId?: string;
  status: string;
  executionMode: string;
  scheduledFor?: string;
  startedAt?: string;
  completedAt?: string;
  errorClass?: string;
  sourceSnapshot: Record<string, unknown>;
  changeSummary: Record<string, unknown>;
  job?: { id: string; status: ScoutStatus; query: string; jurisdiction: string; completedAt?: string };
}

export interface ScoutMonitorRunsPage {
  monitor: ScoutMonitor;
  runs: ScoutMonitorRun[];
  nextCursor?: string;
}

function normalizeMonitor(value: unknown): ScoutMonitor {
  const item = record(value) ?? {};
  return {
    id: optionalString(item.id) ?? "",
    query: optionalString(item.query) ?? "Untitled Scout query",
    jurisdiction: optionalString(item.jurisdiction) ?? "FL",
    cadenceSeconds: number(item.cadence_seconds) ?? 0,
    active: boolean(item.active) ?? false,
    nextRunAt: optionalString(item.next_run_at),
    consecutiveDeferrals: number(item.consecutive_deferrals) ?? 0,
    lastCompletedRunId: optionalString(item.last_completed_run_id),
    createdAt: optionalString(item.created_at),
    updatedAt: optionalString(item.updated_at),
  };
}

function normalizeMonitorRun(value: unknown, index: number): ScoutMonitorRun {
  const item = record(value) ?? {};
  const job = record(item.job);
  return {
    id: optionalString(item.id) ?? `monitor-run-${index}`,
    jobId: optionalString(item.job_id),
    baselineRunId: optionalString(item.baseline_run_id),
    status: optionalString(item.status) ?? "unknown",
    executionMode: optionalString(item.execution_mode) ?? "unknown",
    scheduledFor: optionalString(item.scheduled_for),
    startedAt: optionalString(item.started_at),
    completedAt: optionalString(item.completed_at),
    errorClass: optionalString(item.error_class),
    sourceSnapshot: record(item.source_snapshot) ?? {},
    changeSummary: record(item.change_summary) ?? {},
    job: job ? {
      id: optionalString(job.id) ?? "",
      status: status(job.status),
      query: optionalString(job.query) ?? "Scout research",
      jurisdiction: optionalString(job.jurisdiction) ?? "FL",
      completedAt: optionalString(job.completed_at),
    } : undefined,
  };
}

function monitorPayload(payload: unknown): ScoutMonitor {
  const outer = record(payload) ?? {};
  const monitor = normalizeMonitor(outer.monitor);
  if (!monitor.id) throw new ScoutApiError("Scout returned a monitor without an identifier.");
  return monitor;
}

async function monitorRequest(path: string, init?: RequestInit): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/v1/scout${path}`, {
      credentials: "include",
      cache: "no-store",
      ...init,
      headers: { Accept: "application/json", ...(init?.body ? { "Content-Type": "application/json" } : {}), ...init?.headers },
    });
  } catch {
    throw new ScoutApiError("Scout could not reach saved monitors. Please try again.");
  }
  const payload = await responseJson(response);
  if (!response.ok) throw apiError(response, payload);
  return payload;
}

export async function listScoutMonitors(): Promise<ScoutMonitor[]> {
  const payload = record(await monitorRequest("/monitors")) ?? {};
  return list(payload.monitors).map(normalizeMonitor).filter((monitor) => Boolean(monitor.id));
}

export async function saveScoutMonitor(jobId: string, cadenceSeconds: number): Promise<{ created: boolean; monitor: ScoutMonitor }> {
  const payload = record(await monitorRequest(`/jobs/${encodeURIComponent(jobId)}/monitor`, {
    method: "POST", body: JSON.stringify({ cadence_seconds: cadenceSeconds }),
  })) ?? {};
  return { created: boolean(payload.created) ?? false, monitor: monitorPayload(payload) };
}

export async function updateScoutMonitor(id: string, update: { active?: boolean; cadenceSeconds?: number }): Promise<ScoutMonitor> {
  const body: Record<string, unknown> = {};
  if (update.active !== undefined) body.active = update.active;
  if (update.cadenceSeconds !== undefined) body.cadence_seconds = update.cadenceSeconds;
  return monitorPayload(await monitorRequest(`/monitors/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(body) }));
}

export async function getScoutMonitorRuns(id: string, cursor?: string): Promise<ScoutMonitorRunsPage> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  const payload = record(await monitorRequest(`/monitors/${encodeURIComponent(id)}/runs${query}`)) ?? {};
  const monitor = normalizeMonitor(payload.monitor);
  if (!monitor.id) throw new ScoutApiError("Scout returned monitor history without a monitor identifier.");
  return { monitor, runs: list(payload.runs).map(normalizeMonitorRun), nextCursor: optionalString(payload.next_cursor) };
}

export function isScoutMonitorEligible(job: ScoutJob): boolean {
  return (job.status === "complete" || job.status === "partial") && job.findings.length > 0 && !/operator|canary/i.test(job.strategy ?? "");
}
