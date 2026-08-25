"use client";

import { useState } from "react";
import { API_BASE } from "@/lib/config";

/** POST /api/v1/account/magic-link -- always 202, never reveals whether an
 * account exists (see routers/account.py). */
export default function MagicLinkForm() {
  const [email, setEmail] = useState("");
  const [state, setState] = useState<"idle" | "busy" | "done">("idle");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (state === "busy") return;
    setState("busy");
    try {
      await fetch(`${API_BASE}/api/v1/account/magic-link`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
    } finally {
      setState("done");
    }
  }

  if (state === "done") {
    return (
      <div className="mt-4 rounded-md border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-900">
        Check your inbox for a sign-in link (expires in 15 minutes).
      </div>
    );
  }

  return (
    <form onSubmit={submit} className="mt-4 flex max-w-md gap-2">
      <input
        type="email"
        required
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        placeholder="you@example.com"
        className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm focus:border-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-700/40"
      />
      <button
        type="submit"
        disabled={state === "busy"}
        className="shrink-0 rounded-md bg-blue-700 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-800 disabled:opacity-50"
      >
        {state === "busy" ? "Sending…" : "Send link"}
      </button>
    </form>
  );
}
