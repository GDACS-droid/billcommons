import Link from "next/link";
import SearchBox from "./SearchBox";

const NAV = [
  { href: "/states", label: "States" },
  { href: "/topics", label: "Topics" },
  { href: "/reports/2026-bill-mortality", label: "Reports" },
  { href: "/coverage", label: "Coverage" },
  { href: "/docs/api", label: "API" },
  { href: "/docs/agents", label: "AI Agents" },
  { href: "/methodology", label: "Methodology" },
  { href: "/about", label: "About" },
];

export default function SiteHeader() {
  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-4 px-4 py-4 sm:px-6">
        <Link
          href="/"
          className="flex items-center gap-2 text-lg font-semibold tracking-tight text-slate-900"
        >
          <span
            aria-hidden
            className="inline-block h-2.5 w-2.5 rounded-full bg-amber-500"
          />
          Bill Commons
        </Link>

        <nav
          aria-label="Primary"
          className="order-3 w-full overflow-x-auto sm:order-2 sm:w-auto sm:flex-1"
        >
          <ul className="flex min-w-max gap-5 text-sm font-medium text-slate-600 sm:min-w-0 sm:flex-wrap">
            {NAV.map((item) => (
              <li key={item.href}>
                <Link
                  href={item.href}
                  className="rounded px-1 py-1 hover:text-slate-900 hover:underline hover:underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber-500"
                >
                  {item.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>

        <div className="order-2 ml-auto w-full max-w-xs sm:order-3 sm:w-64">
          <SearchBox compact />
        </div>
      </div>
    </header>
  );
}
