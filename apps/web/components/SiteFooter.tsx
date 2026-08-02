import Link from "next/link";

export default function SiteFooter() {
  return (
    <footer className="mt-20 border-t border-slate-200 bg-slate-50">
      <div className="mx-auto max-w-6xl px-4 py-12 text-sm text-slate-600 sm:px-6">
        <div className="grid gap-10 sm:grid-cols-[1.4fr_repeat(3,1fr)]">
          <div>
            <p className="text-base font-semibold tracking-tight text-slate-950">
              <span className="font-normal">Bill</span> Commons
            </p>
            <p className="mt-3 max-w-xs leading-6">
              Free, open-source, nonpartisan legislative search covering all
              50 states and DC.
            </p>
          </div>
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.1em] text-slate-900">
              Product
            </p>
            <ul className="mt-4 space-y-2">
              <li>
                <Link className="transition-colors hover:text-blue-800" href="/states">
                  States
                </Link>
              </li>
              <li>
                <Link className="transition-colors hover:text-blue-800" href="/topics">
                  Topics
                </Link>
              </li>
              <li>
                <Link className="transition-colors hover:text-blue-800" href="/reports/2026-bill-mortality">
                  Reports
                </Link>
              </li>
            </ul>
          </div>
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.1em] text-slate-900">
              Data
            </p>
            <ul className="mt-4 space-y-2">
              <li>
                <Link className="transition-colors hover:text-blue-800" href="/coverage">
                  Coverage status
                </Link>
              </li>
              <li>
                <Link className="transition-colors hover:text-blue-800" href="/methodology">
                  Methodology
                </Link>
              </li>
              <li>
                <Link className="transition-colors hover:text-blue-800" href="/docs/api">
                  REST API docs
                </Link>
              </li>
              <li>
                <Link className="transition-colors hover:text-blue-800" href="/docs/mcp">
                  MCP server
                </Link>
              </li>
            </ul>
          </div>
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.1em] text-slate-900">
              Company
            </p>
            <ul className="mt-4 space-y-2">
              <li>
                <Link className="transition-colors hover:text-blue-800" href="/about">
                  About &amp; license
                </Link>
              </li>
              <li>
                <a
                  className="transition-colors hover:text-blue-800"
                  href="https://github.com/GDACS-droid/billcommons"
                >
                  Source on GitHub
                </a>
              </li>
              <li>
                <Link className="transition-colors hover:text-blue-800" href="/changelog">
                  Changelog &amp; limitations
                </Link>
              </li>
              <li>
                <Link className="transition-colors hover:text-blue-800" href="/feedback">
                  Feedback
                </Link>
              </li>
            </ul>
          </div>
        </div>
        <p className="mt-10 border-t border-slate-200 pt-6 text-xs leading-5 text-slate-500">
          Bill Commons is an independent, nonpartisan project. Legislative
          data is sourced from official state records and Open States;
          see the{" "}
          <Link className="underline" href="/methodology">
            methodology page
          </Link>{" "}
          for attribution and known limitations. Not affiliated with any
          government body.
        </p>
      </div>
    </footer>
  );
}
