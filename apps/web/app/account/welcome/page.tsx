import type { Metadata } from "next";
import Link from "next/link";
import PageHeader from "@/components/PageHeader";

export const metadata: Metadata = {
  title: "Check your email",
  description: "Your Bill Commons order is confirmed — check your email to sign in.",
  robots: { index: false, follow: false },
};

/**
 * B1 (2026-08-21 monetization spec, round-1b amendment): Checkout's
 * success_url. This page is deliberately static and does NOT read
 * `session_id` or reveal anything -- `checkout.session.completed` mints
 * the key server-side and emails a MAGIC LINK, never the key itself. The
 * customer clicks that link, lands on `/account/login`, and reveals the
 * key from there (self-serve keys are shown inline at mint time; a
 * checkout-minted key is revealed once via `POST /account/keys/{id}/reveal`
 * from a logged-in `/account`).
 */
export default function AccountWelcomePage() {
  return (
    <div className="mx-auto max-w-md px-4 py-16 sm:px-6">
      <PageHeader
        eyebrow="Bill Commons"
        title="Thanks — check your email"
        description={
          <p>
            We&apos;ve emailed a sign-in link to the address you used at
            checkout. Click it to finish setting up your account and reveal
            your API key (subscriptions) or check your order status
            (snapshots).
          </p>
        }
      />
      <p className="mt-6 text-sm text-slate-600">
        Didn&apos;t get it in a couple of minutes? Check spam, or request a
        new link from the{" "}
        <Link href="/docs/api-keys" className="underline">
          API keys page
        </Link>
        .
      </p>
    </div>
  );
}
