"use client";

import { useCallback, useEffect, useState } from "react";
import PageHeader from "@/components/PageHeader";
import { KeyActions, MintKeyButton } from "@/components/AccountKeyActions";
import { API_BASE } from "@/lib/config";

type AccountKey = {
  id: string;
  name: string;
  key_prefix: string;
  environment: string;
  plan: string;
  status: string;
  created_at: string;
  last_used_at: string | null;
  pending_reveal: boolean;
  usage_today: { requests: number; heavy_requests: number };
};

type AccountMe = {
  email: string;
  plan: string;
  keys: AccountKey[];
};

/**
 * Account dashboard: key metadata + today's usage, plus mint/rotate/revoke/
 * reveal controls (item 17 fix -- `/docs/api-keys` documents this incident
 * response; before this fix, no control for it existed anywhere in the web
 * app), and a "Manage billing" button (`POST /billing/portal`, Phase 2). No
 * password, no signup form here -- get a session via the magic-link flow at
 * `/account/login`. Every unsafe action here is a cookie-authed
 * `fetch(..., { credentials: "include" })`; the browser attaches the real
 * `Origin` header itself (B7's CSRF gate on the API), so nothing here has
 * to set it. Per-key mint/reveal state lives in `AccountKeyActions` (each
 * component instance owns its own `revealed` state), which is what keeps
 * a freshly minted key's plaintext from ever colliding with another key's
 * slot (fixlist item 13). A ROTATED key's plaintext is bubbled up to this
 * page's own `rotatedKey` state instead (fixlist item 15) -- the
 * per-key card `KeyActions` renders is captioned with the OLD (now
 * `rotating`) key, so showing the NEW key's secret there mislabeled it.
 */
export default function AccountPage() {
  const [me, setMe] = useState<AccountMe | null>(null);
  const [loggedOut, setLoggedOut] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  // Item 15 fix: a just-rotated key's plaintext is shown HERE, at the page
  // level, correctly attributed to the new key -- not inside the old
  // key's (still-`rotating`) card, which is what `KeyActions` used to do.
  const [rotatedKey, setRotatedKey] = useState<string | null>(null);

  const refresh = useCallback(() => {
    fetch(`${API_BASE}/api/v1/account/me`, { credentials: "include" })
      .then((res) => {
        if (!res.ok) {
          setLoggedOut(true);
          return null;
        }
        return res.json();
      })
      .then((body) => {
        if (body) setMe(body);
      })
      .catch(() => setLoggedOut(true));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function manageBilling() {
    if (busy) return;
    setBusy("portal");
    setError("");
    try {
      const res = await fetch(`${API_BASE}/api/v1/billing/portal`, {
        method: "POST",
        credentials: "include",
      });
      if (!res.ok) {
        setError("No billing history yet — subscribe from /pricing first.");
        return;
      }
      const body = await res.json();
      window.location.href = body.url;
    } catch {
      setError("Could not reach the server — please try again.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="mx-auto max-w-2xl px-4 py-12 sm:px-6">
      <PageHeader eyebrow="Bill Commons account" title="Account" />
      {loggedOut ? (
        <p className="mt-6 text-sm text-slate-600">
          You&apos;re not signed in. Request a sign-in link from{" "}
          <a href="/docs/api-keys" className="underline">
            the API keys page
          </a>
          .
        </p>
      ) : null}
      {me ? (
        <div className="mt-6 space-y-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-slate-600">
              {me.email} — plan: <strong>{me.plan}</strong>
            </p>
            <button
              type="button"
              onClick={manageBilling}
              disabled={busy === "portal"}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              Manage billing
            </button>
          </div>

          {error ? <p className="text-xs text-red-700">{error}</p> : null}

          {rotatedKey ? (
            <div className="rounded-md border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">
              <p className="font-semibold">
                Your rotated key — this is the only time it will be shown:
              </p>
              <code className="mt-2 block break-all rounded bg-white px-3 py-2 font-mono text-xs text-slate-900">
                {rotatedKey}
              </code>
            </div>
          ) : null}

          <div className="space-y-3">
            {me.keys.map((k) => (
              <div key={k.id} className="rounded-md border border-slate-200 p-4 text-sm">
                <p className="font-mono">{k.key_prefix}…</p>
                <p className="mt-1 text-slate-500">
                  {k.environment} · {k.plan} · {k.status}
                </p>
                <p className="mt-1 text-slate-500">
                  Today: {k.usage_today.requests} requests ({k.usage_today.heavy_requests} heavy)
                </p>
                <KeyActions
                  keyId={k.id}
                  status={k.status}
                  pendingReveal={k.pending_reveal}
                  onChanged={refresh}
                  onRotated={setRotatedKey}
                />
              </div>
            ))}
          </div>
          <MintKeyButton onMinted={refresh} />
        </div>
      ) : null}
    </div>
  );
}
