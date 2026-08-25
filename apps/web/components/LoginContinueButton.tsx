"use client";

import { useState } from "react";
import { API_BASE } from "@/lib/config";

/**
 * Consumes a magic-link token via `POST /api/v1/account/session`
 * (credentials included so the session cookie the API sets is actually
 * stored). `204` = nothing new, redirect straight to `/account`. `200
 * {"key": "..."}` = this login just auto-minted the account's first
 * Developer key (D5) -- shown once, here, before continuing.
 */
export default function LoginContinueButton({ token }: { token: string }) {
  const [state, setState] = useState<"idle" | "busy" | "key" | "error">("idle");
  const [key, setKey] = useState("");
  const [error, setError] = useState("");

  async function continueLogin() {
    if (state === "busy") return;
    setState("busy");
    try {
      const res = await fetch(`${API_BASE}/api/v1/account/session`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      if (res.status === 204) {
        window.location.href = "/account";
        return;
      }
      if (res.ok) {
        const body = await res.json();
        if (body?.key) {
          setKey(body.key);
          setState("key");
          return;
        }
        window.location.href = "/account";
        return;
      }
      setError("This sign-in link is invalid or has expired.");
      setState("error");
    } catch {
      setError("Could not reach the server — please try again.");
      setState("error");
    }
  }

  if (state === "key") {
    return (
      <div className="mt-8 rounded-md border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">
        <p className="font-semibold">
          Your API key — this is the only time it will be shown:
        </p>
        <code className="mt-2 block break-all rounded bg-white px-3 py-2 font-mono text-xs text-slate-900">
          {key}
        </code>
        <button
          type="button"
          onClick={() => {
            window.location.href = "/account";
          }}
          className="mt-4 rounded-md bg-blue-700 px-4 py-2 text-sm font-medium text-white hover:bg-blue-800"
        >
          I&apos;ve saved it — continue
        </button>
      </div>
    );
  }

  return (
    <div className="mt-8">
      <button
        type="button"
        onClick={continueLogin}
        disabled={state === "busy"}
        className="rounded-md bg-blue-700 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-800 disabled:opacity-50"
      >
        {state === "busy" ? "Signing in…" : "Continue"}
      </button>
      {state === "error" ? <p className="mt-2 text-xs text-red-700">{error}</p> : null}
    </div>
  );
}
