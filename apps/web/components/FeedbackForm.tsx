"use client";

import { useState } from "react";
import { API_BASE } from "@/lib/config";

/**
 * Site feedback form. Posts straight to the public API like AlertSignup does,
 * so the page around it stays fully static. The `website` field is a honeypot
 * the API checks; it stays visually hidden and empty for humans.
 */
export default function FeedbackForm() {
  const [message, setMessage] = useState("");
  const [email, setEmail] = useState("");
  const [page, setPage] = useState("");
  const [state, setState] = useState<"idle" | "busy" | "done" | "error">("idle");
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (state === "busy") return;
    setState("busy");
    try {
      const form = e.target as HTMLFormElement;
      const honeypot = (form.elements.namedItem("website") as HTMLInputElement)?.value;
      const res = await fetch(`${API_BASE}/api/v1/feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          email: email || undefined,
          page: page || undefined,
          website: honeypot || undefined,
        }),
      });
      if (res.ok) {
        setState("done");
      } else {
        const body = await res.json().catch(() => null);
        setError(
          typeof body?.detail === "string"
            ? body.detail
            : "Something went wrong — please try again."
        );
        setState("error");
      }
    } catch {
      setError("Could not reach the server — please try again.");
      setState("error");
    }
  }

  if (state === "done") {
    return (
      <div className="rounded-md border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-900">
        <strong>Thank you.</strong> Your feedback was received
        {email ? " — we’ll reply if a response is needed" : ""}.
      </div>
    );
  }

  return (
    <form onSubmit={submit} className="max-w-xl">
      <label htmlFor="fb-message" className="block text-sm font-medium text-slate-900">
        What should we know?
      </label>
      <textarea
        id="fb-message"
        required
        minLength={3}
        maxLength={5000}
        rows={5}
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        placeholder="A bug, a data error, a state that looks wrong, a feature you need…"
        className="mt-1.5 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 focus:border-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-700/40"
      />

      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <div>
          <label htmlFor="fb-email" className="block text-sm font-medium text-slate-900">
            Email <span className="font-normal text-slate-500">(optional)</span>
          </label>
          <input
            id="fb-email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com"
            className="mt-1.5 w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm focus:border-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-700/40"
          />
        </div>
        <div>
          <label htmlFor="fb-page" className="block text-sm font-medium text-slate-900">
            Page or bill <span className="font-normal text-slate-500">(optional)</span>
          </label>
          <input
            id="fb-page"
            type="text"
            value={page}
            onChange={(e) => setPage(e.target.value)}
            placeholder="e.g. NY S 1234, or a URL"
            className="mt-1.5 w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm focus:border-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-700/40"
          />
        </div>
      </div>

      {/* Honeypot: hidden from humans, tempting to bots. */}
      <div aria-hidden="true" className="absolute -left-[9999px] h-0 w-0 overflow-hidden">
        <label htmlFor="fb-website">Website</label>
        <input id="fb-website" name="website" type="text" tabIndex={-1} autoComplete="off" />
      </div>

      <button
        type="submit"
        disabled={state === "busy"}
        className="mt-5 rounded-md bg-blue-700 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-800 disabled:opacity-50"
      >
        {state === "busy" ? "Sending…" : "Send feedback"}
      </button>

      {state === "error" ? (
        <p className="mt-2 text-xs text-red-700">{error}</p>
      ) : null}
    </form>
  );
}
