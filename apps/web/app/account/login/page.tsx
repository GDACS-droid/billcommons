import type { Metadata } from "next";
import PageHeader from "@/components/PageHeader";
import LoginContinueButton from "@/components/LoginContinueButton";

export const metadata: Metadata = {
  title: "Sign in",
  description: "Continue signing in to your Bill Commons account.",
  robots: { index: false, follow: false },
};

/**
 * D7 (2026-08-21 monetization spec, round-3 amendment): the emailed magic
 * link points HERE, a static page, never at the API directly -- a `GET`
 * must never consume a single-use login token (an email scanner/proxy that
 * pre-fetches links would burn it before the human ever clicks). The
 * "Continue" button below is what actually calls
 * `POST /api/v1/account/session {token}`.
 */
export default async function AccountLoginPage({
  searchParams,
}: {
  searchParams: Promise<{ token?: string }>;
}) {
  const sp = await searchParams;
  const token = sp.token ?? "";

  return (
    <div className="mx-auto max-w-md px-4 py-16 sm:px-6">
      <PageHeader
        eyebrow="Bill Commons account"
        title="Sign in"
        description={
          token ? (
            <p>Click continue to finish signing in.</p>
          ) : (
            <p>
              This link is missing its sign-in token. Request a new one from{" "}
              <a href="/docs/api-keys" className="underline">
                the API keys page
              </a>
              .
            </p>
          )
        }
      />
      {token ? <LoginContinueButton token={token} /> : null}
    </div>
  );
}
