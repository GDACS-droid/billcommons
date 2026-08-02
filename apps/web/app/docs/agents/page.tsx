import type { Metadata } from "next";
import Link from "next/link";
import { API_DOCS_URL, MCP_URL } from "@/lib/config";
import PageHeader from "@/components/PageHeader";

export const metadata: Metadata = {
  title: "Use Bill Commons from your AI agent",
  description:
    "Connect Claude Code, Claude Desktop, Cursor, or any AI agent to Bill Commons in one command — search 209,000+ state bills, track status changes, and build legislative monitors without writing integration code.",
  alternates: { canonical: "/docs/agents" },
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

export default function AgentsDocsPage() {
  return (
    <div className="mx-auto max-w-4xl px-4 py-12 sm:px-6">
      <PageHeader
        eyebrow="Quickstart"
        title="Use Bill Commons from your AI agent"
        description={
          <p>
        You don&apos;t need to write integration code — or be a programmer at
        all. Connect your AI assistant once, then ask it questions in plain
        English: <em>&quot;find every AI bill introduced in Texas this
        session&quot;</em>, <em>&quot;did GA SB 594 pass?&quot;</em>,{" "}
        <em>&quot;which of these 40 bills moved this week?&quot;</em> No API
        key, no signup, free.
          </p>
        }
      />

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-900">Claude Code</h2>
        <p className="mt-2 text-sm text-slate-600">
          One command in your terminal:
        </p>
        <CodeBlock label="shell">{`claude mcp add bill-commons --transport http ${MCP_URL}`}</CodeBlock>
        <p className="mt-2 text-sm text-slate-600">
          Then just ask: <em>&quot;Using bill-commons, list every enacted data
          privacy bill from 2026 and summarize what each one requires.&quot;</em>
        </p>
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-900">
          Claude Desktop / Claude.ai
        </h2>
        <p className="mt-2 text-sm text-slate-600">
          Settings → Connectors → Add custom connector, and paste the server
          URL:
        </p>
        <CodeBlock>{MCP_URL}</CodeBlock>
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-900">
          Cursor / other MCP clients
        </h2>
        <CodeBlock label="mcp.json">{`{
  "mcpServers": {
    "bill-commons": {
      "url": "${MCP_URL}",
      "transport": "http"
    }
  }
}`}</CodeBlock>
        <p className="mt-2 text-sm text-slate-600">
          Any client that speaks MCP over Streamable HTTP works. Full tool
          list and a Python example are on the{" "}
          <Link href="/docs/mcp" className="underline">
            MCP server page
          </Link>
          .
        </p>
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-900">
          Building an automated monitor
        </h2>
        <p className="mt-2 text-sm text-slate-600">
          For standing watchlists — a lobbying shop tracking 100 bills, a
          policy team watching a topic — pair two endpoints. Resolve your
          watchlist once:
        </p>
        <CodeBlock label="shell — resolve bill numbers to IDs">{`curl -X POST "${API_DOCS_URL}/api/v1/bills/lookup" \\
  -H "Content-Type: application/json" \\
  -d '{"keys": [
        {"jurisdiction": "AZ", "identifier": "HB 2192"},
        {"jurisdiction": "GA", "identifier": "SB 594"}
      ]}'`}</CodeBlock>
        <p className="mt-2 text-sm text-slate-600">
          Then poll the change feed — it returns only what moved, with the
          transition itself (&quot;in_committee → enacted&quot;) in each
          event:
        </p>
        <CodeBlock label="shell — what changed since my last check?">{`curl "${API_DOCS_URL}/api/v1/changes?kind=status&ids=<your-bill-ids>"
# store next_cursor from the response, pass it back next time:
curl "${API_DOCS_URL}/api/v1/changes?cursor=<next_cursor>&kind=status&ids=..."`}</CodeBlock>
        <p className="mt-2 text-sm text-slate-600">
          Give those two snippets to your agent and ask it to build the
          monitor for you — this page is written to be readable by agents,
          too. Full reference:{" "}
          <Link href="/docs/api" className="underline">
            API docs
          </Link>
          .
        </p>
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-900">
          Three prompts, and what you should get back
        </h2>
        <p className="mt-2 text-sm text-slate-700">
          Worked examples, including the answers that look unhelpful and are
          actually correct. If your agent returns something confidently
          different from the third one, it is guessing — see the{" "}
          <Link href="/quality" className="underline">
            data-integrity contract
          </Link>
          .
        </p>

        <h3 className="mt-6 text-sm font-semibold text-slate-900">
          1. A straight lookup
        </h3>
        <CodeBlock label="prompt">
          {`Did Hawaii SB 2135 become law? Give me the source URL.`}
        </CodeBlock>
        <p className="mt-2 text-sm text-slate-700">
          <strong>Expect:</strong> enacted, with the signing date and a{" "}
          <code>source_url</code> pointing at the Hawaii legislature. Note the
          session adjourned 2026-05-08 and it was signed 2026-07-07 — a tracker
          that assumes everything dies at adjournment gets this wrong.
        </p>

        <h3 className="mt-6 text-sm font-semibold text-slate-900">
          2. A multi-state scan
        </h3>
        <CodeBlock label="prompt">
          {`Search all states for bills about algorithmic pricing this session.
For each one tell me the state, bill number, status, and whether the
session has already adjourned. Flag any state where coverage is degraded.`}
        </CodeBlock>
        <p className="mt-2 text-sm text-slate-700">
          <strong>Expect:</strong> a table, plus a{" "}
          <code>coverage_warning</code> for any jurisdiction below the search
          threshold. The warning is the point — it is what stops an empty
          result from reading as &quot;no such legislation exists&quot;.
        </p>

        <h3 className="mt-6 text-sm font-semibold text-slate-900">
          3. The one that should refuse
        </h3>
        <CodeBlock label="prompt">{`What happened to Texas HB 1?`}</CodeBlock>
        <p className="mt-2 text-sm text-slate-700">
          <strong>Expect a refusal.</strong> &quot;TX HB 1&quot; matches three
          different sessions, so there is no single answer. A correct response
          lists the candidate sessions and asks which one you mean. An answer
          that confidently reports one status has picked a session for you
          without saying so — that is the single most common failure in
          legislative tooling, and it is why{" "}
          <code>match_type: bill_number_ambiguous</code> exists.
        </p>
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold text-slate-900">
          What this system will not tell you
        </h2>
        <p className="mt-2 text-sm text-slate-700">
          Stated up front, because an agent that discovers a gap mid-answer
          tends to fill it:
        </p>
        <ul className="mt-3 list-disc space-y-1.5 pl-5 text-sm text-slate-700">
          <li>
            <strong>No hearing schedules.</strong> A hearings tool exists but
            there are zero hearing records. An empty result there means
            &quot;not collected&quot;, never &quot;none scheduled&quot;.
          </li>
          <li>
            <strong>No federal legislation.</strong> 50 states and DC only.
          </li>
          <li>
            <strong>No historical sessions.</strong> Current session or biennium
            only — not an archive.
          </li>
          <li>
            <strong>Status is derived, not reported.</strong> Especially{" "}
            <code>died_on_adjournment</code>, which exists precisely because
            nothing was filed. Cite it as our conclusion, not the
            legislature&apos;s.
          </li>
          <li>
            <strong>Roughly 5% of bills have no status at all</strong>, on
            purpose. States disagree on identical wording, so the derivation
            returns nothing rather than guess.
          </li>
        </ul>
      </section>

      <section className="mt-10 rounded-md border border-blue-200 bg-blue-50 p-5">
        <h2 className="text-base font-semibold text-slate-900">
          Not technical? You&apos;re done after step one.
        </h2>
        <p className="mt-2 text-sm text-slate-700">
          The whole point of the agent integration is that the API disappears:
          connect once, then talk to your assistant like you&apos;d talk to a
          research analyst. It knows how to search, look up, compare versions,
          and check coverage on its own.
        </p>
      </section>
    </div>
  );
}
