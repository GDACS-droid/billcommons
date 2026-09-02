"use client";

import { track } from "@vercel/analytics";
import Link from "next/link";
import { FormEvent, useEffect, useRef, useState } from "react";
import {
  cancelScoutJob,
  createScoutJob,
  getScoutJob,
  getScoutReplay,
  isScoutTerminal,
  scoutAnalyticsFacts,
  scoutBrowserProviderUsage,
  scoutPollRetryDelay,
  type ScoutFinding,
  type ScoutJob,
  scoutStatusSummary,
} from "@/lib/scout";

const JURISDICTIONS = [{ value: "FL", label: "Florida" }];
const EXAMPLES = [
  "Research Florida legislation involving artificial intelligence.",
  "What changed recently for Florida SB 1344?",
  "Investigate Florida activity involving social media.",
];
// Scout evidence windows are bounded in the worker.  Preserve the original
// retained excerpt while making a potentially bounded display unmistakable.
const EXCERPT_CHARACTER_LIMIT = 500;
function label(value?: string | null): string {
  return value ? value.replaceAll("_", " ") : "Not recorded";
}

function strategyLabel(value?: string | null): string {
  if (value === "structured_first") return "Bill Commons first";
  if (value === "direct_first") return "Official source retrieval";
  if (value === "browser_fallback") return "Official source browser";
  return label(value);
}

function eventLabel(value?: string | null): string {
  const labels: Record<string, string> = {
    claimed: "Searching Bill Commons",
    direct_retrieval: "Checking an official source",
    browser_started: "Navigating the official website",
    source_persisted: "Evidence retained",
    finding_persisted: "Finding verified",
    finished: "Research complete",
  };
  return value ? labels[value] ?? label(value) : "Research update";
}

function time(value?: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat("en-US", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(date);
}

function relevantDate(value?: string): string | null {
  if (!value) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const date = new Date(`${value}T00:00:00Z`);
    return new Intl.DateTimeFormat("en-US", {
      dateStyle: "long",
      timeZone: "UTC",
    }).format(date);
  }
  return time(value);
}

function shortHash(value?: string): string | null {
  return value ? `${value.slice(0, 12)}${value.length > 12 ? "…" : ""}` : null;
}

function StatusLine({ job }: { job: ScoutJob }) {
  const colors: Record<ScoutJob["status"], string> = {
    queued: "bg-blue-700",
    running: "bg-blue-700",
    complete: "bg-emerald-700",
    partial: "bg-amber-600",
    failed: "bg-red-700",
    canceled: "bg-slate-500",
    unknown: "bg-slate-500",
  };
  return (
    <span className="inline-flex items-center gap-2 text-sm font-semibold text-slate-950">
      <span className={`h-2 w-2 rounded-full ${colors[job.status]}`} aria-hidden="true" />
      {label(job.status)}
    </span>
  );
}

function FindingCard({ finding, source }: { finding: ScoutFinding; source?: ScoutJob["sources"][number] }) {
  const sourceUrl = source?.canonicalUrl ?? finding.sourceUrl;
  const sourceType = source?.sourceType?.toLowerCase() ?? "";
  const excerptMayBeTruncated = (finding.evidenceExcerpt?.length ?? 0) >= EXCERPT_CHARACTER_LIMIT;
  const evidenceLinkLabel = sourceType.includes("pdf")
    ? "Open official document"
    : "Open official record";
  return (
    <article className="border-t border-slate-200 py-5 first:border-t-0 first:pt-0">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-base font-semibold text-slate-950">{finding.title}</h3>
          {finding.relevantDate ? (
            <p className="mt-1 text-xs font-semibold uppercase tracking-[0.08em] text-slate-500">
              {relevantDate(finding.relevantDate) ?? finding.relevantDate}
            </p>
          ) : null}
        </div>
        {finding.confidence ? (
          <span className="text-xs font-medium text-slate-500">
            {label(finding.confidence)} confidence
          </span>
        ) : null}
      </div>
      <dl className="mt-4 grid gap-3 text-sm leading-6 sm:grid-cols-[8.5rem_minmax(0,1fr)]">
        <dt className="font-semibold text-slate-900">What happened</dt>
        <dd className="whitespace-pre-wrap break-words text-slate-700">{finding.whatHappened}</dd>
        {finding.whyItMatters ? (
          <>
            <dt className="font-semibold text-slate-900">Why it matters</dt>
            <dd className="whitespace-pre-wrap break-words text-slate-700">{finding.whyItMatters}</dd>
          </>
        ) : null}
      </dl>
      <div className="mt-4 border-l-2 border-slate-300 pl-3">
        <p className="text-xs font-semibold uppercase tracking-[0.08em] text-slate-500">Attached official evidence</p>
        {finding.evidenceExcerpt ? (
          <blockquote className="mt-2 border-l-2 border-blue-200 pl-3 text-sm leading-6 text-slate-700">
            {finding.evidenceExcerpt}{excerptMayBeTruncated ? <span aria-label="Excerpt may continue">…</span> : null}
          </blockquote>
        ) : (
          <p className="mt-2 text-sm text-slate-500">No excerpt was retained for this finding.</p>
        )}
        {excerptMayBeTruncated ? (
          <p className="mt-2 text-xs leading-5 text-slate-500">Excerpt is limited to 500 characters. Open the official document for the complete record.</p>
        ) : null}
        {sourceUrl ? (
          <a
            href={sourceUrl}
            target="_blank"
            rel="noreferrer noopener"
            onClick={() => track("scout_evidence_opened", { control: "finding" })}
            className="mt-3 inline-flex text-sm font-semibold text-blue-800 underline underline-offset-2 hover:text-blue-600"
          >
            {evidenceLinkLabel} <span aria-hidden="true">↗</span>
          </a>
        ) : (
          <p className="mt-3 text-sm text-slate-500">A safe source link was not available in this response.</p>
        )}
      </div>
    </article>
  );
}

