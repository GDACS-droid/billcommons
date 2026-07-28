const COVERAGE_STYLES: Record<string, string> = {
  GREEN: "bg-emerald-100 text-emerald-800",
  DEGRADED: "bg-amber-100 text-amber-800",
  BLOCKED: "bg-red-100 text-red-800",
  VALIDATING: "bg-sky-100 text-sky-800",
  FULL_TEXT_SEARCHABLE: "bg-sky-100 text-sky-800",
  METADATA_SEARCHABLE: "bg-slate-200 text-slate-700",
  BOOTSTRAPPED: "bg-slate-200 text-slate-700",
  SOURCE_IDENTIFIED: "bg-slate-100 text-slate-600",
  NOT_STARTED: "bg-slate-100 text-slate-600",
};

const COVERAGE_DOTS: Record<string, string> = {
  GREEN: "bg-emerald-600",
  DEGRADED: "bg-amber-600",
  BLOCKED: "bg-red-600",
  VALIDATING: "bg-sky-600",
  FULL_TEXT_SEARCHABLE: "bg-sky-600",
  METADATA_SEARCHABLE: "bg-slate-500",
  BOOTSTRAPPED: "bg-slate-500",
  SOURCE_IDENTIFIED: "bg-slate-400",
  NOT_STARTED: "bg-slate-400",
};

export function CoverageBadge({ status }: { status: string }) {
  const style = COVERAGE_STYLES[status] ?? "bg-slate-100 text-slate-600";
  const dot = COVERAGE_DOTS[status] ?? "bg-slate-400";
  return (
    <span
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-0.5 text-[0.6875rem] font-semibold tracking-wide ${style}`}
    >
      <span aria-hidden className={`h-1.5 w-1.5 rounded-full ${dot}`} />
      {status.replaceAll("_", " ")}
    </span>
  );
}

export function BillStatusBadge({ status }: { status?: string | null }) {
  if (!status) {
    return (
      <span className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full bg-slate-100 px-2 py-0.5 text-[0.6875rem] font-semibold text-slate-600">
        <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-slate-400" />
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
  const dot = style.includes("emerald")
    ? "bg-emerald-600"
    : style.includes("red")
      ? "bg-red-600"
      : style.includes("amber")
        ? "bg-amber-600"
        : "bg-slate-400";
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-0.5 text-[0.6875rem] font-semibold ${style}`}>
      <span aria-hidden className={`h-1.5 w-1.5 rounded-full ${dot}`} />
      {label}
    </span>
  );
}
