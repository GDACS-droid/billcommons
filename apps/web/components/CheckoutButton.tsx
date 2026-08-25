"use client";

import { useState } from "react";
import { API_BASE } from "@/lib/config";

type Props = {
  plan: "builder" | "scale";
  interval: "monthly" | "annual";
  label: string;
  className?: string;
};

/**
 * POST /api/v1/billing/checkout with `credentials: "include"` so an
 * already-logged-in customer's session cookie rides along (billing.py
 * reads it to prefill the Stripe customer, C2) -- checkout itself is
 * guest-capable, so this works for a signed-out visitor too. On success,
 * redirects the browser to the returned Stripe Checkout URL.
 */
export default function CheckoutButton({ plan, interval, label, className }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function startCheckout() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const res = await fetch(`${API_BASE}/api/v1/billing/checkout`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan, interval }),
      });
      if (res.status === 409) {
        window.location.href = "/account";
        return;
      }
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        setError(body?.error?.message ?? "Could not start checkout. Please try again.");
        setBusy(false);
        return;
      }
      const body = await res.json();
      window.location.href = body.url;
    } catch {
      setError("Could not reach the server — please try again.");
      setBusy(false);
    }
  }

  return (
    <div>
      <button
        type="button"
        onClick={startCheckout}
        disabled={busy}
        className={
          className ??
          "rounded-md bg-blue-700 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-800 disabled:opacity-50"
        }
      >
        {busy ? "Redirecting…" : label}
      </button>
      {error ? <p className="mt-2 text-xs text-red-700">{error}</p> : null}
    </div>
  );
}
