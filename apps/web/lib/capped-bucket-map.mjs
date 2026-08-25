// A fixed-window bucket map with a hard size cap, evicting the OLDEST
// slice (by window start) rather than clearing everything when at
// capacity. Extracted out of middleware.ts (which used to inline exactly
// this at module scope) into plain ESM for the same reason
// subnet-bucket.mjs was: it needs to run directly under `node --test` with
// zero build step, exercising the EXACT code middleware.ts runs rather
// than a hand-copied stand-in that can silently drift from it.
//
// Mirrors billcommons_api.rate_limit._FixedWindowCounter (sweep + insert-
// time capacity check + oldest-first eviction), one level simpler: no
// per-request "would this be allowed" prediction (peek/allow) is needed
// here, only "is this key's window still live, and how many hits so far".
//
// Verify r6 (round 88e289c), finding #3: every `now` this class receives
// MUST be a MONOTONIC clock reading (e.g. `performance.now()`), never
// wall-clock time (`Date.now()`) -- a backward jump in wall-clock time
// (an NTP correction, a container clock adjustment) would make
// `now - bucket.start` go negative, corrupting every live window's
// remaining-time math (a bucket could appear to have MORE than a full
// window left, or sweep could never reclaim it). This class itself is
// clock-agnostic (it just does arithmetic on whatever numbers it's
// given); the contract lives with the caller.

/**
 * @typedef {{ start: number, count: number }} Bucket
 */

export class CappedBucketMap {
  /**
   * @param {{ windowMs: number, maxKeys: number, evictionFraction?: number }} options
   */
  constructor({ windowMs, maxKeys, evictionFraction = 0.1 }) {
    this.windowMs = windowMs;
    this.maxKeys = maxKeys;
    this.evictionFraction = evictionFraction;
    /** @type {Map<string, Bucket>} */
    this.buckets = new Map();
    this.lastSweep = 0;
  }

  /**
   * Verify r5 (round b93690a), finding #3: evict only the OLDEST ~10% of
   * tracked keys (by window start), never clear() the whole map --
   * clearing resets EVERY in-window caller's count to zero at once, the
   * worst possible moment for a caller who was about to be correctly
   * throttled (or one who just started a window) to instead get a fresh
   * budget. Evicting the oldest slice only drops keys closest to expiring
   * anyway (the least valuable to keep), so a still-active, recently-
   * started key survives.
   */
  evictOldest() {
    const evictCount = Math.max(1, Math.floor(this.buckets.size * this.evictionFraction));
    const oldestFirst = [...this.buckets.entries()].sort((a, b) => a[1].start - b[1].start);
    for (const [key] of oldestFirst.slice(0, evictCount)) {
      this.buckets.delete(key);
    }
  }

  /**
   * Drop buckets whose window has expired. Without this the map grows
   * once per distinct key and is never reclaimed -- an unbounded memory
   * leak, and a cheap way to exhaust a single instance by rotating source
   * addresses. Run at most once per window, so the cost is amortized to
   * near nothing.
   * @param {number} now
   */
  sweep(now) {
    if (now - this.lastSweep < this.windowMs) return;
    this.lastSweep = now;
    // Collect the expired keys FIRST, then delete -- never mutate a Map
    // while iterating it (this loop used to `delete` mid-iteration;
    // functionally safe per spec for deleting the CURRENT key, but this
    // is one of two spots in this class that both walk `this.buckets`, so
    // both use the same collect-then-delete shape rather than leaning on
    // that spec detail).
    const expired = [];
    for (const [key, bucket] of this.buckets) {
      if (now - bucket.start >= this.windowMs) expired.push(key);
    }
    for (const key of expired) this.buckets.delete(key);
    // Sweeping alone does not bound a burst WITHIN one window across many
    // keys -- if it's still oversized after dropping expired entries,
    // evict the oldest slice.
    if (this.buckets.size > this.maxKeys) this.evictOldest();
  }

  /**
   * Enforce `maxKeys` at the point a genuinely NEW key (or one whose
   * window already expired) is about to be inserted. `sweep` alone is not
   * enough: it only runs once per window, so a burst of new keys arriving
   * faster than that can blow past `maxKeys` before the next scheduled
   * sweep ever fires.
   * @param {number} now
   */
  ensureCapacityForNewKey(now) {
    if (this.buckets.size < this.maxKeys) return;
    // Same collect-then-delete shape as sweep() above -- never mutate the
    // Map while iterating it.
    const expired = [];
    for (const [key, bucket] of this.buckets) {
      if (now - bucket.start >= this.windowMs) expired.push(key);
    }
    for (const key of expired) this.buckets.delete(key);
    if (this.buckets.size >= this.maxKeys) this.evictOldest();
  }

  /**
   * Preview what a hit against `key` at time `now` WOULD produce, without
   * mutating the map at all -- no sweep, no insert, no eviction. Pairs
   * with `commit()` below for a check-all-buckets-THEN-increment-only-the-
   * ones-that-passed admission flow (see middleware.ts): every bucket
   * involved in one admission decision must be peeked before ANY of them
   * is incremented, or a request that fails one bucket's check would
   * still have already incremented a sibling bucket it never should have
   * touched.
   * @param {string} key
   * @param {number} now
   * @returns {Bucket}
   */
  peek(key, now) {
    const bucket = this.buckets.get(key);
    if (!bucket || now - bucket.start >= this.windowMs) {
      return { start: now, count: 1 };
    }
    return { start: bucket.start, count: bucket.count + 1 };
  }

  /**
   * Actually record one hit against `key` at time `now`, inserting a
   * fresh bucket (subject to the capacity check above) if the key is new
   * or its window has expired. Returns the bucket AFTER this hit is
   * recorded. Call this only once the caller has decided (via `peek()`
   * on every bucket involved) that the request is admitted.
   * @param {string} key
   * @param {number} now
   * @returns {Bucket}
   */
  commit(key, now) {
    this.sweep(now);
    const bucket = this.buckets.get(key);
    if (!bucket || now - bucket.start >= this.windowMs) {
      this.ensureCapacityForNewKey(now);
      const fresh = { start: now, count: 1 };
      this.buckets.set(key, fresh);
      return fresh;
    }
    bucket.count += 1;
    return bucket;
  }

  /**
   * Convenience wrapper kept for the pre-existing peek-and-commit-in-one-
   * call use (and its unit tests): equivalent to `commit()`. New callers
   * that need to check MULTIPLE buckets before committing to any of them
   * should use `peek()`/`commit()` directly instead (see middleware.ts).
   * @param {string} key
   * @param {number} now
   * @returns {Bucket}
   */
  hit(key, now) {
    return this.commit(key, now);
  }
}
