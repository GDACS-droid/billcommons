"use client";

import { useState } from "react";
import { API_BASE } from "@/lib/config";

/**
 * Email-alert signup for a topic tracker. Posts straight to the public API
 * from the browser (CORS allows POST for exactly this); no Next server
 * action, so the crawlable page around it stays fully static/cached.
 */
export default function AlertSignup({ topicSlug, topicName }: { topicSlug: string; topicName: string }) {
  const [email, setEmail] = useState("");
  const [state, setState] = useState<"idle" | "busy" | "done" | "error">("idle");
  const [message, setMessage] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (state === "busy") return;
    setState("busy");
    try {
      const res = await fetch(`${API_BASE}/api/v1/alerts/subscribe`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, kind: "topic", target: topicSlug }),
      });
      if (res.ok) {
        setState("done");
      } else {
        const body = await res.json().catch(() => null);
        setMessage(
          typeof body?.detail === "string"
            ? body.detail
            : "Something went wrong — please try again."
        );
        setState("error");
      }
    } catch {
      setMessage("Could not reach the server — please try again.");
      setState("error");
    }
  }

  if (state === "done") {
    return (
      <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-900">
        <strong>You&apos;re on the list.</strong> You&apos;ll get an email
        digest when {topicName.toLowerCase()} bills move — status changes, new
        bills, new text. Every email has a one-click unsubscribe.
      </div>
    );
  }

  return (
    <form
      onSubmit={submit}
      className="rounded-lg border border-amber-200 bg-amber-50 p-4"
    >
      <p className="text-sm font-semibold text-slate-900">
        Get emailed when {topicName.toLowerCase()} bills move
      </p>
      <p className="mt-1 text-xs text-slate-600">
        Status changes, new bills, and new text across all 50 states —
        digested, not one email per event. Free, unsubscribe any time.
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        <input
          type="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="you@example.com"
          aria-label="Email address"
          className="min-w-0 flex-1 rounded border border-slate-300 bg-white px-3 py-1.5 text-sm focus:border-amber-500 focus:outline-none"
        />
        <button
          type="submit"
          disabled={state === "busy"}
          className="rounded bg-slate-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
        >
          {state === "busy" ? "Subscribing…" : "Subscribe"}
        </button>
      </div>
      {state === "error" ? (
        <p className="mt-2 text-xs text-red-700">{message}</p>
      ) : null}
    </form>
  );
}