function ReplayControls({ job }: { job: ScoutJob }) {
  const [urls, setUrls] = useState<Record<string, string>>({});
  const [checking, setChecking] = useState<string | null>(null);
  const [error, setError] = useState("");

  async function resolveReplay(sessionId: string) {
    setChecking(sessionId);
    setError("");
    try {
      const replay = await getScoutReplay(job.id, sessionId);
      if (!replay.available || !replay.url) {
        setError("A replay is not available for that browser session.");
        return;
      }
      setUrls((current) => ({ ...current, [sessionId]: replay.url as string }));
      track("scout_replay_resolved", { status: "available" });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Scout could not check replay availability.");
    } finally {
      setChecking(null);
    }
  }

  return (
    <section aria-labelledby="replay-heading">
      <h2 id="replay-heading" className="text-sm font-semibold text-slate-950">Browser session</h2>
      <p className="mt-2 text-sm leading-6 text-slate-600">
        A replay is operational context, not evidence. It is available only to the job owner.
      </p>
      {job.browserSessions.length ? (
        <ul className="mt-3 space-y-2 text-sm">
          {job.browserSessions.map((session) => (
            <li key={session.id} className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 py-3 first:border-t-0 first:pt-0">
              <span className="text-slate-700">{label(session.status)} · {session.pages ?? 0} {(session.pages ?? 0) === 1 ? "page" : "pages"} · {session.actions ?? 0} actions</span>
              {urls[session.id] ? (
                <a href={urls[session.id]} target="_blank" rel="noreferrer noopener" onClick={() => track("scout_replay_opened", { status: "available" })} className="text-sm font-semibold text-blue-800 underline underline-offset-2 hover:text-blue-600">
                  Open replay <span aria-hidden="true">↗</span>
                </a>
              ) : (
                <button type="button" disabled={!session.replayAvailable || checking === session.id} onClick={() => resolveReplay(session.id)} className="text-xs font-semibold text-blue-800 underline underline-offset-2 hover:text-blue-600 disabled:cursor-not-allowed disabled:no-underline disabled:opacity-50">
                  {checking === session.id ? "Loading…" : session.replayAvailable ? "Load replay link" : "Replay unavailable"}
                </button>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-3 text-sm text-slate-600">No browser session was needed for this job.</p>
      )}
      {error ? <p className="mt-3 text-sm text-red-800" role="alert">{error}</p> : null}
    </section>
  );
}

function JobDetails({ job, refreshError, onCancel, canceling }: { job: ScoutJob; refreshError: string; onCancel: () => void; canceling: boolean }) {
  const sourceById = new Map(job.sources.map((source) => [source.id, source]));
  const terminal = isScoutTerminal(job.status);
  const browserUsage = scoutBrowserProviderUsage(job);
  const startedBrowser = job.browserSessions.find((session) =>
    ["running", "released", "cleanup_failed", "reaping"].includes(session.status),
  );
  const alertStyle =
    job.status === "failed"
      ? "border-red-300 text-red-950"
      : job.status === "partial"
        ? "border-amber-300 text-amber-950"
        : "border-slate-300 text-slate-950";

  return (
    <section aria-labelledby="scout-results" className="mt-8">
      <div className={`border-y py-4 text-sm ${alertStyle}`} role="status" aria-live="polite">
        <div className="flex flex-wrap items-center gap-3">
          <StatusLine job={job} />
          <span aria-hidden="true" className="text-slate-300">/</span>
          <p className="font-medium">{scoutStatusSummary(job)}</p>
          {!terminal ? (
            <button type="button" onClick={onCancel} disabled={canceling} className="ml-auto text-xs font-semibold text-blue-900 underline underline-offset-2 hover:text-blue-700 disabled:cursor-not-allowed disabled:opacity-50">
              {canceling ? "Canceling…" : "Cancel research"}
            </button>
          ) : null}
        </div>
        {refreshError ? <p className="mt-2 text-xs">{refreshError}</p> : null}
      </div>

      <div className="mt-6 grid gap-8 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div>
          <div className="flex flex-wrap items-baseline justify-between gap-3">
            <div>
              <p className="page-eyebrow">Findings</p>
              <h2 id="scout-results" className="mt-1 text-xl font-semibold tracking-tight text-slate-950">
                Research record
              </h2>
            </div>
            <p className="text-sm text-slate-500">
              {job.findings.length} {job.findings.length === 1 ? "finding" : "findings"}
            </p>
          </div>
          <div className="mt-4">
            {job.findings.map((finding) => (
              <FindingCard key={finding.id} finding={finding} source={sourceById.get(finding.sourceId ?? "")} />
            ))}
            {!job.findings.length ? (
              <div className="border-y border-slate-200 py-5 text-sm leading-6 text-slate-600">
                {terminal
                  ? "There are no verified findings to display for this job. This does not establish that no legislative activity occurred."
                  : "No verified finding has been recorded yet. Scout will show evidence only after the service retains it."}
              </div>
            ) : null}
          </div>
        </div>

        <aside className="border-l border-slate-200 pl-5" aria-label="Scout job details">
          <section>
            <h2 className="text-sm font-semibold text-slate-950">Sources and method</h2>
            <dl className="mt-3 space-y-3 text-sm">
              <div>
                <dt className="text-slate-500">Jurisdiction</dt>
                <dd className="font-medium text-slate-900">{job.jurisdiction}</dd>
              </div>
              <div>
                <dt className="text-slate-500">Strategy</dt>
                <dd className="font-medium text-slate-900">{strategyLabel(job.strategy)}</dd>
              </div>
              <div>
                <dt className="text-slate-500">Cache</dt>
                <dd className="font-medium text-slate-900">
                  {job.cacheHit ? "Reused matching research" : "New research"}
                </dd>
              </div>
              {time(job.createdAt) ? (
                <div>
                  <dt className="text-slate-500">Created</dt>
                  <dd className="font-medium text-slate-900">{time(job.createdAt)}</dd>
                </div>
              ) : null}
              {time(job.completedAt) ? (
                <div>
                  <dt className="text-slate-500">Completed</dt>
                  <dd className="font-medium text-slate-900">{time(job.completedAt)}</dd>
                </div>
              ) : null}
            </dl>
            {startedBrowser ? (
              <p className="mt-4 border-t border-slate-200 pt-3 text-sm leading-6 text-slate-700">
                <span className="font-semibold text-slate-950">Solari:</span>{" "}
                {label(startedBrowser.status)} · {startedBrowser.pages ?? 0} {(startedBrowser.pages ?? 0) === 1 ? "page" : "pages"} · {startedBrowser.actions ?? 0} actions
                {browserUsage.runtimeSeconds ? ` · ${browserUsage.runtimeSeconds}s` : ""}
              </p>
            ) : null}
          </section>

          <section className="mt-6 border-t border-slate-200 pt-5">
            <h2 className="text-sm font-semibold text-slate-950">Usage</h2>
            <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-4 text-sm">
              <div><dt className="text-slate-500">Official requests</dt><dd className="font-semibold">{job.usage.externalRequests ?? "—"}</dd></div>
              <div><dt className="text-slate-500">Browser pages</dt><dd className="font-semibold">{job.usage.browserPages ?? "—"}</dd></div>
              <div><dt className="text-slate-500">Actions</dt><dd className="font-semibold">{job.usage.browserActions ?? "—"}</dd></div>
              <div><dt className="text-slate-500">Browser requests</dt><dd className="font-semibold">{job.usage.browserRoutedRequests ?? "—"}</dd></div>
              <div><dt className="text-slate-500">Browser time</dt><dd className="font-semibold">{job.usage.browserRuntimeMs === undefined ? "—" : `${Math.round(job.usage.browserRuntimeMs / 1000)}s`}</dd></div>
            </dl>
          </section>

          <section className="mt-6 border-t border-slate-200 pt-5" aria-labelledby="sources-heading">
            <div className="flex items-baseline justify-between gap-3">
              <h2 id="sources-heading" className="text-sm font-semibold text-slate-950">Evidence sources</h2>
              <p className="text-xs text-slate-500">{job.sources.length} retained</p>
            </div>
            {job.sources.length ? (
              <ul className="mt-3 divide-y divide-slate-200 border-y border-slate-200">
                {job.sources.map((source) => (
                  <li key={source.id} className="py-3">
                    <p className="font-medium leading-5 text-slate-950">{source.title}</p>
                    <p className="mt-1 break-words text-xs leading-5 text-slate-600">
                      {[source.officialDomain, source.sourceType, source.retrievalMechanism, source.status, time(source.retrievedAt)]
                        .filter(Boolean)
                        .map(label)
                        .join(" · ") || "Metadata not recorded"}
                    </p>
                    {shortHash(source.contentHash) ? (
                      <p className="mt-1 font-mono text-xs text-slate-500">Hash {shortHash(source.contentHash)}</p>
                    ) : null}
                    {source.changeKind && source.changeSummary ? (
                      <p className="mt-2 text-xs leading-5 text-slate-600">
                        <span className="font-semibold text-slate-700">{label(source.changeKind)}.</span>{" "}
                        {source.changeSummary}
                      </p>
                    ) : null}
                    {source.priorSource ? (
                      <p className="mt-1 text-xs leading-5 text-slate-500">
                        Prior evidence: {time(source.priorSource.retrievedAt) || "time not recorded"}
                        {shortHash(source.priorSource.contentHash) ? ` · ${shortHash(source.priorSource.contentHash)}` : ""}
                        {source.priorSource.canonicalUrl ? (
                          <>{" · "}<a className="underline underline-offset-2 hover:text-slate-700" href={source.priorSource.canonicalUrl} target="_blank" rel="noreferrer noopener" onClick={() => track("scout_evidence_opened", { control: "prior_source_metadata" })}>source ↗</a></>
                        ) : null}
                      </p>
                    ) : null}
                    {source.canonicalUrl ? (
                      <a href={source.canonicalUrl} target="_blank" rel="noreferrer noopener" onClick={() => track("scout_evidence_opened", { control: "source_metadata" })} className="mt-2 inline-flex text-sm font-semibold text-blue-800 underline underline-offset-2 hover:text-blue-600">
                        Open source <span aria-hidden="true">↗</span>
                      </a>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-3 text-sm text-slate-600">No source metadata has been retained yet.</p>
            )}
          </section>
        </aside>
      </div>

      <div className="mt-8 grid gap-8 border-t border-slate-200 pt-6 md:grid-cols-2">
        <section aria-labelledby="events-heading">
          <h2 id="events-heading" className="text-sm font-semibold text-slate-950">Durable service events</h2>
          {job.events.length ? (
            <ol className="mt-3 space-y-3 border-l border-slate-200 pl-4">
              {job.events.map((event) => (
                <li key={event.id} className="text-sm">
                  <p className="font-medium text-slate-900">{eventLabel(event.stage)}</p>
                  {event.message ? <p className="mt-0.5 break-words text-slate-700">{event.message}</p> : null}
                  {time(event.createdAt) ? <p className="mt-1 text-xs text-slate-500">{time(event.createdAt)}</p> : null}
                </li>
              ))}
            </ol>
          ) : (
            <p className="mt-3 text-sm text-slate-600">The service has not recorded an update yet.</p>
          )}
        </section>
        <ReplayControls job={job} />
      </div>

      {job.errors.length ? (
        <section className="mt-8 border-l-2 border-amber-500 bg-amber-50 px-4 py-3" aria-labelledby="job-errors">
          <h2 id="job-errors" className="text-sm font-semibold text-amber-950">Service notes</h2>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-amber-900">
            {job.errors.map((error, index) => <li key={`${error}-${index}`}>{error}</li>)}
          </ul>
        </section>
      ) : null}
    </section>
  );
}

export default function ScoutExperience({ enabled }: { enabled: boolean }) {
  const [query, setQuery] = useState("");
  const [jurisdiction, setJurisdiction] = useState("FL");
  const [job, setJob] = useState<ScoutJob | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [canceling, setCanceling] = useState(false);
  const [error, setError] = useState("");
  const [refreshError, setRefreshError] = useState("");
  const trackedFacts = useRef(new Set<string>());
  const unknownPolls = useRef(0);
  const pollJobId = job?.id;
  const pollJobStatus = job?.status;

  useEffect(() => {
    // Child passive effects can run before the root <Analytics /> effect has
    // installed Vercel's queue. Retry this one page-entry fact briefly rather
    // than silently dropping it; all later Scout events are user-driven.
    let attempts = 0;
    let timer: number | undefined;
    const emit = () => {
      const analyticsWindow = window as Window & { va?: unknown };
      if (typeof analyticsWindow.va === "function") {
        track("scout_opened", { availability: enabled ? "enabled" : "disabled" });
        return;
      }
      attempts += 1;
      if (attempts < 20) timer = window.setTimeout(emit, 50);
    };
    emit();
    return () => {
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [enabled]);

  useEffect(() => {
    if (!job) return;
    const trackOnce = (key: string, event: string, properties: Record<string, string | number | boolean>) => {
      if (trackedFacts.current.has(key)) return;
      const storageKey = `billcommons:scout:analytics:${key}`;
      try {
        if (window.localStorage.getItem(storageKey)) {
          trackedFacts.current.add(key);
          return;
        }
        window.localStorage.setItem(storageKey, "1");
      } catch {
        // Privacy modes may deny storage. The in-memory set still prevents
        // duplicate polling emissions for the current component lifetime.
      }
      trackedFacts.current.add(key);
      track(event, properties);
    };
    for (const fact of scoutAnalyticsFacts(job)) {
      trackOnce(fact.key, fact.event, fact.properties);
    }
    const browserUsage = scoutBrowserProviderUsage(job);
    if (job.status === "running") trackOnce(`${job.id}:status:running`, "scout_job_started", { jurisdiction: job.jurisdiction });
    if (job.status === "partial") trackOnce(`${job.id}:status:partial`, "scout_job_partial", { findings: job.findings.length });
    if (job.status === "complete") trackOnce(`${job.id}:status:complete`, "scout_job_completed", {
      jurisdiction: job.jurisdiction,
      findings: job.findings.length,
      sources: job.sources.length,
      direct_used: job.sources.some((source) => source.retrievalMechanism === "direct"),
      browser_used: browserUsage.sessions > 0,
      browser_seconds: browserUsage.runtimeSeconds,
    });
    if (job.status === "failed") trackOnce(`${job.id}:status:failed`, "scout_job_failed", { errors: job.errors.length });
  }, [job]);

  useEffect(() => {
    if (!pollJobId || !pollJobStatus || isScoutTerminal(pollJobStatus)) return;
    let active = true;
    const controller = new AbortController();
    let timer: number | undefined;

    const schedule = (status: ScoutJob["status"]) => {
      unknownPolls.current = status === "unknown" ? unknownPolls.current + 1 : 0;
      const delay = scoutPollRetryDelay(status, unknownPolls.current);
      if (delay === undefined && status === "unknown" && active) {
        setRefreshError("Scout returned an unrecognized status repeatedly. Refresh the page to try again.");
      }
      if (delay !== undefined && active) timer = window.setTimeout(poll, delay);
    };
    const poll = async () => {
      try {
        const next = await getScoutJob(pollJobId, controller.signal);
        if (!active) return;
        setJob(next);
        setRefreshError("");
        schedule(next.status);
      } catch (reason) {
        if (!active || (reason instanceof DOMException && reason.name === "AbortError")) return;
        setRefreshError(reason instanceof Error ? reason.message : "Scout could not refresh this job. Retrying shortly.");
        schedule(pollJobStatus);
      }
    };

    schedule(pollJobStatus);
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
      controller.abort();
    };
  }, [pollJobId, pollJobStatus]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = query.trim();
    if (!trimmed) {
      setError("Enter a research question before starting Scout.");
      return;
    }
    if (!enabled) {
      setError("Scout is not enabled in this environment, so no research job was created.");
      return;
    }
    setSubmitting(true);
    setError("");
    setRefreshError("");
    try {
      const next = await createScoutJob(trimmed, jurisdiction);
      setJob(next);
      track("scout_job_created", {
        jurisdiction: next.jurisdiction,
        status: next.status,
        cache: next.cacheHit ? "hit" : "miss",
      });
      if (next.cacheHit) track("scout_cache_hit", { jurisdiction: next.jurisdiction });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Scout could not start this research job.");
    } finally {
      setSubmitting(false);
    }
  }

  async function cancel() {
    if (!job || isScoutTerminal(job.status) || canceling) return;
    setCanceling(true);
    setError("");
    try {
      const next = await cancelScoutJob(job.id);
      setJob(next);
      track("scout_job_cancel_requested", { status: next.status });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Scout could not cancel this research job.");
    } finally {
      setCanceling(false);
    }
  }

  return (
    <div className="mx-auto max-w-6xl px-4 py-8 sm:px-6 sm:py-10">
      <div className="border-b border-slate-300 pb-6">
        <p className="page-eyebrow">Scout</p>
        <div className="mt-2 flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2">
          <h1 className="text-2xl font-semibold tracking-[-0.025em] text-slate-950 sm:text-3xl">Research government activity</h1>
          <p className="text-sm text-slate-600">Florida · official sources · evidence retained</p>
        </div>
        <p className="mt-3 max-w-3xl text-sm leading-6 text-slate-600">
          Bill Commons is checked first. Scout then uses admitted official sources and keeps the record needed to assess each finding.
          <span className="ml-1"><Link href="/account/login" className="font-medium text-blue-800 underline underline-offset-2">Sign in</Link> to create and view research jobs.</span>
        </p>
      </div>

      {!enabled ? (
        <div className="mt-8 rounded-md border border-amber-200 bg-amber-50 p-4 text-sm text-amber-950" role="status">
          Scout is not enabled in this environment. This route remains visible so its availability is not hidden; it cannot start research here.
        </div>
      ) : null}

      <form onSubmit={submit} className="mt-6 border-b border-slate-300 pb-5" aria-describedby="scout-help">
        <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_10rem_auto] md:items-end">
          <div>
            <label htmlFor="scout-query" className="sr-only">Research question</label>
            <input id="scout-query" required minLength={3} maxLength={500} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Research Florida legislation, an agency notice, or a committee action" className="w-full rounded-sm border border-slate-400 bg-white px-3 py-2.5 text-sm text-slate-950 placeholder:text-slate-400 focus:border-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-700/40" />
          </div>
          <div>
            <label htmlFor="scout-jurisdiction" className="sr-only">Jurisdiction</label>
            <select id="scout-jurisdiction" value={jurisdiction} onChange={(event) => setJurisdiction(event.target.value)} className="w-full rounded-sm border border-slate-400 bg-white px-3 py-2.5 text-sm text-slate-950 focus:border-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-700/40">
              {JURISDICTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
            </select>
          </div>
          <button type="submit" disabled={submitting || !enabled} className="rounded-sm bg-slate-950 px-5 py-2.5 text-sm font-semibold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50">
            {submitting ? "Starting…" : "Run research"}
          </button>
        </div>
        <div className="mt-3 flex flex-wrap items-baseline gap-x-4 gap-y-2 text-sm" aria-label="Example Scout questions">
          <span className="text-xs font-semibold uppercase tracking-[0.08em] text-slate-500">Suggested</span>
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              onClick={() => {
                setQuery(example);
                track("scout_example_selected", { jurisdiction: "FL" });
              }}
              className="text-left text-sm text-blue-800 underline underline-offset-2 hover:text-blue-600"
            >
              {example.replace(/^(Research|What changed recently in|Investigate)\s+/i, "")}
            </button>
          ))}
        </div>
        <p id="scout-help" className="mt-3 text-xs leading-5 text-slate-500">Matching active or fresh research may be reused. Progress is derived from durable service events, not a timer or estimated percentage.</p>
        {error ? (
          <p className="mt-3 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900" role="alert">
            {error}{error.includes("Sign in") ? <> <Link href="/account/login" className="font-semibold underline underline-offset-2">Sign in</Link>.</> : null}
          </p>
        ) : null}
      </form>

      {job ? <JobDetails job={job} refreshError={refreshError} onCancel={cancel} canceling={canceling} /> : null}
    </div>
  );
}
