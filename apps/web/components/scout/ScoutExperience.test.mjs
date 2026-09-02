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
