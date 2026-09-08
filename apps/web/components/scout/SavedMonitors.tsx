"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  getScoutMonitorRuns,
  isScoutMonitorEligible,
  listScoutMonitors,
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
  return CADENCES.find((cadence) => cadence.seconds === seconds)?.label ?? `${Math.max(1, Math.round(seconds / 3600))} hours`;
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function RunSummary({ run }: { run: ScoutMonitorRun }) {
  const summary = run.changeSummary;
  const baseline = summary.baseline === true || run.executionMode === "baseline";
  const newSources = Array.isArray(summary.new_sources) ? summary.new_sources.length : count(summary.new_source_count);
  const changedSources = Array.isArray(summary.changed_sources) ? summary.changed_sources.length : count(summary.changed_source_count);
  const unchanged = count(summary.unchanged_source_count);
  const observed = count(summary.observed_source_count);
  const absenceEvaluated = summary.absence_evaluated === true;
  const mode = run.executionMode === "cached" ? "Cached matching research" : run.executionMode === "coalesced" ? "Joined active research" : run.executionMode === "new" ? "Fresh research" : run.executionMode.replaceAll("_", " ");
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
        <p className="mt-3 text-sm leading-6 text-slate-700">This saved result is the comparison baseline. {observed ? `${observed} retained ${observed === 1 ? "source" : "sources"} observed.` : "Observed source count was not returned."}</p>
      ) : (
        <div className="mt-3 text-sm leading-6 text-slate-700">
          <p>{newSources} new · {changedSources} changed · {unchanged} unchanged observed sources.</p>
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

function MonitorRow({ monitor, onChange }: { monitor: ScoutMonitor; onChange: (monitor: ScoutMonitor) => void }) {
  const [cadence, setCadence] = useState(monitor.cadenceSeconds);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  async function update(change: { active?: boolean; cadenceSeconds?: number }) {
    setSaving(true); setError("");
    try { const next = await updateScoutMonitor(monitor.id, change); setCadence(next.cadenceSeconds); onChange(next); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Scout could not update this monitor."); }
    finally { setSaving(false); }
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
      <label className="block text-xs font-semibold text-slate-700">Cadence<select value={cadence} onChange={(event) => setCadence(Number(event.target.value))} className="mt-1 block rounded-sm border border-slate-400 bg-white px-2 py-1.5 text-sm text-slate-950"><option value={cadence}>{cadenceLabel(cadence)}</option>{CADENCES.filter((item) => item.seconds !== cadence).map((item) => <option key={item.seconds} value={item.seconds}>{item.label}</option>)}</select></label>
      <button type="button" disabled={saving || cadence === monitor.cadenceSeconds} onClick={() => void update({ cadenceSeconds: cadence })} className="pb-1.5 text-sm font-semibold text-blue-800 underline underline-offset-2 hover:text-blue-600 disabled:no-underline disabled:opacity-50">Update cadence</button>
    </div>
    {monitor.jurisdiction === "CA" ? <p className="mt-4 text-xs leading-5 text-slate-600">California monitor runs compare retained weekday archive evidence. They do not establish a current or complete bill history.</p> : null}
    <MonitorHistory monitor={monitor} />
    {error ? <p role="alert" className="mt-3 text-sm text-red-800">{error}</p> : null}
  </li>;
}

export default function SavedMonitors({ job }: { job?: ScoutJob }) {
  const [monitors, setMonitors] = useState<ScoutMonitor[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [cadence, setCadence] = useState(CADENCES[1].seconds);
  const [saving, setSaving] = useState(false);
  const eligible = job ? isScoutMonitorEligible(job) : false;
  async function refresh() { try { setMonitors(await listScoutMonitors()); setError(""); } catch (reason) { setError(reason instanceof Error ? reason.message : "Scout could not load saved monitors."); } finally { setLoading(false); } }
  useEffect(() => { void refresh(); }, []);
  async function save() { if (!job || !eligible) return; setSaving(true); setError(""); try { const result = await saveScoutMonitor(job.id, cadence); setMonitors((current) => [result.monitor, ...current.filter((item) => item.id !== result.monitor.id)]); } catch (reason) { setError(reason instanceof Error ? reason.message : "Scout could not save this monitor."); } finally { setSaving(false); } }
  return <section className="mt-10 border-y border-slate-300 py-6" aria-labelledby="saved-monitors-heading">
    <div className="flex flex-wrap items-baseline justify-between gap-3"><div><h2 id="saved-monitors-heading" className="text-xl font-semibold tracking-tight text-slate-950">Saved monitors</h2><p className="mt-2 max-w-3xl text-sm leading-6 text-slate-600">Keep up to three evidence-backed queries on a cadence. A run records observed source changes; it does not claim a source was removed.</p></div><p className="text-sm text-slate-500">{monitors.length}/3 saved</p></div>
    {job ? <div className="mt-5 border-t border-slate-200 pt-4"><p className="text-sm font-semibold text-slate-900">Save this research result</p>{eligible ? <div className="mt-3 flex flex-wrap items-end gap-3"><label className="text-sm text-slate-700">Cadence<select value={cadence} onChange={(event) => setCadence(Number(event.target.value))} className="ml-2 rounded-sm border border-slate-400 bg-white px-2 py-1.5 text-sm text-slate-950">{CADENCES.map((item) => <option key={item.seconds} value={item.seconds}>{item.label}</option>)}</select></label><button type="button" disabled={saving || monitors.length >= 3} onClick={() => void save()} className="rounded-sm bg-slate-950 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-800 disabled:opacity-50">{saving ? "Saving…" : monitors.length >= 3 ? "Monitor limit reached" : "Save monitor"}</button></div> : <p className="mt-2 text-sm leading-6 text-slate-600">Only completed or partial results with retained findings can become monitors. Operator and canary research cannot be saved.</p>}</div> : null}
    {loading ? <p className="mt-6 text-sm text-slate-600">Loading saved monitors…</p> : null}
    {!loading && !monitors.length ? <p className="mt-6 text-sm text-slate-600">No saved monitors yet. Save an eligible evidence result to begin a bounded comparison history.</p> : null}
    {monitors.length ? <ul className="mt-6">{monitors.map((monitor) => <MonitorRow key={monitor.id} monitor={monitor} onChange={(next) => setMonitors((current) => current.map((item) => item.id === next.id ? next : item))} />)}</ul> : null}
    {error ? <p role="alert" className="mt-4 text-sm text-red-800">{error}</p> : null}
  </section>;
}
