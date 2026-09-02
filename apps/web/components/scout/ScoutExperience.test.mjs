import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("./ScoutExperience.tsx", import.meta.url), "utf8");

test("Scout leaves unsupported significance absent, marks bounded excerpts, and suppresses empty event detail", () => {
  assert.match(source, /const EXCERPT_CHARACTER_LIMIT = 500/);
  assert.match(source, /excerptMayBeTruncated \? <span aria-label="Excerpt may continue">…<\/span> : null/);
  assert.match(source, /Excerpt is limited to 500 characters\. Open the official document/);
  assert.match(source, /finding\.whyItMatters \? \(/);
  assert.match(source, /event\.message \? <p className="mt-0\.5 break-words text-slate-700">/);
});

test("every visible evidence-source control records a privacy-safe open event", () => {
  assert.match(source, /track\("scout_evidence_opened", \{ control: "finding" \}\)/);
  assert.match(source, /track\("scout_evidence_opened", \{ control: "source_metadata" \}\)/);
  assert.match(source, /track\("scout_evidence_opened", \{ control: "prior_source_metadata" \}\)/);
});

test("Scout open waits briefly for the root analytics queue instead of dropping the event", () => {
  assert.match(source, /typeof analyticsWindow\.va === "function"/);
  assert.match(source, /attempts < 20/);
  assert.match(source, /track\("scout_opened", \{ availability: enabled \? "enabled" : "disabled" \}\)/);
  assert.match(source, /window\.clearTimeout\(timer\)/);
});

test("Scout intro does not show a signed-out prompt over an authenticated result", () => {
  assert.match(source, /Research jobs are owner-scoped\. <Link href="\/account\/login"[^>]*>Account access<\/Link>/);
  assert.doesNotMatch(source, /Sign in<\/Link> to create and view research jobs/);
});
