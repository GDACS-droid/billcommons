"use client";

import { useState } from "react";
import { API_BASE } from "@/lib/config";
import { trackFunnel } from "@/lib/funnel";

type Props = {
  scope: "state" | "full";
  jurisdiction?: string;
  label: string;
  className?: string;
};

/** POST /api/v1/billing/checkout/snapshot -- one-time snapshot purchase,
 * guest-capable, same redirect pattern as CheckoutButton. */
export default function SnapshotCheckoutButton({ scope, jurisdiction, label, className }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function startCheckout() {
    if (busy) return;
    setBusy(true);
    setError("");
    trackFunnel("checkout_intent", { product: "snapshot", scope });
    try {
      const res = await fetch(`${API_BASE}/api/v1/billing/checkout/snapshot`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope, jurisdiction }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        setError(body?.error?.message ?? "Could not start checkout. Please try again.");
        setBusy(false);
        return;
      }
      const body = await res.json();
      if (typeof body?.url !== "string" || !body.url.trim()) {
        throw new Error("Checkout URL missing");
      }
      const redirect = new URL(body.url);
      if (redirect.protocol !== "https:" || redirect.username || redirect.password) {
        throw new Error("Checkout URL invalid");
      }
      trackFunnel("checkout_redirect_created", { product: "snapshot", scope });
      window.location.href = redirect.toString();
    } catch {
      setError("Could not reach the server — please try again.");
      setBusy(false);
    }
  }

  return (
    <div className="inline-block">
      <button
        type="button"
        onClick={startCheckout}
        disabled={busy}
        className={
          className ??
          "inline-block rounded-md bg-blue-700 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-800 disabled:opacity-50"
        }
      >
        {busy ? "Redirecting…" : label}
      </button>
      {error ? <p className="mt-2 text-xs text-red-700">{error}</p> : null}
    </div>
  );
}
