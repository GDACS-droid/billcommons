import { track } from "@vercel/analytics";

/**
 * Funnel analytics intentionally accepts only a small, closed set of
 * dimensions. This keeps emails, URLs, checkout/session identifiers, API
 * keys, and research queries from reaching the analytics provider even if a
 * future caller passes an overly broad object.
 */
export type FunnelEvent =
  | "magic_link_requested"
  | "checkout_intent"
  | "checkout_redirect_created"
  | "api_key_revealed";

type FunnelProperties = Record<string, string>;

const subscriptionPlans = new Set(["builder", "scale"]);
const subscriptionIntervals = new Set(["monthly", "annual"]);
const snapshotScopes = new Set(["state", "full"]);
const keyRevealOperations = new Set(["mint", "reveal", "rotate"]);

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

/** Return only the approved, aggregate dimensions for an event. */
export function sanitizeFunnelProperties(
  event: FunnelEvent,
  input: Record<string, unknown> = {},
): FunnelProperties {
  switch (event) {
    case "magic_link_requested":
      return { surface: "api_docs" };
    case "checkout_intent":
    case "checkout_redirect_created": {
      const product = stringValue(input.product);
      if (product === "subscription") {
        const plan = stringValue(input.plan);
        const interval = stringValue(input.interval);
        return {
          product,
          plan: plan && subscriptionPlans.has(plan) ? plan : "unknown",
          interval: interval && subscriptionIntervals.has(interval) ? interval : "unknown",
        };
      }
      if (product === "snapshot") {
        const scope = stringValue(input.scope);
        return {
          product,
          scope: scope && snapshotScopes.has(scope) ? scope : "unknown",
        };
      }
      return { product: "unknown" };
    }
    case "api_key_revealed": {
      const operation = stringValue(input.operation);
      return {
        operation: operation && keyRevealOperations.has(operation) ? operation : "unknown",
      };
    }
  }
}

/** Analytics is strictly observational: a provider failure must not affect UI flows. */
export function trackFunnel(event: FunnelEvent, input?: Record<string, unknown>): void {
  try {
    track(event, sanitizeFunnelProperties(event, input));
  } catch {
    // Tracking may be blocked by privacy tooling or fail before its client queue initializes.
  }
}
