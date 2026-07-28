const COVERAGE_STYLES: Record<string, string> = {
  GREEN: "bg-emerald-100 text-emerald-800",
  DEGRADED: "bg-amber-100 text-amber-800",
  BLOCKED: "bg-red-100 text-red-800",
  VALIDATING: "bg-sky-100 text-sky-800",
  FULL_TEXT_SEARCHABLE: "bg-sky-100 text-sky-800",
  METADATA_SEARCHABLE: "bg-slate-200 text-slate-700",
  BOOTSTRAPPED: "bg-slate-200 text-slate-700",
  SOURCE_IDENTIFIED: "bg-slate-100 text-slate-600",
  NOT_STARTED: "bg-slate-100 text-slate-500",
};

export function CoverageBadge({ status }: { status: string }) {
  const style = COVERAGE_STYLES[status] ?? "bg-slate-100 text-slate-600";
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${style}`}
    >
      {status.replaceAll("_", " ")}
    </span>
  );
}

export function BillStatusBadge({ status }: { status?: string | null }) {
  if (!status) {
    return (
      <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-medium text-slate-500">
        Status unknown
      </span>
    );
  }
  const lower = status.toLowerCase();
  let style = "bg-slate-100 text-slate-700";
  if (lower.includes("enact") || lower.includes("signed") || lower.includes("pass")) {
    style = "bg-emerald-100 text-emerald-800";
  } else if (
    lower.includes("veto") ||
    lower.includes("fail") ||
    lower.includes("dead") ||
    lower.includes("died")
  ) {
    style = "bg-red-100 text-red-800";
  } else if (lower.includes("withdraw")) {
    style = "bg-slate-200 text-slate-600";
  } else if (lower.includes("committee") || lower.includes("introduc") || lower.includes("pending")) {
    style = "bg-amber-100 text-amber-900";
  }
  // The API's controlled vocabulary is snake_case ("in_committee"); render it
  // as prose so the page reads like English rather than like a database column.
  const label = status.replaceAll("_", " ");
  return (
    <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${style}`}>
      {label}
    </span>
  );
}
