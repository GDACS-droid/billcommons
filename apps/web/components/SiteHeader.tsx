"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import SearchBox from "./SearchBox";

const NAV = [
  { href: "/states", label: "States" },
  { href: "/topics", label: "Topics" },
  { href: "/reports/2026-bill-mortality", label: "Reports" },
  { href: "/coverage", label: "Coverage" },
  { href: "/docs/api", label: "API" },
  { href: "/docs/agents", label: "AI Agents" },
  { href: "/quality", label: "Data quality" },
  { href: "/methodology", label: "Methodology" },
  { href: "/about", label: "About" },
];

export default function SiteHeader() {
  const pathname = usePathname();

  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-3 px-4 py-3 sm:px-6">
        <Link
          href="/"
          className="shrink-0 rounded-sm text-[1.05rem] font-semibold tracking-[-0.02em] text-slate-950"
        >
          <span className="font-normal">Bill</span> Commons
        </Link>

        <nav
          aria-label="Primary"
          className="order-3 w-full overflow-x-auto sm:order-2 sm:w-auto sm:flex-1"
        >
          <ul className="flex min-w-max gap-1 text-[0.8125rem] font-medium text-slate-500 sm:min-w-0 sm:flex-wrap">
            {NAV.map((item) => {
              const active =
                pathname === item.href || pathname.startsWith(`${item.href}/`);
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    aria-current={active ? "page" : undefined}
                    className={`block rounded-md px-2 py-1.5 transition-colors hover:bg-slate-50 hover:text-slate-950 ${
                      active ? "bg-blue-50 text-blue-800" : ""
                    }`}
                  >
                    {item.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>

        <div className="order-2 ml-auto w-full max-w-xs sm:order-3 sm:w-60">
          <SearchBox compact />
        </div>
      </div>
    </header>
  );
}
