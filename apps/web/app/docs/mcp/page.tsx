import type { Metadata } from "next";
import Link from "next/link";
import { MCP_URL } from "@/lib/config";
import PageHeader from "@/components/PageHeader";

export const metadata: Metadata = {
  title: "MCP server setup",
  description:
    "Connect Claude or any MCP-compatible client to the Bill Commons Model Context Protocol server for legislative search tools.",
  alternates: { canonical: "/docs/mcp" },
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

const TOOLS: { name: string; description: string }[] = [
  { name: "search_legislation", description: "Search bills across jurisdictions by keyword, number, sponsor, subject, or status." },
  { name: "get_bill_record", description: "Fetch a full bill record: sponsors, actions, versions, votes, official sources." },
  { name: "compare_bill_versions", description: "Deterministic diff between two versions of a bill's text." },
  { name: "find_similar_bills", description: "Find related bills by title similarity and shared text (labeled as derived, not official)." },
  { name: "get_vote_details", description: "Get a recorded vote, including member-level votes where available." },
  { name: "get_upcoming_hearings", description: "NOT COLLECTED — always returns an empty list with data_status: \"not_collected\". No hearing or calendar data is ingested for any jurisdiction, so an empty result means we lack the data, never that no hearings are scheduled." },
  { name: "trace_legislative_history", description: "Trace a bill's full action timeline from introduction to current status." },
  { name: "build_legislative_evidence_packet", description: "Assemble a citation-backed packet of bill facts for research use." },
  { name: "get_jurisdiction_coverage", description: "Check ingestion/coverage status for a jurisdiction before trusting results." },
  { name: "get_active_sessions", description: "List currently active legislative sessions across all jurisdictions." },
];

export default function McpDocsPage() {
  return (
    <div className="mx-auto max-w-4xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Agent access"
        title="MCP server setup"
        description={
          <p>
        Bill Commons runs a Model Context Protocol (MCP) server over
        Streamable HTTP, so Claude and other MCP-compatible clients can
        search and cite legislation directly. Every tool returns structured
        JSON with canonical IDs, official source URLs, and freshness
        timestamps — and warns you explicitly when a jurisdiction&rsquo;s
        coverage is thin rather than guessing.
          </p>
        }
      />

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-900">Endpoint</h2>
        <CodeBlock>{MCP_URL}</CodeBlock>
        <p className="mt-2 text-sm text-slate-600">
          Streamable HTTP, stateless, no authentication required for public
          read access.
        </p>
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-900">
          Claude Desktop / Claude Code
        </h2>
        <p className="mt-2 text-sm text-slate-600">
          Add Bill Commons as a remote MCP server in your client config:
        </p>
        <CodeBlock label="claude_desktop_config.json / mcp settings">{`{
  "mcpServers": {
    "bill-commons": {
      "url": "${MCP_URL}",
      "transport": "http"
    }
  }
}`}</CodeBlock>
        <p className="mt-2 text-sm text-slate-600">
          Or, from the Claude Code CLI:
        </p>
        <CodeBlock label="shell">{`claude mcp add bill-commons --transport http ${MCP_URL}`}</CodeBlock>
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-900">
          Other MCP clients
        </h2>
        <p className="mt-2 text-sm text-slate-600">
          Any client supporting the Streamable HTTP transport can connect
          directly to the endpoint above — no SDK required beyond a
          standard MCP client library.
        </p>
        <CodeBlock label="Python (mcp SDK)">{`from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

async with streamablehttp_client("${MCP_URL}") as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool(
            "search_legislation", {"query": "paid family leave"}
        )
        print(result)`}</CodeBlock>
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-900">Tools</h2>
        <div className="surface-card mt-4 overflow-x-auto">
          <table className="data-table min-w-[640px]">
            <thead>
              <tr>
                <th>Tool</th>
                <th>Description</th>
              </tr>
            </thead>
            <tbody>
              {TOOLS.map((t) => (
                <tr key={t.name}>
                  <td className="whitespace-nowrap font-mono text-xs text-slate-800">
                    {t.name}
                  </td>
                  <td className="text-slate-600">
                    {t.description}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <p className="mt-8 text-xs text-slate-500">
        MCP tool results reflect the same underlying data as the{" "}
        <Link href="/docs/api" className="underline">
          REST API
        </Link>
        , including attribution and known-limitations metadata. See{" "}
        <Link href="/methodology" className="underline">
          methodology
        </Link>{" "}
        for sourcing details.
      </p>
    </div>
  );
}
