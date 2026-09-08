import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("./SavedMonitors.tsx", import.meta.url), "utf8");

test("saved monitor presentation names cadence, paused state, and bounded comparisons", () => {
  assert.match(source, /Keep up to three evidence-backed queries on a cadence/);
  assert.match(source, /monitor\.active \? `Next due/);
  assert.match(source, /"Paused"/);
  assert.match(source, /new · \{changedSources\} changed · \{unchanged\} unchanged observed sources/);
  assert.match(source, /Source absence is not evaluated/);
  assert.match(source, /California monitor runs compare retained weekday archive evidence/);
  assert.match(source, /Load earlier runs/);
});

test("saved monitor controls do not promise email and gate saving on eligible evidence", () => {
  assert.doesNotMatch(source, /email notification|webhook delivery/i);
  assert.match(source, /isScoutMonitorEligible/);
  assert.match(source, /Operator and canary research cannot be saved/);
  assert.match(source, /Open evidence/);
});


test("saved monitor UI distinguishes pending, unavailable, missing, and zero-count comparisons", () => {
  assert.match(source, /Number\.isInteger\(value\) && value >= 0/);
  assert.match(source, /Comparison is pending\. Source-change counts are not available yet\./);
  assert.match(source, /Comparison is unavailable for this run\./);
  assert.match(source, /Comparison counts were not returned for this run\./);
  assert.match(source, /listStatus === "ready" && !monitors\.length/);
  assert.match(source, /monitorVersion\.current !== version/);
});

test("acknowledged monitor mutations merge locally before one authoritative post-settlement refresh", () => {
  assert.match(source, /const pendingMutations = useRef\(0\)/);
  assert.match(source, /function beginMutation\(\)/);
  assert.match(source, /function settleMutation\(\)/);
  assert.match(source, /if \(!pendingMutations\.current\) void refresh\(\)/);
  assert.match(source, /acceptMonitor\(result\.monitor\)/);
  assert.match(source, /onMutationStart=\{beginMutation\}/);
  assert.match(source, /onMutationSettled=\{settleMutation\}/);
});
