"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import SearchBox from "./SearchBox";

type NavigationItem = {
  href: string;
  label: string;
  beta?: boolean;
};

const PRIMARY_NAV: NavigationItem[] = [
  { href: "/states", label: "States" },
  { href: "/topics", label: "Topics" },
  { href: "/reports/2026-bill-mortality", label: "Reports" },
  { href: "/coverage", label: "Coverage" },
];

const RESOURCE_NAV: NavigationItem[] = [
  { href: "/docs/api", label: "API" },
  { href: "/docs/agents", label: "AI Agents" },
  { href: "/pricing", label: "Pricing" },
  { href: "/quality", label: "Data quality" },
  { href: "/methodology", label: "Methodology" },
  { href: "/about", label: "About" },
];

// A private canary can enable the direct /scout entry path without advertising
// it in global navigation. Public launch deliberately flips a second flag.
const SCOUT_ENABLED = process.env.NEXT_PUBLIC_SCOUT_NAV_ENABLED === "true";

function NavigationLabel({ label, beta = false }: { label: string; beta?: boolean }) {
  return (
    <>
      {label}
      {beta ? (
        <span className="ml-1 border-l border-blue-700/70 pl-1 text-[0.625rem] font-semibold uppercase tracking-[0.1em] text-blue-800">
          Beta
        </span>
      ) : null}
    </>
  );
}

export default function SiteHeader() {
  const pathname = usePathname();
  const active = (href: string) => pathname === href || pathname.startsWith(`${href}/`);
  const moreActive = RESOURCE_NAV.some((item) => active(item.href));
  const navigation = SCOUT_ENABLED
    ? [{ href: "/scout", label: "Scout", beta: true }, ...PRIMARY_NAV]
    : PRIMARY_NAV;
  const hasBetaNavigation = navigation.some((item) => item.beta);

  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-3 px-4 py-3 sm:flex-nowrap sm:px-6">
        <Link
          href="/"
          className="shrink-0 rounded-sm text-[1.05rem] font-semibold tracking-[-0.02em] text-slate-950"
        >
          <span className="font-normal">Bill</span> Commons
        </Link>

        <nav
          aria-label="Primary"
          className="order-2 ml-auto sm:ml-0 sm:min-w-0 sm:flex-1"
        >
          <details className="group relative sm:hidden">
            <summary className={`site-nav-summary cursor-pointer list-none rounded-sm border border-slate-300 px-3 py-2 text-sm font-semibold text-slate-700 hover:bg-slate-50 ${navigation.some((item) => active(item.href)) || moreActive ? "text-slate-950" : ""}`}>
              Menu
              {hasBetaNavigation ? (
                <span className="ml-1 border-l border-blue-700/70 pl-1 text-[0.625rem] font-semibold uppercase tracking-[0.1em] text-blue-800">
                  Beta
                </span>
              ) : null}
              <span aria-hidden="true" className="ml-1 text-slate-400 group-open:hidden">+</span><span aria-hidden="true" className="ml-1 hidden text-slate-400 group-open:inline">−</span>
            </summary>
            <ul className="absolute right-0 z-20 mt-1 w-52 border border-slate-200 bg-white py-1 text-sm font-medium text-slate-700 shadow-sm">
              {[...navigation, ...RESOURCE_NAV].map((item) => {
                const isActive = active(item.href);
                return (
                  <li key={item.href}>
                    <Link href={item.href} aria-current={isActive ? "page" : undefined} className={`block border-l-2 px-3 py-2 hover:bg-slate-50 hover:text-slate-950 ${isActive ? "border-blue-700 bg-slate-50 font-semibold text-slate-950" : "border-transparent"}`}>
                      <NavigationLabel label={item.label} beta={item.beta} />
                    </Link>
                  </li>
                );
              })}
            </ul>
          </details>
          <ul className="hidden items-center gap-0.5 text-[0.8125rem] font-medium text-slate-600 sm:flex">
            {navigation.map((item) => {
              const isActive = active(item.href);
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    aria-current={isActive ? "page" : undefined}
                    className={`block rounded-sm px-2 py-1.5 hover:bg-slate-100 hover:text-slate-950 ${
                      isActive ? "bg-slate-100 text-slate-950" : ""
                    }`}
                  >
                    <NavigationLabel label={item.label} beta={item.beta} />
                  </Link>
                </li>
              );
            })}
            <li className="relative">
              <details className="group">
                <summary className={`site-nav-summary cursor-pointer list-none rounded-sm px-2 py-1.5 hover:bg-slate-100 hover:text-slate-950 ${moreActive ? "bg-slate-100 text-slate-950" : ""}`}>
                  Resources <span aria-hidden="true" className="ml-0.5 text-slate-400 group-open:hidden">+</span><span aria-hidden="true" className="ml-0.5 text-slate-400 hidden group-open:inline">−</span>
                </summary>
                <ul className="absolute right-0 z-20 mt-1 w-40 border border-slate-200 bg-white py-1 shadow-sm">
                  {RESOURCE_NAV.map((item) => {
                    const isActive = active(item.href);
                    return (
                      <li key={item.href}>
                        <Link href={item.href} aria-current={isActive ? "page" : undefined} className={`block px-3 py-2 text-sm hover:bg-slate-50 hover:text-slate-950 ${isActive ? "font-semibold text-slate-950" : ""}`}>
                          {item.label}
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              </details>
            </li>
          </ul>
        </nav>

        <div className="order-3 w-full shrink-0 sm:w-56">
          <SearchBox compact />
        </div>
      </div>
    </header>
  );
}
