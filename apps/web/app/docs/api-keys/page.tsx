import type { Metadata } from "next";
import Link from "next/link";
import PageHeader from "@/components/PageHeader";
import MagicLinkForm from "@/components/MagicLinkForm";

export const metadata: Metadata = {
  title: "API keys",
  description:
    "Get a free Bill Commons API key: how to request one, authenticate, read your quota headers, and rotate or revoke a key.",
  alternates: { canonical: "/docs/api-keys" },
};

function CodeBlock({ children, label }: { children: string; label?: string }) {
  return (
    <div className="code-block mt-4">
      {label ? (
        <div className="border-b border-slate-700 bg-slate-800 px-4 py-1.5 text-xs font-medium text-slate-400">
          {label}
        </div>
      ) : null}
      <pre>
        <code>{children}</code>
      </pre>
    </div>
  );
}

export default function ApiKeysDocsPage() {
  return (
    <div className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Developer access"
        title="API keys"
        description={
          <p>
            Anonymous access to the public API stays free, with a per-IP
            daily cap. A free Developer key raises that ceiling to 5,000
            requests/day and lets us tell your traffic apart from anonymous
            scraping instead of throttling both the same way. See{" "}
            <Link href="/docs/bulk" className="underline">
              bulk access
            </Link>{" "}
            for higher-volume paid tiers and full-corpus snapshots.
          </p>
        }
      />

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-950">Get a free key</h2>
        <p className="mt-2 text-sm text-slate-600">
          Enter your email — we&apos;ll send a sign-in link. No password, no
          account form. First sign-in mints your Developer key automatically.
        </p>
        <MagicLinkForm />
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-950">Authenticate</h2>
        <p className="mt-2 text-sm text-slate-600">
          Send the key as a Bearer token. <code>X-Api-Key</code> is also
          accepted for clients that can&apos;t set{" "}
          <code>Authorization</code>. Never pass a key as a query string —
          query strings land in logs, <code>Referer</code> headers, and
          shared URLs.
        </p>
        <CodeBlock label="curl">{`curl -H "Authorization: Bearer bc_live_..." \\
  https://api.billcommons.org/api/v1/bills?jurisdiction=FL`}</CodeBlock>
        <CodeBlock label="Python">{`import httpx

resp = httpx.get(
    "https://api.billcommons.org/api/v1/bills",
    params={"jurisdiction": "FL"},
    headers={"Authorization": "Bearer bc_live_..."},
)
resp.raise_for_status()`}</CodeBlock>
        <CodeBlock label="MCP client config">{`{
  "mcpServers": {
    "billcommons": {
      "url": "https://mcp.billcommons.org/mcp",
      "headers": { "Authorization": "Bearer bc_live_..." }
    }
  }
}`}</CodeBlock>
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-950">Quota headers</h2>
        <p className="mt-2 text-sm text-slate-600">
          Every keyed response carries both the per-minute burst budget and
          the daily quota — no need to discover your limit by getting
          throttled.
        </p>
        <div className="mt-4 overflow-x-auto rounded-md border border-slate-200">
          <table className="w-full text-left text-sm">
            <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-4 py-2">Header</th>
                <th className="px-4 py-2">Meaning</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {[
                ["X-RateLimit-Limit", "Per-minute burst ceiling for your plan."],
                ["X-RateLimit-Remaining", "Burst requests left in the current minute."],
                ["X-RateLimit-Reset", "Seconds until the burst window resets."],
                ["X-Quota-Limit", "Your plan's total daily request limit."],
                ["X-Quota-Remaining", "Total requests left today."],
                ["X-Quota-Reset", "Unix timestamp of the next UTC midnight."],
                ["X-Quota-Heavy-Limit", "Daily limit for heavy routes (bill list, full bill, versions, compare, search)."],
                ["X-Quota-Heavy-Remaining", "Heavy-route requests left today."],
                ["X-Plan", "Your current plan (developer, builder, scale, enterprise)."],
              ].map(([h, d]) => (
                <tr key={h}>
                  <td className="px-4 py-2 font-mono text-xs">{h}</td>
                  <td className="px-4 py-2 text-slate-600">{d}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-3 text-sm text-slate-600">
          Past your daily quota, requests get <code>429</code> with a{" "}
          <code>Retry-After</code> header (seconds until the next UTC
          midnight) and an <code>error.code</code> of{" "}
          <code>quota_exceeded</code>. An unknown or revoked key gets{" "}
          <code>401 invalid_api_key</code>.
        </p>
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-950">
          Rotation and revocation
        </h2>
        <p className="mt-2 text-sm text-slate-600">
          From <Link href="/account" className="underline">your account page</Link>,
          rotate a key to mint a successor while the old one keeps working for
          24 hours (so a deploy can roll without downtime), or revoke a key
          immediately if it leaks. <strong>We will never show a key again
          after it&apos;s revealed</strong> — if you lose it, rotate.
        </p>
      </section>
    </div>
  );
}
