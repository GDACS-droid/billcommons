import Link from "next/link";

export default function SiteFooter() {
  return (
    <footer className="mt-16 border-t border-slate-200 bg-slate-50">
      <div className="mx-auto max-w-6xl px-4 py-10 text-sm text-slate-600 sm:px-6">
        <div className="grid gap-8 sm:grid-cols-3">
          <div>
            <p className="font-semibold text-slate-900">Bill Commons</p>
            <p className="mt-2 max-w-xs">
              Free, open-source, nonpartisan legislative search covering all
              50 states and DC.
            </p>
          </div>
          <div>
            <p className="font-semibold text-slate-900">Data &amp; access</p>
            <ul className="mt-2 space-y-1.5">
              <li>
                <Link className="hover:underline" href="/coverage">
                  Coverage status
                </Link>
              </li>
              <li>
                <Link className="hover:underline" href="/methodology">
                  Methodology
                </Link>
              </li>
              <li>
                <Link className="hover:underline" href="/docs/api">
                  REST API docs
                </Link>
              </li>
              <li>
                <Link className="hover:underline" href="/docs/mcp">
                  MCP server
                </Link>
              </li>
            </ul>
          </div>
          <div>
            <p className="font-semibold text-slate-900">Project</p>
            <ul className="mt-2 space-y-1.5">
              <li>
                <Link className="hover:underline" href="/about">
                  About &amp; license
                </Link>
              </li>
              <li>
                <a
                  className="hover:underline"
                  href="https://github.com/GDACS-droid/billcommons"
                >
                  Source on GitHub
                </a>
              </li>
              <li>
                <Link className="hover:underline" href="/services">
                  Custom tracking &amp; consulting
                </Link>
              </li>
            </ul>
          </div>
        </div>
        <p className="mt-8 border-t border-slate-200 pt-6 text-xs text-slate-500">
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
