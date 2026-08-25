"use client";

import { useState } from "react";
import Link from "next/link";
import CheckoutButton from "@/components/CheckoutButton";

// Fixlist item 20: the tier table advertised "$49/mo · $490/yr" and
// "$299/mo · $2,990/yr", but every `CheckoutButton` hardcoded
// `interval="monthly"` -- the annual prices were on the page as a promise
// the UI could not fulfil (`STRIPE_PRICE_BUILDER_ANNUAL`/`_SCALE_ANNUAL`
// were wired in the API and the runbook, just never reachable from here).
// This toggle flips the `interval` prop the API already accepts.
const TIERS: {
  name: string;
  monthlyPrice: string;
  annualPrice: string | null;
  reqDay: string;
  heavyDay: string;
  note?: string;
}[] = [
  { name: "Anonymous", monthlyPrice: "$0", annualPrice: null, reqDay: "2,000/IP, 5,000/24", heavyDay: "per-minute cap only" },
  { name: "Developer", monthlyPrice: "$0 (free key)", annualPrice: null, reqDay: "5,000", heavyDay: "500" },
  { name: "Builder", monthlyPrice: "$49/mo", annualPrice: "$490/yr", reqDay: "50,000", heavyDay: "5,000" },
  {
    name: "Scale",
    monthlyPrice: "$299/mo",
    annualPrice: "$2,990/yr",
    reqDay: "500,000",
    heavyDay: "100,000",
    note: "nightly snapshots (manual delivery until the automated builder ships)",
  },
  { name: "Enterprise", monthlyPrice: "from $1,500/mo, invoiced", annualPrice: null, reqDay: "custom", heavyDay: "custom" },
];

export default function PricingTiersTable() {
  const [interval, setInterval] = useState<"monthly" | "annual">("monthly");

  return (
    <>
      <div className="mt-10 flex items-center gap-3 text-sm">
        <span className="text-slate-600">Billing:</span>
        <div className="inline-flex rounded-md border border-slate-300 p-0.5">
          <button
            type="button"
            onClick={() => setInterval("monthly")}
            className={`rounded px-3 py-1 text-xs font-medium ${
              interval === "monthly" ? "bg-blue-700 text-white" : "text-slate-700"
            }`}
          >
            Monthly
          </button>
          <button
            type="button"
            onClick={() => setInterval("annual")}
            className={`rounded px-3 py-1 text-xs font-medium ${
              interval === "annual" ? "bg-blue-700 text-white" : "text-slate-700"
            }`}
          >
            Annual (10× monthly)
          </button>
        </div>
      </div>

      <section className="mt-4 overflow-x-auto rounded-md border border-slate-200">
        <table className="w-full text-left text-sm">
          <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-2">Tier</th>
              <th className="px-4 py-2">Price</th>
              <th className="px-4 py-2">Requests/day</th>
              <th className="px-4 py-2">Heavy/day</th>
              <th className="px-4 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {TIERS.map((t) => (
              <tr key={t.name}>
                <td className="px-4 py-3 font-medium text-slate-900">{t.name}</td>
                <td className="px-4 py-3 text-slate-600">
                  {interval === "annual" && t.annualPrice ? t.annualPrice : t.monthlyPrice}
                </td>
                <td className="px-4 py-3 text-slate-600">{t.reqDay}</td>
                <td className="px-4 py-3 text-slate-600">{t.heavyDay}</td>
                <td className="px-4 py-3">
                  {t.name === "Builder" ? (
                    <CheckoutButton
                      plan="builder"
                      interval={interval}
                      label="Subscribe"
                      className="rounded-md bg-blue-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-800"
                    />
                  ) : null}
                  {t.name === "Scale" ? (
                    <CheckoutButton
                      plan="scale"
                      interval={interval}
                      label="Subscribe"
                      className="rounded-md bg-blue-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-800"
                    />
                  ) : null}
                  {t.name === "Developer" ? (
                    <Link href="/docs/api-keys" className="text-xs underline">
                      Get a free key
                    </Link>
                  ) : null}
                  {t.name === "Enterprise" ? (
                    <a href="mailto:sales@billcommons.org" className="text-xs underline">
                      Contact sales
                    </a>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {TIERS.filter((t) => t.note).map((t) => (
          <p key={t.name} className="border-t border-slate-100 px-4 py-2 text-xs text-slate-500">
            {t.name}: {t.note}.
          </p>
        ))}
      </section>
    </>
  );
}
