import { API_BASE } from "@/lib/config";

export interface OfficialTarget {
  target_id: string;
  adapter_name: string;
  source_url: string;
  enabled: boolean;
  state: string;
  retrieved_at: string | null;
  next_check_at: string;
  raw_sha256: string | null;
  scope: { day?: string };
}

export interface OfficialSourceOverview {
  generated_at: string;
  jurisdiction_count: number;
  items: { jurisdiction: string; official_freshness: string; targets: OfficialTarget[] }[];
}

export function sourceNeedsAttention(target: OfficialTarget) {
  return target.enabled && target.state !== "observed";
}

function stateLabel(state: string): string {
  return ({ disabled: "Check disabled", not_observed: "Awaiting first observation",
    failed: "Source check failed", observation_overdue: "Observation overdue",
    observed: "Source observed" } as Record<string, string>)[state] ?? "Check status unavailable";
}

function sourceLabel(target: OfficialTarget): string {
  if (target.adapter_name === "ca_official_actions") {
    return `California action delta${target.scope.day ? ` · ${target.scope.day}` : ""}`;
  }
  if (target.adapter_name === "fl_senate_bill_history") return "Florida bill history";
  if (target.adapter_name === "official_link_discovery") return "Official website links";
  return "Official source";
}

function timestamp(value: string | null): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Not recorded"
    : `${date.toISOString().replace("T", " ").slice(0, 16)} UTC`;
}

function officialUrl(value: string): string | undefined {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password ? url.toString() : undefined;
  } catch {
    return undefined;
  }
}

export default function OfficialSourceChecks({ jurisdiction, targets }: {
  jurisdiction: string;
  targets: OfficialTarget[];
}) {
  return <div>
    <h3 className="font-semibold text-slate-950">Official-source checks</h3>
    <p className="mt-1 text-sm text-slate-600">
      Website-link checks discover material; action-delta checks compare a limited published archive.
      Bill-history checks retain facts from one official bill page. These checks do not establish statewide completeness.
    </p>
    {targets.length === 0 ? <p className="mt-3">No official-source targets are recorded for this jurisdiction.</p> :
      <ul className="mt-3 divide-y divide-slate-200">
        {targets.map((target) => {
          const sourceUrl = officialUrl(target.source_url);
          return <li key={target.target_id} className="py-3">
            <p className="font-medium text-slate-900">
              {sourceLabel(target)}
              {" — "}{stateLabel(target.state)}
            </p>
            <p className="mt-1 text-sm tabular-nums text-slate-600">
              Last observation: {timestamp(target.retrieved_at)}.
              {target.enabled ? ` Next check: ${timestamp(target.next_check_at)}.` : " Automatic checks are disabled."}
            </p>
            <div className="mt-2 flex flex-wrap gap-x-5 gap-y-2 text-blue-800">
              {sourceUrl && <a href={sourceUrl} className="underline underline-offset-2">Visit source</a>}
              {target.raw_sha256 && /^[a-f0-9]{64}$/.test(target.raw_sha256) && <a
                href={new URL(`/api/v1/official-evidence/blobs/${target.raw_sha256}`, API_BASE).toString()}
                className="underline underline-offset-2">Download retained response</a>}
            </div>
          </li>;
        })}
      </ul>}
    <a href={new URL(`/api/v1/official-evidence/observations?jurisdiction=${encodeURIComponent(jurisdiction)}`, API_BASE).toString()}
      className="mt-3 inline-block text-blue-800 underline underline-offset-2">Read observation history and evidence identifiers</a>
  </div>;
}
