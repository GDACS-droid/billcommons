// Pure helper combining a per-IP bucket hit and a per-subnet bucket hit
// into one admission decision -- extracted out of middleware.ts (see
// r6, round 88e289c, finding #3: a per-IP bucket now runs ALONGSIDE the
// per-subnet one, a request must pass BOTH) so the combination logic
// itself is unit-testable directly with `node --test`, same reasoning as
// subnet-bucket.mjs and capped-bucket-map.mjs.

/**
 * @typedef {{ start: number, count: number }} Bucket
 */

/**
 * @param {{
 *   ipBucket: Bucket,
 *   subnetBucket: Bucket,
 *   now: number,
 *   perIpLimit: number,
 *   perSubnetLimit: number,
 *   windowMs: number,
 * }} args
 * @returns {{ exceeded: boolean, retryAfter: number }}
 */
export function checkDualBuckets({
  ipBucket,
  subnetBucket,
  now,
  perIpLimit,
  perSubnetLimit,
  windowMs,
}) {
  const exceeded = [];
  if (ipBucket.count > perIpLimit) exceeded.push(ipBucket);
  if (subnetBucket.count > perSubnetLimit) exceeded.push(subnetBucket);

  if (exceeded.length === 0) {
    return { exceeded: false, retryAfter: 0 };
  }

  const retryAfter = Math.max(
    1,
    ...exceeded.map((b) => Math.ceil((windowMs - (now - b.start)) / 1000))
  );
  return { exceeded: true, retryAfter };
}
