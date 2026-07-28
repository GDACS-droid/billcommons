function SearchIcon() {
  return (
    <svg
      aria-hidden
      viewBox="0 0 20 20"
      fill="none"
      className="h-4 w-4 text-slate-400"
    >
      <path
        d="M9 3a6 6 0 104.472 10.028l3.25 3.25a.75.75 0 101.06-1.06l-3.25-3.25A6 6 0 009 3zm-4.5 6a4.5 4.5 0 119 0 4.5 4.5 0 01-9 0z"
        fill="currentColor"
      />
    </svg>
  );
}

export default function SearchBox({
  compact = false,
  autoFocus = false,
  defaultValue = "",
}: {
  compact?: boolean;
  autoFocus?: boolean;
  defaultValue?: string;
}) {
  return (
    <form action="/search" method="GET" role="search" aria-label="Search legislation">
      <label htmlFor={compact ? "q-compact" : "q"} className="sr-only">
        Search bills by number, keyword, or full text
      </label>
      <div
        className={`flex items-center gap-2 rounded-md border border-slate-300 bg-white px-3 shadow-[0_1px_2px_rgba(15,23,42,0.04)] transition-colors focus-within:border-blue-700 focus-within:ring-4 focus-within:ring-blue-700/10 ${
          compact ? "py-1" : "py-3.5"
        }`}
      >
        <SearchIcon />
        <input
          id={compact ? "q-compact" : "q"}
          name="q"
          type="search"
          autoFocus={autoFocus}
          defaultValue={defaultValue}
          placeholder={
            compact
              ? "Search bills…"
              : "Search all 50 states + DC — e.g. “HB 123” or “paid family leave”"
          }
          className={`w-full bg-transparent text-slate-900 placeholder:text-slate-400 focus:outline-none ${
            compact ? "text-sm" : "text-base"
          }`}
        />
        <button
          type="submit"
          className={`shrink-0 rounded-md bg-blue-700 font-medium text-white transition-colors hover:bg-blue-800 ${
            compact ? "px-2.5 py-1 text-xs" : "px-4 py-2 text-sm"
          }`}
        >
          Search
        </button>
      </div>
    </form>
  );
}
