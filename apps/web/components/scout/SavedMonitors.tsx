"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  getScoutMonitorRuns,
  isScoutMonitorEligible,
  getScoutMonitorOverview,
  scoutMonitorCadences,
  type ScoutMonitorPolicy,
  saveScoutMonitor,
  updateScoutMonitor,
  type ScoutJob,
  type ScoutMonitor,
  type ScoutMonitorRun,
} from "@/lib/scout";

const CADENCES = [
  { seconds: 6 * 60 * 60, label: "Every 6 hours" },
  { seconds: 24 * 60 * 60, label: "Daily" },
  { seconds: 7 * 24 * 60 * 60, label: "Weekly" },
];

function when(value?: string): string {
  if (!value) return "time not recorded";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function cadenceLabel(seconds: number): string {
  return CADENCES.find((cadence) => cadence.seconds === seconds)?.label ?? (seconds % 3600 === 0 ? `Every ${seconds / 3600} hours` : seconds % 60 === 0 ? `Every ${seconds / 60} minutes` : `Every ${seconds} seconds`);
}

function count(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : undefined;
}

function arrayOrCount(value: unknown, fallback: unknown): number | undefined {
  return Array.isArray(value) ? value.length : count(fallback);
}

function comparisonState(run: ScoutMonitorRun): "deferred" | "pending" | "unavailable" | "available" | "missing" {
  if (run.status === "deferred") return "deferred";
  if (["queued", "running", "scheduled"].includes(run.status)) return "pending";
  if (["failed", "canceled", "cancelled"].includes(run.status)) return "unavailable";
  const summary = run.changeSummary;
  return arrayOrCount(summary.new_sources, summary.new_source_count) !== undefined
    && arrayOrCount(summary.changed_sources, summary.changed_source_count) !== undefined
    && count(summary.unchanged_source_count) !== undefined
    ? "available"
    : "missing";
}

function RunSummary({ run }: { run: ScoutMonitorRun }) {
  const summary = run.changeSummary;
  const baseline = summary.baseline === true || run.executionMode === "baseline";
  const newSources = arrayOrCount(summary.new_sources, summary.new_source_count);
  const changedSources = arrayOrCount(summary.changed_sources, summary.changed_source_count);
  const unchanged = count(summary.unchanged_source_count);
  const observed = count(summary.observed_source_count);
  const absenceEvaluated = summary.absence_evaluated === true;
  const comparison = comparisonState(run);
  const mode = comparison === "deferred" ? "Research not started" : run.executionMode === "cached" ? "Cached matching research" : run.executionMode === "coalesced" ? "Joined active research" : run.executionMode === "new" ? "Fresh research" : run.executionMode.replaceAll("_", " ");
  return (
    <article className="border-t border-slate-200 py-4 first:border-t-0 first:pt-0">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <p className="font-medium text-slate-950">{baseline ? "Baseline evidence" : run.status.replaceAll("_", " ")}</p>
          <p className="mt-1 text-xs text-slate-500">{when(run.completedAt ?? run.scheduledFor)} · {mode}</p>
        </div>
        {run.job?.id ? <Link href={`/scout?job=${encodeURIComponent(run.job.id)}`} className="text-sm font-semibold text-blue-800 underline underline-offset-2 hover:text-blue-600">Open evidence</Link> : null}
      </div>
      {baseline ? (
        <p className="mt-3 text-sm leading-6 text-slate-700">This saved result is the comparison baseline. {observed === undefined ? "Observed source count was not returned." : `${observed} retained ${observed === 1 ? "source" : "sources"} observed.`}</p>
      ) : (
        <div className="mt-3 text-sm leading-6 text-slate-700">
          {comparison === "available" ? <p>{newSources} new · {changedSources} changed · {unchanged} unchanged observed sources.</p> : null}
          {comparison === "available" ? <p className="mt-1 text-slate-600">Compared with the previous observed result. Previously seen sources can reappear after an incomplete run.</p> : null}
          {comparison === "pending" ? <p>Comparison is pending. Source-change counts are not available yet.</p> : null}
          {comparison === "deferred" ? <p>This attempt was deferred before research started. No comparison was made. An active monitor will try again at its next due time.</p> : null}
          {comparison === "unavailable" ? <p>Comparison is unavailable for this run.</p> : null}
          {comparison === "missing" ? <p>Comparison counts were not returned for this run.</p> : null}
          <p className="mt-1 text-slate-600">{absenceEvaluated ? "Absence evaluation was recorded." : "Source absence is not evaluated: an incomplete fetch cannot prove a source disappeared."}</p>
          {run.errorClass ? <p className="mt-1 text-amber-800">Run note: {run.errorClass.replaceAll("_", " ")}</p> : null}
        </div>
      )}
    </article>
  );
}

function MonitorHistory({ monitor }: { monitor: ScoutMonitor }) {
  const [runs, setRuns] = useState<ScoutMonitorRun[]>([]);
  const [cursor, setCursor] = useState<string>();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function load(nextCursor?: string) {
    setLoading(true); setError("");
    try {
      const page = await getScoutMonitorRuns(monitor.id, nextCursor);
      setRuns((current) => nextCursor ? [...current, ...page.runs] : page.runs);
      setCursor(page.nextCursor);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Scout could not load monitor history.");
    } finally { setLoading(false); }
  }

  return (
    <div className="mt-4 border-t border-slate-200 pt-4">
      <button type="button" onClick={() => { const next = !open; setOpen(next); if (next && !runs.length) void load(); }} className="text-sm font-semibold text-blue-800 underline underline-offset-2 hover:text-blue-600">
        {open ? "Hide run history" : "View run history"}
      </button>
      {open ? <div className="mt-4" aria-live="polite">
        {loading && !runs.length ? <p className="text-sm text-slate-600">Loading retained run history…</p> : null}
        {runs.map((run) => <RunSummary key={run.id} run={run} />)}
        {!loading && !runs.length && !error ? <p className="text-sm text-slate-600">No monitor runs have been recorded yet.</p> : null}
        {cursor ? <button type="button" disabled={loading} onClick={() => void load(cursor)} className="mt-3 text-sm font-semibold text-blue-800 underline underline-offset-2 hover:text-blue-600 disabled:opacity-50">{loading ? "Loading…" : "Load earlier runs"}</button> : null}
        {error ? <p role="alert" className="mt-3 text-sm text-red-800">{error}</p> : null}
      </div> : null}
    </div>
  );
}

function MonitorRow({ monitor, policy, onChange, onMutationStart, onMutationSettled }: { monitor: ScoutMonitor; policy?: ScoutMonitorPolicy; onChange: (monitor: ScoutMonitor) => void; onMutationStart: () => void; onMutationSettled: () => void }) {
  const [cadence, setCadence] = useState(monitor.cadenceSeconds);
  const choices = scoutMonitorCadences(policy, cadence);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  async function update(change: { active?: boolean; cadenceSeconds?: number }) {
    onMutationStart();
    setSaving(true); setError("");
    try { const next = await updateScoutMonitor(monitor.id, change); setCadence(next.cadenceSeconds); onChange(next); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Scout could not update this monitor."); }
    finally { onMutationSettled(); setSaving(false); }
  }
  return <li className="border-t border-slate-200 py-5 first:border-t-0 first:pt-0">
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div className="min-w-0">
        <p className="font-semibold text-slate-950">{monitor.query}</p>
        <p className="mt-1 text-sm text-slate-600">{monitor.jurisdiction} · {cadenceLabel(monitor.cadenceSeconds)} · {monitor.active ? `Next due ${when(monitor.nextRunAt)}` : "Paused"}</p>
        {monitor.consecutiveDeferrals ? <p className="mt-1 text-xs text-amber-800">{monitor.consecutiveDeferrals} deferred {monitor.consecutiveDeferrals === 1 ? "run" : "runs"}</p> : null}
      </div>
      <button type="button" disabled={saving} onClick={() => void update({ active: !monitor.active })} className="text-sm font-semibold text-blue-800 underline underline-offset-2 hover:text-blue-600 disabled:opacity-50">{saving ? "Saving…" : monitor.active ? "Pause" : "Resume"}</button>
    </div>
    <div className="mt-4 flex flex-wrap items-end gap-3">
      <label className="block text-xs font-semibold text-slate-700">Cadence<select value={cadence} onChange={(event) => setCadence(Number(event.target.value))} className="mt-1 block rounded-sm border border-slate-400 bg-white px-2 py-1.5 text-sm text-slate-950"><option value={cadence}>{cadenceLabel(cadence)}</option>{choices.filter((seconds) => seconds !== cadence).map((seconds) => <option key={seconds} value={seconds}>{cadenceLabel(seconds)}</option>)}</select></label>
      <button type="button" disabled={saving || cadence === monitor.cadenceSeconds || !choices.includes(cadence)} onClick={() => void update({ cadenceSeconds: cadence })} className="pb-1.5 text-sm font-semibold text-blue-800 underline underline-offset-2 hover:text-blue-600 disabled:no-underline disabled:opacity-50">Update cadence</button>
    </div>
    {monitor.jurisdiction === "CA" ? <p className="mt-4 text-xs leading-5 text-slate-600">California monitor runs compare retained weekday archive evidence. They do not establish a current or complete bill history.</p> : null}
    <MonitorHistory monitor={monitor} />
    {error ? <p role="alert" className="mt-3 text-sm text-red-800">{error}</p> : null}
  </li>;
}

type ListStatus = "loading" | "ready" | "error";

export default function SavedMonitors({ job }: { job?: ScoutJob }) {
  const [monitors, setMonitors] = useState<ScoutMonitor[]>([]);
  const [policy, setPolicy] = useState<ScoutMonitorPolicy>();
  const [listStatus, setListStatus] = useState<ListStatus>("loading");
  const [listError, setListError] = useState("");
  const [saveError, setSaveError] = useState("");
  const [cadence, setCadence] = useState(CADENCES[1].seconds);
  const [saving, setSaving] = useState(false);
  const monitorVersion = useRef(0);
  const pendingMutations = useRef(0);
  const eligible = job ? isScoutMonitorEligible(job) : false;

  const refresh = useCallback(async () => {
    const version = monitorVersion.current;
    setListStatus("loading");
    setListError("");
    try {
      const next = await getScoutMonitorOverview();
      if (monitorVersion.current !== version) return;
      setMonitors(next.monitors);
      setPolicy(next.policy);
      if (next.policy) {
        const { minCadenceSeconds, maxCadenceSeconds } = next.policy;
        setCadence((current) => Math.max(minCadenceSeconds, Math.min(maxCadenceSeconds, current)));
      }
      setListStatus("ready");
    } catch (reason) {
      if (monitorVersion.current !== version) return;
      setListError(reason instanceof Error ? reason.message : "Scout could not load saved monitors.");
      setListStatus("error");
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  function acceptMonitor(next: ScoutMonitor) {
    setMonitors((current) => current.some((item) => item.id === next.id)
      ? current.map((item) => item.id === next.id ? next : item)
      : [next, ...current]);
  }

  function beginMutation() {
    monitorVersion.current += 1;
    pendingMutations.current += 1;
  }

  function settleMutation() {
    pendingMutations.current -= 1;
    if (!pendingMutations.current) void refresh();
  }

  async function save() {
    if (!job || !eligible) return;
    beginMutation();
    setSaving(true); setSaveError("");
    try {
      const result = await saveScoutMonitor(job.id, cadence);
      acceptMonitor(result.monitor);
    } catch (reason) {
      setSaveError(reason instanceof Error ? reason.message : "Scout could not save this monitor.");
    } finally {
      settleMutation();
      setSaving(false);
    }
  }

  const savedCount = listStatus === "ready" ? `${monitors.length}${policy ? `/${policy.maxSavedMonitors}` : ""} saved` : listStatus === "loading" ? "Loading saved monitors…" : "Saved monitor count unavailable";
  const limitReached = listStatus === "ready" && policy !== undefined && monitors.length >= policy.maxSavedMonitors;
  const saveDisabled = saving || listStatus !== "ready" || limitReached;
  const saveLabel = saving ? "Saving…" : listStatus === "loading" ? "Loading saved monitors…" : listStatus === "error" ? "Saved monitor count unavailable" : limitReached ? "Monitor limit reached" : "Save monitor";
  return <section className="mt-10 border-y border-slate-300 py-6" aria-labelledby="saved-monitors-heading">
    <div className="flex flex-wrap items-baseline justify-between gap-3"><div><h2 id="saved-monitors-heading" className="text-xl font-semibold tracking-tight text-slate-950">Saved monitors</h2><p className="mt-2 max-w-3xl text-sm leading-6 text-slate-600">{policy ? `Keep up to ${policy.maxSavedMonitors} evidence-backed ${policy.maxSavedMonitors === 1 ? "query" : "queries"} on a cadence.` : "Keep evidence-backed queries on a cadence."} A run records observed source changes; it does not claim a source was removed.</p></div><p className="text-sm text-slate-500">{savedCount}</p></div>
    {job ? <div className="mt-5 border-t border-slate-200 pt-4"><p className="text-sm font-semibold text-slate-900">Save this research result</p>{eligible ? <div className="mt-3 flex flex-wrap items-end gap-3"><label className="text-sm text-slate-700">Cadence<select value={cadence} onChange={(event) => setCadence(Number(event.target.value))} className="ml-2 rounded-sm border border-slate-400 bg-white px-2 py-1.5 text-sm text-slate-950">{scoutMonitorCadences(policy, cadence).map((seconds) => <option key={seconds} value={seconds}>{cadenceLabel(seconds)}</option>)}</select></label><button type="button" disabled={saveDisabled} onClick={() => void save()} className="rounded-sm bg-slate-950 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-800 disabled:opacity-50">{saveLabel}</button></div> : <p className="mt-2 text-sm leading-6 text-slate-600">Only completed or partial results with retained findings can become monitors. Operator and canary research cannot be saved.</p>}</div> : null}
    {listStatus === "loading" ? <p className="mt-6 text-sm text-slate-600">Loading saved monitors…</p> : null}
    {listStatus === "ready" && !monitors.length ? <p className="mt-6 text-sm text-slate-600">No saved monitors yet. Save an eligible evidence result to begin a bounded comparison history.</p> : null}
    {monitors.length ? <ul className="mt-6">{monitors.map((monitor) => <MonitorRow key={monitor.id} monitor={monitor} policy={policy} onChange={acceptMonitor} onMutationStart={beginMutation} onMutationSettled={settleMutation} />)}</ul> : null}
    {listError ? <p role="alert" className="mt-4 text-sm text-red-800">{listError}</p> : null}
    {saveError ? <p role="alert" className="mt-4 text-sm text-red-800">{saveError}</p> : null}
  </section>;
}
