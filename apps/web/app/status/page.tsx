import type { Metadata } from "next";
import Link from "next/link";
import PageHeader from "@/components/PageHeader";
import { apiGet } from "@/lib/api";

/**
 * A status page that actually probes.
 *
 * status.billcommons.org used to rewrite to /coverage, which reports how much
 * DATA we have per state -- a genuinely useful page that says nothing about
 * whether the service is answering. On 2026-08-02 the API was down for hours
 * while /coverage rendered perfectly from Next's cache.
 *
 * So: no cache, and the checks run against the API at request time. If the API
 * is down this page must say so, which it cannot do from a stored copy.
 */

export const metadata: Metadata = {
  title: "Status",
  description:
    "Live service status for the Bill Commons API and MCP server, checked at page load.",
  alternates: { canonical: "/status" },
};

// Every read here is deliberately uncached. A cached status page is a lie
// with a timestamp on it.
export const dynamic = "force-dynamic";

interface Health {
  status: string;
  database: string;
  pool: {
    in_use: number;
    idle: number;
    capacity: number;
    saturation: number | null;
  } | null;
  concurrency: {
    in_flight: number;
    max_concurrent: number;
    shed_total: number;
  } | null;
}

interface Usage {
  window_days: number;
  total_tool_calls: number;
  self_probe_calls: number;
}

interface CoverageRow {
  jurisdiction_code: string;
  status: string;
}

function Dot({ ok }: { ok: boolean }) {
  return (
    <span
      aria-hidden
      className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${
        ok ? "bg-emerald-500" : "bg-rose-500"
      }`}
    />
  );
}

function Row({
  label,
  ok,
  detail,
}: {
  label: string;
  ok: boolean;
  detail: string;
}) {
  return (
    <div className="flex items-center gap-3 border-b border-slate-100 py-3 last:border-0">
      <Dot ok={ok} />
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium text-slate-900">{label}</div>
        <div className="text-xs text-slate-600">{detail}</div>
      </div>
      <div
        className={`shrink-0 text-xs font-medium ${
          ok ? "text-emerald-700" : "text-rose-700"
        }`}
      >
        {ok ? "operational" : "not responding"}
      </div>
    </div>
  );
}

export default async function StatusPage() {
  const started = Date.now();
  const [health, usage, coverage] = await Promise.all([
    apiGet<Health>("/api/v1/health"),
    apiGet<Usage>("/api/v1/stats/usage", { days: 7 }),
    apiGet<{ data: CoverageRow[] }>("/api/v1/coverage", { per_page: 60 }),
  ]);
  const elapsed = Date.now() - started;

  // The API returns HTTP 200 with database:"error" when the pool is exhausted.
  // Checking ok alone would report healthy through exactly the failure this
  // page exists to surface.
  const apiUp = health.ok && health.data.database === "ok";
  const pool = health.ok ? health.data.pool : null;
  const conc = health.ok ? health.data.concurrency : null;

  const rows = coverage.ok ? coverage.data.data : [];
  const green = rows.filter((r) => r.status === "GREEN").length;
  const impaired = rows.filter(
    (r) => r.status === "DEGRADED" || r.status === "BLOCKED"
  );

  return (
    <div className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Status"
        title={apiUp ? "All systems operational" : "Service disruption"}
        description={
          <p>
            Checked just now, against the live API — this page is never cached.
            A status page served from cache reports health it did not measure,
            which is how an outage here went unnoticed for hours on 2 August
            2026.
          </p>
        }
      />

      <section className="mt-8 surface-card p-5">
        <Row
          label="REST API"
          ok={apiUp}
          detail={
            apiUp
              ? `Database reachable · responded in ${elapsed} ms`
              : health.ok
                ? `Reachable but degraded: database ${health.data.database}`
                : `No response (${health.error})`
          }
        />
        <Row
          label="Data coverage"
          ok={coverage.ok}
          detail={
            coverage.ok
              ? `${green} of ${rows.length} jurisdictions fully ingested` +
                (impaired.length
                  ? ` · ${impaired.length} impaired: ${impaired
                      .map((r) => r.jurisdiction_code)
                      .join(", ")}`
                  : "")
              : "Coverage matrix unavailable"
          }
        />
      </section>

      {pool || conc ? (
        <section className="mt-6 surface-card p-5">
          <h2 className="text-sm font-semibold text-slate-900">Capacity</h2>
          <dl className="mt-3 grid grid-cols-2 gap-4 text-sm sm:grid-cols-3">
            {pool ? (
              <>
                <div>
                  <dt className="text-xs text-slate-500">Connections in use</dt>
                  <dd className="text-slate-900">
                    {pool.in_use} / {pool.capacity}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Pool saturation</dt>
                  <dd className="text-slate-900">
                    {pool.saturation === null
                      ? "—"
                      : `${Math.round(pool.saturation * 100)}%`}
                  </dd>
                </div>
              </>
            ) : null}
            {conc ? (
              <>
                <div>
                  <dt className="text-xs text-slate-500">Requests in flight</dt>
                  <dd className="text-slate-900">
                    {conc.in_flight} / {conc.max_concurrent}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500">
                    Requests shed (lifetime)
                  </dt>
                  <dd className="text-slate-900">{conc.shed_total}</dd>
                </div>
              </>
            ) : null}
          </dl>
          <p className="mt-3 text-xs text-slate-600">
            Published because the failure that took this service down was a
            capacity failure, not a crash: it kept returning HTTP 200 the whole
            time. Sustained saturation near 100% is the signature.
          </p>
        </section>
      ) : null}

      {usage.ok ? (
        <section className="mt-6 surface-card p-5">
          <h2 className="text-sm font-semibold text-slate-900">
            Usage, last {usage.data.window_days} days
          </h2>
          <p className="mt-2 text-sm text-slate-700">
            <strong>{usage.data.total_tool_calls}</strong> MCP tool calls, plus{" "}
            {usage.data.self_probe_calls} from our own uptime monitor, which are
            excluded from that figure.
          </p>
          <p className="mt-2 text-xs text-slate-600">
            Published unflattering. Counting connections instead of calls, or
            leaving our own monitor in the total, would make this number look
            far better than it is.
          </p>
        </section>
      ) : null}

      <p className="mt-8 text-sm text-slate-600">
        Per-jurisdiction ingestion detail is on the{" "}
        <Link href="/coverage" className="underline">
          coverage page
        </Link>
        . Known limitations are in the{" "}
        <Link href="/changelog" className="underline">
          changelog
        </Link>
        .
      </p>
    </div>
  );
}
