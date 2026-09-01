import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import test from "node:test";

// The web app has no component-test runner. This uses the already-installed
// TypeScript compiler to exercise the exact contract/presentation helpers that
// drive the Scout page, without introducing a test dependency.
const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = await readFile(new URL("./scout.ts", import.meta.url), "utf8");
const javascript = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const compiled = { exports: {} };
new Function("exports", "module", javascript)(compiled.exports, compiled);

const { isScoutTerminal, normalizeScoutJob, safeHttpsUrl, scoutAnalyticsFacts, scoutStatusSummary } = compiled.exports;

test("normalizes the API's snake-case Scout job into safe display data", () => {
  const job = normalizeScoutJob({
    data: {
      id: "job-1",
      query: "What changed for HB 1?",
      jurisdiction: "FL",
      status: "partial",
      cache_hit: true,
      usage: { external_requests: 2, browser_runtime_ms: 1499 },
      events: [{ id: "e-1", stage: "direct_retrieval", message: "Source retained." }],
      sources: [
        { id: "s-1", title: "Official page", canonical_url: "https://www.leg.state.fl.us/bills" },
        { id: "s-2", title: "Unsafe response", canonical_url: "javascript:alert(1)" },
      ],
      findings: [{ id: "f-1", title: "An update", what_happened: "Recorded", why_it_matters: "Relevant", source_id: "s-1" }],
      browser_sessions: [{ id: "b-1", status: "released", replay_available: true }],
      errors: [{ detail: "One source timed out." }],
    },
  });

  assert.equal(job.status, "partial");
  assert.equal(job.cacheHit, true);
  assert.equal(job.usage.externalRequests, 2);
  assert.equal(job.sources[0].canonicalUrl, "https://www.leg.state.fl.us/bills");
  assert.equal(job.sources[1].canonicalUrl, undefined);
  assert.equal(job.browserSessions[0].replayAvailable, true);
  assert.deepEqual(job.errors, ["One source timed out."]);
  assert.match(scoutStatusSummary(job), /partial results/i);
});

test("only known terminal states stop polling and failed jobs never claim a finding", () => {
  assert.equal(isScoutTerminal("queued"), false);
  assert.equal(isScoutTerminal("running"), false);
  assert.equal(isScoutTerminal("unknown"), false);
  assert.equal(isScoutTerminal("complete"), true);
  assert.equal(isScoutTerminal("partial"), true);
  assert.equal(isScoutTerminal("failed"), true);
  assert.equal(isScoutTerminal("canceled"), true);
  assert.equal(normalizeScoutJob({ id: "legacy-job", status: "completed" }).status, "complete");
  assert.match(
    scoutStatusSummary(normalizeScoutJob({ id: "job-2", status: "failed", findings: [{ title: "ignored" }] })),
    /No unverified result/i
  );
});

test("source controls reject non-HTTPS and credential-bearing URLs", () => {
  assert.equal(safeHttpsUrl("http://example.gov"), undefined);
  assert.equal(safeHttpsUrl("https://user:pass@example.gov/record"), undefined);
  assert.equal(safeHttpsUrl("data:text/html,hello"), undefined);
  assert.equal(safeHttpsUrl("https://example.gov/record"), "https://example.gov/record");
});

test("accepts the compact P0 API aliases without losing source evidence", () => {
  const job = normalizeScoutJob({
    coalesced: true,
    job: {
      id: "job-3",
      status: "completed",
      error_class: "partial_source_failure",
      events: [{ kind: "source_retained", detail: { message: "Official source retained." } }],
      sources: [{ id: "source-3", url: "https://www.leg.state.fl.us/record", official: true, mechanism: "direct", status: 200, mime_type: "text/html", content_hash: "a".repeat(64), prior_source_id: "source-2", change_kind: "material", change_summary: "Normalized text changed (10→12 chars; first difference at 7)." }],
      findings: [{ id: "finding-3", title: "Finding", what_happened: "Recorded", why_it_matters: "Relevant", excerpt: "Primary text", source_url: "https://www.leg.state.fl.us/record" }],
      browser_sessions: [{ id: "session-3", status: "released", pages: 2, actions: 3, replay_available: true }],
    },
  });

  assert.equal(job.status, "complete");
  assert.equal(job.cacheHit, true);
  assert.equal(job.events[0].stage, "source_retained");
  assert.equal(job.events[0].message, "Official source retained.");
  assert.equal(job.sources[0].canonicalUrl, "https://www.leg.state.fl.us/record");
  assert.equal(job.sources[0].officialDomain, "Official source");
  assert.equal(job.sources[0].priorSourceId, "source-2");
  assert.equal(job.sources[0].changeKind, "material");
  assert.match(job.sources[0].changeSummary, /Normalized text changed/);
  assert.equal(job.findings[0].evidenceExcerpt, "Primary text");
  assert.equal(job.findings[0].sourceUrl, "https://www.leg.state.fl.us/record");
  assert.equal(job.usage.browserPages, 2);
  assert.equal(job.usage.browserActions, 3);
});

test("derives aggregate analytics without exposing research or provider data", () => {
  const job = normalizeScoutJob({
    id: "job-private-id",
    query: "sensitive research question",
    jurisdiction: "FL",
    status: "completed",
    usage: { browser_runtime_ms: 2400 },
    events: [{ id: "e1", kind: "structured_candidates" }, { id: "e2", kind: "direct_retrieval" }],
    sources: [{ id: "source-private-id", url: "https://example.gov/private-path", mechanism: "direct", official: true, content_hash: "a".repeat(64) }],
    findings: [{ id: "finding-private-id", title: "private title", what_happened: "private claim", confidence: "high" }],
    browser_sessions: [{ id: "provider-private-id", status: "released", runtime_ms: 2400 }],
  });

  const facts = scoutAnalyticsFacts(job);
  assert.deepEqual(
    facts.map((fact) => fact.event),
    [
      "scout_existing_data_used",
      "scout_direct_retrieval_used",
      "scout_solari_used",
      "scout_source_discovered",
      "scout_document_discovered",
      "scout_finding_generated",
    ],
  );
  const serializedProperties = JSON.stringify(facts.map((fact) => fact.properties));
  for (const forbidden of [
    "job-private-id", "source-private-id", "finding-private-id", "provider-private-id",
    "sensitive research question", "example.gov", "private title", "private claim", "aaaaaaaaaaaa",
  ]) {
    assert.equal(serializedProperties.includes(forbidden), false);
  }
});
