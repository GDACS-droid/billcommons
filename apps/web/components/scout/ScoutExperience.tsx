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
const POLL_INTERVAL_MS = 2_500;

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

function StatusBadge({ job }: { job: ScoutJob }) {
  const colors: Record<ScoutJob["status"], string> = {
    queued: "bg-blue-100 text-blue-900",
    running: "bg-blue-100 text-blue-900",
    complete: "bg-emerald-100 text-emerald-900",
    partial: "bg-amber-100 text-amber-900",
    failed: "bg-red-100 text-red-900",
    canceled: "bg-slate-200 text-slate-800",
    unknown: "bg-slate-200 text-slate-800",
  };
  return (
    <span className={`inline-flex rounded-full px-2.5 py-1 text-xs font-semibold ${colors[job.status]}`}>
      {label(job.status)}
    </span>
  );
}

function FindingCard({ finding, source }: { finding: ScoutFinding; source?: ScoutJob["sources"][number] }) {
  const sourceUrl = source?.canonicalUrl ?? finding.sourceUrl;
  return (
    <article className="surface-card p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <h3 className="text-base font-semibold text-slate-950">{finding.title}</h3>
        {finding.confidence ? (
          <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-700">
            {label(finding.confidence)} confidence
          </span>
        ) : null}
      </div>
      {finding.relevantDate ? (
        <p className="mt-2 text-xs font-medium uppercase tracking-wide text-slate-500">
          Relevant date: {relevantDate(finding.relevantDate) ?? finding.relevantDate}
        </p>
      ) : null}
      <dl className="mt-4 space-y-3 text-sm leading-6">
        <div>
          <dt className="font-semibold text-slate-900">What happened</dt>
          <dd className="mt-1 whitespace-pre-wrap break-words text-slate-700">{finding.whatHappened}</dd>
        </div>
        <div>
          <dt className="font-semibold text-slate-900">Why it matters</dt>
          <dd className="mt-1 whitespace-pre-wrap break-words text-slate-700">{finding.whyItMatters}</dd>
        </div>
      </dl>
      <div className="mt-4 border-t border-slate-100 pt-3">
        <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Primary evidence</p>
        {finding.evidenceExcerpt ? (
          <blockquote className="mt-2 border-l-2 border-blue-200 pl-3 text-sm leading-6 text-slate-700">
            {finding.evidenceExcerpt}
          </blockquote>
        ) : (
          <p className="mt-2 text-sm text-slate-500">No excerpt was retained for this finding.</p>
        )}
        {sourceUrl ? (
          <a
            href={sourceUrl}
            target="_blank"
            rel="noreferrer noopener"
            onClick={() => track("scout_evidence_opened", { control: "finding" })}
            className="mt-3 inline-flex text-sm font-medium text-blue-800 underline underline-offset-2 hover:text-blue-600"
          >
            Open official source <span aria-hidden="true">↗</span>
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
      <h2 id="replay-heading" className="text-lg font-semibold text-slate-950">Browser replay</h2>
      <p className="mt-2 text-sm leading-6 text-slate-600">
        When Scout needed a browser, its replay shows how it navigated the official site. The retained source above remains the primary evidence.
      </p>
      {job.browserSessions.length ? (
        <ul className="mt-3 space-y-2 text-sm">
          {job.browserSessions.map((session) => (
            <li key={session.id} className="surface-card flex flex-wrap items-center justify-between gap-3 p-3">
              <span className="text-slate-700">Research browser: {label(session.status)}</span>
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
  const alertStyle =
    job.status === "failed"
      ? "border-red-200 bg-red-50 text-red-950"
      : job.status === "partial"
        ? "border-amber-200 bg-amber-50 text-amber-950"
        : "border-blue-200 bg-blue-50 text-blue-950";

  return (
    <section aria-labelledby="scout-results" className="mt-8">
      <div className={`rounded-md border p-4 text-sm ${alertStyle}`} role="status" aria-live="polite">
        <div className="flex flex-wrap items-center gap-3">
          <StatusBadge job={job} />
          <p className="font-medium">{scoutStatusSummary(job)}</p>
          {!terminal ? (
            <button type="button" onClick={onCancel} disabled={canceling} className="ml-auto text-xs font-semibold text-blue-900 underline underline-offset-2 hover:text-blue-700 disabled:cursor-not-allowed disabled:opacity-50">
              {canceling ? "Canceling…" : "Cancel research"}
            </button>
          ) : null}
        </div>
        {refreshError ? <p className="mt-2 text-xs">{refreshError}</p> : null}
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-[minmax(0,1fr)_19rem]">
        <div>
          <div className="flex flex-wrap items-baseline justify-between gap-3">
            <div>
              <p className="page-eyebrow">Verified findings</p>
              <h2 id="scout-results" className="mt-1 text-xl font-semibold tracking-tight text-slate-950">
                Evidence before conclusions
              </h2>
            </div>
            <p className="text-sm text-slate-500">
              {job.findings.length} {job.findings.length === 1 ? "finding" : "findings"}
            </p>
          </div>
          <div className="mt-4 space-y-4">
            {job.findings.map((finding) => (
              <FindingCard key={finding.id} finding={finding} source={sourceById.get(finding.sourceId ?? "")} />
            ))}
            {!job.findings.length ? (
              <div className="surface-card p-5 text-sm leading-6 text-slate-600">
                {terminal
                  ? "There are no verified findings to display for this job. This does not establish that no legislative activity occurred."
                  : "No verified finding has been recorded yet. Scout will show evidence only after the service retains it."}
              </div>
            ) : null}
          </div>
        </div>

        <aside className="space-y-5" aria-label="Scout job details">
          <section className="surface-card p-4">
            <h2 className="text-sm font-semibold text-slate-950">Research record</h2>
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
          </section>

          <section className="surface-card p-4">
            <h2 className="text-sm font-semibold text-slate-950">Usage</h2>
            <dl className="mt-3 grid grid-cols-2 gap-3 text-sm">
              <div><dt className="text-slate-500">Requests</dt><dd className="font-semibold">{job.usage.externalRequests ?? "—"}</dd></div>
              <div><dt className="text-slate-500">Browser pages</dt><dd className="font-semibold">{job.usage.browserPages ?? "—"}</dd></div>
              <div><dt className="text-slate-500">Actions</dt><dd className="font-semibold">{job.usage.browserActions ?? "—"}</dd></div>
              <div><dt className="text-slate-500">Browser time</dt><dd className="font-semibold">{job.usage.browserRuntimeMs === undefined ? "—" : `${Math.round(job.usage.browserRuntimeMs / 1000)}s`}</dd></div>
            </dl>
          </section>
        </aside>
      </div>

      <section className="mt-8" aria-labelledby="sources-heading">
        <h2 id="sources-heading" className="text-lg font-semibold text-slate-950">Source metadata</h2>
        {job.sources.length ? (
          <ul className="mt-3 divide-y divide-slate-200 border-y border-slate-200">
            {job.sources.map((source) => (
              <li key={source.id} className="py-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="font-medium text-slate-950">{source.title}</p>
                    <p className="mt-1 break-words text-sm text-slate-600">
                      {[source.officialDomain, source.sourceType, source.retrievalMechanism, source.status, time(source.retrievedAt)]
                        .filter(Boolean)
                        .map(label)
                        .join(" · ") || "Metadata not recorded"}
                    </p>
                    {shortHash(source.contentHash) ? (
                      <p className="mt-1 font-mono text-xs text-slate-500">Content hash: {shortHash(source.contentHash)}</p>
                    ) : null}
                    {source.changeKind && source.changeSummary ? (
                      <p className="mt-2 text-xs leading-5 text-slate-600">
                        <span className="font-semibold text-slate-700">Change: {label(source.changeKind)}.</span>{" "}
                        {source.changeSummary}
                      </p>
                    ) : null}
                  </div>
                  {source.canonicalUrl ? (
                    <a href={source.canonicalUrl} target="_blank" rel="noreferrer noopener" onClick={() => track("scout_evidence_opened", { control: "source_metadata" })} className="shrink-0 text-sm font-medium text-blue-800 underline underline-offset-2 hover:text-blue-600">
                      Source <span aria-hidden="true">↗</span>
                    </a>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-3 text-sm text-slate-600">No source metadata has been retained for this job yet.</p>
        )}
      </section>

      <div className="mt-8 grid gap-6 md:grid-cols-2">
        <section aria-labelledby="events-heading">
          <h2 id="events-heading" className="text-lg font-semibold text-slate-950">Recorded updates</h2>
          {job.events.length ? (
            <ol className="mt-3 space-y-3 border-l border-slate-200 pl-4">
              {job.events.map((event) => (
                <li key={event.id} className="text-sm">
                  <p className="font-medium text-slate-900">{eventLabel(event.stage)}</p>
                  <p className="mt-0.5 break-words text-slate-700">{event.message}</p>
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
        <section className="mt-8 rounded-md border border-amber-200 bg-amber-50 p-4" aria-labelledby="job-errors">
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
  const lastTrackedState = useRef("");
  const trackedFacts = useRef(new Set<string>());

  useEffect(() => {
    track("scout_opened", { availability: enabled ? "enabled" : "disabled" });
  }, [enabled]);

  useEffect(() => {
    if (!job) return;
    for (const fact of scoutAnalyticsFacts(job)) {
      if (trackedFacts.current.has(fact.key)) continue;
      trackedFacts.current.add(fact.key);
      track(fact.event, fact.properties);
    }
    const stateKey = `${job.id}:${job.status}`;
    if (lastTrackedState.current === stateKey) return;
    lastTrackedState.current = stateKey;
    if (job.status === "running") track("scout_job_started", { jurisdiction: job.jurisdiction });
    if (job.status === "partial") track("scout_job_partial", { findings: job.findings.length });
    if (job.status === "complete") track("scout_job_completed", {
      jurisdiction: job.jurisdiction,
      findings: job.findings.length,
      sources: job.sources.length,
      direct_used: job.sources.some((source) => source.retrievalMechanism === "direct"),
      browser_used: job.browserSessions.length > 0,
      browser_seconds: Math.round((job.usage.browserRuntimeMs ?? 0) / 1000),
    });
    if (job.status === "failed") track("scout_job_failed", { errors: job.errors.length });
  }, [job]);

  useEffect(() => {
    if (!job || isScoutTerminal(job.status)) return;
    let active = true;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      try {
        const next = await getScoutJob(job.id, controller.signal);
        if (!active) return;
        setJob(next);
        setRefreshError("");
      } catch (reason) {
        if (!active || (reason instanceof DOMException && reason.name === "AbortError")) return;
        setRefreshError(reason instanceof Error ? reason.message : "Scout could not refresh this job. Retrying shortly.");
      }
    }, POLL_INTERVAL_MS);
    return () => {
      active = false;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [job]);

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
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6 sm:py-14">
      <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_18rem] lg:items-end">
        <div>
          <p className="page-eyebrow">Scout · authenticated research</p>
          <h1 className="page-title">Follow the official record.</h1>
          <p className="page-lead">
            Scout checks Bill Commons&apos; data and admitted Florida government sources. It presents findings only with retained source metadata and evidence excerpts.
          </p>
        </div>
        <aside className="rounded-md border border-slate-200 bg-slate-50 p-4 text-sm leading-6 text-slate-600">
          <p className="font-semibold text-slate-900">Currently researching Florida legislation</p>
          <p className="mt-1">Scout does not accept a URL or conduct a broad web crawl. <Link href="/account/login" className="font-medium text-blue-800 underline underline-offset-2">Sign in</Link> to create and view your research jobs.</p>
        </aside>
      </div>

      {!enabled ? (
        <div className="mt-8 rounded-md border border-amber-200 bg-amber-50 p-4 text-sm text-amber-950" role="status">
          Scout is not enabled in this environment. This route remains visible so its availability is not hidden; it cannot start research here.
        </div>
      ) : null}

      <form onSubmit={submit} className="surface-card mt-8 p-5 sm:p-6" aria-describedby="scout-help">
        <div className="grid gap-5 md:grid-cols-[minmax(0,1fr)_11rem_auto] md:items-end">
          <div>
            <label htmlFor="scout-query" className="block text-sm font-semibold text-slate-950">What should Scout investigate?</label>
            <textarea id="scout-query" required minLength={3} maxLength={500} rows={3} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="For example: Research Florida legislation involving artificial intelligence." className="mt-2 w-full resize-y rounded-md border border-slate-300 bg-white px-3 py-2 text-sm leading-6 text-slate-950 placeholder:text-slate-400 focus:border-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-700/40" />
          </div>
          <div>
            <label htmlFor="scout-jurisdiction" className="block text-sm font-semibold text-slate-950">Jurisdiction</label>
            <select id="scout-jurisdiction" value={jurisdiction} onChange={(event) => setJurisdiction(event.target.value)} className="mt-2 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-950 focus:border-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-700/40">
              {JURISDICTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
            </select>
          </div>
          <button type="submit" disabled={submitting || !enabled} className="rounded-md bg-blue-700 px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-blue-800 disabled:cursor-not-allowed disabled:opacity-50">
            {submitting ? "Starting research…" : "Start Scout"}
          </button>
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-2" aria-label="Example Scout questions">
          <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">Try an example</span>
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              onClick={() => {
                setQuery(example);
                track("scout_example_selected", { jurisdiction: "FL" });
              }}
              className="rounded-full border border-slate-300 bg-white px-3 py-1.5 text-left text-xs font-medium text-slate-700 hover:border-blue-400 hover:text-blue-800 focus:outline-none focus:ring-2 focus:ring-blue-700/40"
            >
              {example.replace(/^(Research|What changed recently in|Investigate)\s+/i, "")}
            </button>
          ))}
        </div>
        <p id="scout-help" className="mt-3 text-xs leading-5 text-slate-500">A matching active or fresh job may be reused to avoid duplicate research. Scout records actual service updates; it does not estimate progress.</p>
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
