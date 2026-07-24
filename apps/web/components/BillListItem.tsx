import Link from "next/link";
import type { BillSummary } from "@/lib/types";
import { BillStatusBadge } from "./StatusBadge";

export default function BillListItem({ bill }: { bill: BillSummary }) {
  return (
    <li className="rounded-lg border border-slate-200 p-4 transition hover:border-slate-300 hover:shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <Link
          href={`/bills/${encodeURIComponent(bill.id)}`}
          className="font-medium text-slate-900 hover:underline"
        >
          {bill.jurisdiction_code ? (
            <span className="text-slate-500">{bill.jurisdiction_code}</span>
          ) : null}{" "}
          {bill.identifier} — {bill.title}
        </Link>
        <BillStatusBadge status={bill.status} />
      </div>
      {bill.highlight ? (
        <p
          className="mt-2 text-sm text-slate-600 [&_mark]:bg-amber-200 [&_mark]:font-medium [&_mark]:text-slate-900"
          dangerouslySetInnerHTML={{ __html: bill.highlight }}
        />
      ) : null}
      <p className="mt-2 text-xs text-slate-500">
        {bill.session_identifier ? `${bill.session_identifier} · ` : ""}
        {bill.latest_action_date
          ? `Latest action ${bill.latest_action_date}: ${
              bill.latest_action_description ?? ""
            }`
          : "No recorded actions yet"}
      </p>
    </li>
  );
}
