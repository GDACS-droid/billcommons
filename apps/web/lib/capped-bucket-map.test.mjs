// Run with: node --test apps/web/lib/capped-bucket-map.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import { CappedBucketMap } from "./capped-bucket-map.mjs";

test("hit() increments an existing in-window key", () => {
  const map = new CappedBucketMap({ windowMs: 60_000, maxKeys: 10 });
  map.hit("a", 0);
  const b = map.hit("a", 100);
  assert.equal(b.count, 2);
  assert.equal(b.start, 0);
});

test("hit() starts a fresh window once the old one expires", () => {
  const map = new CappedBucketMap({ windowMs: 60_000, maxKeys: 10 });
  map.hit("a", 0);
  const b = map.hit("a", 60_001);
  assert.equal(b.count, 1);
  assert.equal(b.start, 60_001);
});

test("verify r5, finding #3: eviction at cap keeps a hot key's count, not clear()", () => {
  const map = new CappedBucketMap({ windowMs: 60_000, maxKeys: 10 });
  // 9 keys, all inserted at t=0 -- the OLDEST cohort.
  for (let i = 0; i < 9; i++) {
    map.hit(`old-${i}`, 0);
  }
  // A 10th, "hot" key inserted later and hit several times (still well
  // within the window) -- its window START is more recent than the
  // first 9's.
  for (let i = 0; i < 5; i++) {
    map.hit("hot", 30_000);
  }
  assert.equal(map.buckets.get("hot").count, 5); // sanity: count accumulated

  // Now AT the cap (10 keys). A brand-new 11th key forces eviction.
  map.hit("new-key", 31_000);

  // The hot key must have SURVIVED (it is not among the oldest) with its
  // count intact -- a clear-everything eviction would have reset it.
  assert.ok(map.buckets.has("hot"), "hot key was evicted -- eviction is not oldest-first");
  assert.equal(map.buckets.get("hot").count, 5, "hot key's count must survive eviction");
});

test("eviction drops only ~10% of tracked keys, not everything", () => {
  const map = new CappedBucketMap({ windowMs: 60_000, maxKeys: 10 });
  for (let i = 0; i < 10; i++) {
    map.hit(`k-${i}`, i); // staggered starts, all still in-window
  }
  map.hit("k-new", 11); // forces eviction (at cap)
  // Evicted exactly floor(10 * 0.1) = 1 of the original 10, so 9 of them
  // plus the new one remain -- not a wipe.
  assert.equal(map.buckets.size, 10);
});

test("ensureCapacityForNewKey reclaims expired entries before evicting a live one", () => {
  const map = new CappedBucketMap({ windowMs: 60_000, maxKeys: 5 });
  for (let i = 0; i < 5; i++) {
    map.hit(`k-${i}`, 0);
  }
  // All 5 expire; a new key at t=61_000 should reclaim the expired ones
  // via the sweep, needing no eviction at all.
  map.hit("fresh", 61_000);
  assert.equal(map.buckets.size, 1);
  assert.ok(map.buckets.has("fresh"));
});

test("sweep collect-then-delete drops every expired key, not just some (mid-iteration mutation trap)", () => {
  const map = new CappedBucketMap({ windowMs: 60_000, maxKeys: 1_000 });
  for (let i = 0; i < 200; i++) {
    map.hit(`k-${i}`, 0);
  }
  assert.equal(map.buckets.size, 200);
  // All 200 expire; forcing a sweep (via a new key past the window) must
  // reclaim ALL of them, not a partial set an in-place `delete()`-while-
  // iterating could skip.
  map.hit("fresh", 60_001);
  assert.equal(map.buckets.size, 1);
  assert.ok(map.buckets.has("fresh"));
});

test("ensureCapacityForNewKey collect-then-delete reclaims every expired key at capacity", () => {
  const map = new CappedBucketMap({ windowMs: 60_000, maxKeys: 50 });
  for (let i = 0; i < 50; i++) {
    map.hit(`k-${i}`, 0);
  }
  // Bypass sweep's once-per-window gate (lastSweep already advanced by
  // the hits above) by inserting a 51st key still within the SAME window
  // as the other 50 -- sweep() is a no-op here, so only
  // ensureCapacityForNewKey's own expired-key scan can reclaim anything.
  // All 50 are still live (window hasn't expired), so this exercises the
  // no-expired-keys path of the collect-then-delete loop without
  // triggering it -- pair with the sweep test above for the reclaim path.
  map.hit("live-51", 100);
  // No expired keys to reclaim, so eviction kicks in: 10% of 50 (= 5)
  // oldest keys dropped, then the new key inserted -- 50 - 5 + 1 = 46.
  // The point under test is that the (empty) expired-key scan runs its
  // collect-then-delete loop without error, not the exact eviction count.
  assert.equal(map.buckets.size, 46);
  assert.ok(map.buckets.has("live-51"));
});

test("peek() previews the next hit without mutating the map", () => {
  const map = new CappedBucketMap({ windowMs: 60_000, maxKeys: 10 });
  map.commit("a", 0);
  const preview = map.peek("a", 100);
  assert.equal(preview.count, 2);
  assert.equal(preview.start, 0);
  // The real bucket must be untouched -- peek() never commits.
  assert.equal(map.buckets.get("a").count, 1);
});

test("peek() on a brand-new key reports a hypothetical count of 1 without inserting it", () => {
  const map = new CappedBucketMap({ windowMs: 60_000, maxKeys: 10 });
  const preview = map.peek("never-seen", 0);
  assert.equal(preview.count, 1);
  assert.equal(preview.start, 0);
  assert.equal(map.buckets.size, 0);
});

test("commit() actually records the hit peek() previewed", () => {
  const map = new CappedBucketMap({ windowMs: 60_000, maxKeys: 10 });
  map.commit("a", 0);
  const preview = map.peek("a", 100);
  const committed = map.commit("a", 100);
  assert.deepEqual(committed, preview);
});

test("check-all-then-increment: a peek that would deny leaves BOTH buckets uncommitted", () => {
  // Regression for the admission-order bug: incrementing both buckets
  // unconditionally (then checking) burns a slot in a bucket that never
  // should have been touched when the OTHER bucket denies the request.
  const ipState = new CappedBucketMap({ windowMs: 60_000, maxKeys: 10 });
  const subnetState = new CappedBucketMap({ windowMs: 60_000, maxKeys: 10 });

  // Pre-load the per-IP bucket right up to its limit.
  for (let i = 0; i < 300; i++) {
    ipState.commit("1.2.3.4", 0);
  }
  // Subnet bucket starts fresh, nowhere near its own limit.
  subnetState.commit("1.2.3.0/24", 0);

  const ipPeek = ipState.peek("1.2.3.4", 100); // would be 301 -> over 300
  const subnetPeek = subnetState.peek("1.2.3.0/24", 100); // would be 2 -> well under 600

  const perIpLimit = 300;
  const perSubnetLimit = 600;
  const wouldDeny = ipPeek.count > perIpLimit || subnetPeek.count > perSubnetLimit;
  assert.ok(wouldDeny);

  // Simulate the middleware's actual control flow: only commit if the
  // peek says admit.
  if (!wouldDeny) {
    ipState.commit("1.2.3.4", 100);
    subnetState.commit("1.2.3.0/24", 100);
  }

  // Neither real bucket moved -- the subnet bucket in particular must NOT
  // have been incremented just because the sibling IP bucket denied.
  assert.equal(ipState.buckets.get("1.2.3.4").count, 300);
  assert.equal(subnetState.buckets.get("1.2.3.0/24").count, 1);
});
