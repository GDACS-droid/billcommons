export default function DataUnavailable({
  message,
  detail,
}: {
  message?: string;
  detail?: string;
}) {
  return (
    <div
      role="status"
      className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-6 text-sm text-slate-800"
    >
      <p className="font-medium">
        {message ?? "This data is temporarily unavailable."}
      </p>
      <p className="mt-1 text-slate-600">
        {detail ??
          "The Bill Commons API could not be reached. Please try again shortly."}
      </p>
    </div>
  );
}
