/**
 * Canonical, human-readable bill URLs.
 *
 * Bills were originally addressed only by UUID (/bills/<uuid>). That URL is
 * unguessable, carries no keyword signal, and is not something anybody types or
 * links -- people search for "Georgia SB 160". The canonical form is therefore
 *
 *     /states/GA/bills/2025-26/sb-160
 *
 * and /bills/<uuid> permanently redirects onto it, so existing links keep
 * working while all ranking signal consolidates on one URL per bill.
 *
 * Uniqueness: (jurisdiction, session, identifier_norm) was verified distinct
 * across all 209,612 bills, and slugified session identifiers were verified
 * distinct within every jurisdiction -- so this path always names one bill.
 */

/** Lowercase, hyphenate, collapse runs, trim edges. Used for both segments. */
export function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

/** "SB 160" -> "sb-160" */
export function billSlug(identifierNorm: string): string {
  return slugify(identifierNorm);
}

/**
 * Turn a bill slug back into something the API's `identifier` filter accepts.
 * The filter normalizes its input, so "sb-160" -> "sb 160" -> "SB 160" without
 * the web app having to reimplement normalize_bill_number.
 */
export function billSlugToIdentifier(slug: string): string {
  return slug.replace(/-/g, " ");
}

export function billPath(
  jurisdictionCode: string,
  sessionIdentifier: string,
  identifierNorm: string
): string {
  return `/states/${jurisdictionCode.toUpperCase()}/bills/${slugify(
    sessionIdentifier
  )}/${billSlug(identifierNorm)}`;
}
