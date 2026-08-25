"use client";

import { useState } from "react";
import { API_BASE } from "@/lib/config";

/**
 * Item 17 fix: `/docs/api-keys` tells customers "rotate a key... or revoke
 * a key immediately if it leaks" from the account page -- but the account
 * page had no mint/rotate/revoke/reveal control anywhere; it only rendered
 * a read-only key list. All four endpoints already existed (cookie +
 * Origin authed, B7) with no UI in front of them, so the documented
 * incident response for a leaked key did not exist. This component wires
 * up the three per-key actions; `MintKeyButton` below (used by the parent
 * page) covers the fourth.
 */
export function KeyActions({
  keyId,
  status,
  pendingReveal,
  onChanged,
  onRotated,
}: {
  keyId: string;
  status: string;
  pendingReveal: boolean;
  onChanged: () => void;
  /**
   * Item 15 fix (cosmetic residual of round-1 item 13): `rotate()` mints a
   * SUCCESSOR key, but this component instance is captioned with the OLD
   * key's `key_prefix` and renders `status: rotating` -- rendering the new
   * secret inline here (as the pre-fix version did) put it inside a card
   * labelled for the wrong key. When the parent supplies this callback,
   * the new plaintext is handed UP to a page-level "just rotated" slot
   * instead, so it can be shown correctly attributed to the NEW key (the
   * one that will appear as a fresh row in the list once `onChanged`
   * triggers a refetch). Optional so this component still degrades to its
   * old (mislabeled but non-data-losing) local reveal if a future caller
   * doesn't wire it up.
   */
  onRotated?: (key: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [revealed, setRevealed] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function post(path: string): Promise<{ key?: string } | null> {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/v1/account/keys/${keyId}${path}`, {
        method: "POST",
        credentials: "include",
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        setError(body?.error?.message ?? "That didn't work -- please try again.");
        return null;
      }
      if (res.status === 204) return {};
      return res.json();
    } catch {
      setError("Could not reach the server -- please try again.");
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function rotate() {
    if (
      !window.confirm(
        "Rotate this key? The old key keeps working for 24 hours while you roll out the new one."
      )
    )
      return;
    const body = await post("/rotate");
    if (body?.key) {
      if (onRotated) {
        onRotated(body.key);
      } else {
        setRevealed(body.key);
      }
      onChanged();
    } else if (body) {
      onChanged();
    }
  }

  async function revoke() {
    if (!window.confirm("Revoke this key immediately? This cannot be undone.")) return;
    const body = await post("/revoke");
    if (body) onChanged();
  }

  async function reveal() {
    const body = await post("/reveal");
    if (body?.key) setRevealed(body.key);
  }

  if (revealed) {
    return (
      <div className="mt-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-xs text-amber-900">
        <p className="font-semibold">This is the only time this key will be shown:</p>
        <code className="mt-1 block break-all rounded bg-white px-2 py-1 font-mono">{revealed}</code>
      </div>
    );
  }

  return (
    <div className="mt-2 flex flex-wrap items-center gap-3">
      {status === "active" ? (
        <>
          <button
            type="button"
            onClick={rotate}
            disabled={busy}
            className="text-xs font-medium text-blue-700 underline hover:text-blue-900 disabled:opacity-50"
          >
            Rotate
          </button>
          <button
            type="button"
            onClick={revoke}
            disabled={busy}
            className="text-xs font-medium text-red-700 underline hover:text-red-900 disabled:opacity-50"
          >
            Revoke
          </button>
        </>
      ) : null}
      {pendingReveal ? (
        <button
          type="button"
          onClick={reveal}
          disabled={busy}
          className="text-xs font-medium text-blue-700 underline hover:text-blue-900 disabled:opacity-50"
        >
          Reveal
        </button>
      ) : null}
      {error ? <p className="text-xs text-red-700">{error}</p> : null}
    </div>
  );
}

export function MintKeyButton({ onMinted }: { onMinted: () => void }) {
  const [busy, setBusy] = useState(false);
  const [revealed, setRevealed] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function mint() {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/v1/account/keys`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "default", environment: "live" }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        setError(body?.error?.message ?? "Could not create a key right now.");
        return;
      }
      const body = await res.json();
      setRevealed(body.key);
      onMinted();
    } catch {
      setError("Could not reach the server -- please try again.");
    } finally {
      setBusy(false);
    }
  }

  if (revealed) {
    return (
      <div className="rounded-md border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">
        <p className="font-semibold">
          Your new API key — this is the only time it will be shown:
        </p>
        <code className="mt-2 block break-all rounded bg-white px-3 py-2 font-mono text-xs text-slate-900">
          {revealed}
        </code>
      </div>
    );
  }

  return (
    <div>
      <button
        type="button"
        onClick={mint}
        disabled={busy}
        className="rounded-md bg-blue-700 px-4 py-2 text-sm font-medium text-white hover:bg-blue-800 disabled:opacity-50"
      >
        {busy ? "Generating…" : "Generate a new key"}
      </button>
      {error ? <p className="mt-2 text-xs text-red-700">{error}</p> : null}
    </div>
  );
}
