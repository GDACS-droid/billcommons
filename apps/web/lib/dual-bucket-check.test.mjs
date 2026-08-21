// Run with: node --test apps/web/lib/dual-bucket-check.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import { checkDualBuckets } from "./dual-bucket-check.mjs";

const WINDOW_MS = 60_000;

test("neither bucket over its limit -> not exceeded", () => {
  const result = checkDualBuckets({
    ipBucket: { start: 0, count: 100 },
    subnetBucket: { start: 0, count: 200 },
    now: 1_000,
    perIpLimit: 300,
    perSubnetLimit: 600,
    windowMs: WINDOW_MS,
  });
  assert.equal(result.exceeded, false);
});

test("verify r6, finding #3: per-IP bucket alone can trip it, independent of the subnet bucket", () => {
  // The real 2026-08-21 scraper's shape: one IP running hot, but its /24
  // (shared with nobody) never crosses the subnet limit on its own.
  const result = checkDualBuckets({
    ipBucket: { start: 0, count: 301 }, // over 300
    subnetBucket: { start: 0, count: 301 }, // still well under 600
    now: 1_000,
    perIpLimit: 300,
    perSubnetLimit: 600,
    windowMs: WINDOW_MS,
  });
  assert.equal(result.exceeded, true);
});

test("subnet bucket alone can trip it, independent of any one IP", () => {
  const result = checkDualBuckets({
    ipBucket: { start: 0, count: 50 }, // well under 300
    subnetBucket: { start: 0, count: 601 }, // over 600
    now: 1_000,
    perIpLimit: 300,
    perSubnetLimit: 600,
    windowMs: WINDOW_MS,
  });
  assert.equal(result.exceeded, true);
});

test("retryAfter is the LONGER of the two when both are over", () => {
  const result = checkDualBuckets({
    ipBucket: { start: 55_000, count: 301 }, // started recently -> long reset
    subnetBucket: { start: 1_000, count: 601 }, // started long ago -> short reset
    now: 59_000,
    perIpLimit: 300,
    perSubnetLimit: 600,
    windowMs: WINDOW_MS,
  });
  assert.equal(result.exceeded, true);
  // ipBucket: ceil((60000 - (59000-55000))/1000) = ceil(56) = 56
  // subnetBucket: ceil((60000 - (59000-1000))/1000) = ceil(2) = 2
  assert.equal(result.retryAfter, 56);
});

test("retryAfter is never less than 1", () => {
  const result = checkDualBuckets({
    ipBucket: { start: 59_999, count: 301 },
    subnetBucket: { start: 0, count: 100 },
    now: 60_000,
    perIpLimit: 300,
    perSubnetLimit: 600,
    windowMs: WINDOW_MS,
  });
  assert.ok(result.retryAfter >= 1);
});
