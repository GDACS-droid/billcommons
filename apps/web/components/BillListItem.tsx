import Link from "next/link";
import type { BillSummary } from "@/lib/types";
import { renderHighlight } from "@/lib/highlight";
import { BillStatusBadge } from "./StatusBadge";

/**
 * `href` lets a caller that knows the jurisdiction and session link straight to
 * the canonical /states/{code}/bills/{session}/{number} URL. Without it the
 * item falls back to /bills/{uuid}, which 301s to the same place -- correct,
 * but a redirect hop that wastes crawl budget on listing pages a search engine
 * follows in bulk.
 */
export default function BillListItem({
  bill,
  href,
}: {
  bill: BillSummary;
  href?: string;
}) {
  return (
    <li className="rounded-md border border-slate-200 bg-white p-4 transition-colors hover:border-slate-300 hover:bg-slate-50">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <Link
          href={href ?? `/bills/${encodeURIComponent(bill.id)}`}
          className="font-medium text-blue-800 hover:text-blue-700 hover:underline"
        >
          {bill.identifier} — {bill.title}
        </Link>
        <BillStatusBadge status={bill.status} />
      </div>
      {bill.highlight ? (
        <p className="mt-2 text-sm leading-6 text-slate-600 [&_mark]:bg-blue-100 [&_mark]:font-medium [&_mark]:text-slate-900">
          {renderHighlight(bill.highlight)}
        </p>
      ) : null}
      <p className="mt-2 text-xs text-slate-500">
        {bill.latest_action_date
          ? `Latest action ${bill.latest_action_date}: ${
              bill.latest_action_text ?? ""
            }`
          : "No recorded actions yet"}
      </p>
    </li>
  );
}
