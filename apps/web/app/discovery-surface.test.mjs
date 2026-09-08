import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const [layout, sitemap, apiDocs] = await Promise.all([
  readFile(new URL("./layout.tsx", import.meta.url), "utf8"),
  readFile(new URL("../lib/sitemap.ts", import.meta.url), "utf8"),
  readFile(new URL("./docs/api/page.tsx", import.meta.url), "utf8"),
]);

test("search-engine verification is opt-in and supports Google and Bing", () => {
  assert.match(layout, /process\.env\.GOOGLE_SITE_VERIFICATION/);
  assert.match(layout, /process\.env\.BING_SITE_VERIFICATION/);
  assert.match(layout, /google: googleSiteVerification/);
  assert.match(layout, /"msvalidate\.01": bingSiteVerification/);
});

test("sitemap submits useful conversion and integration documentation", () => {
  for (const route of ["/pricing", "/docs/api-keys", "/docs/bulk", "/docs/agents"]) {
    assert.match(sitemap, new RegExp(`"${route}"`));
  }
  assert.doesNotMatch(sitemap, /"\/hearings"/);
});

test("API documentation sends high-volume visitors to live access paths", () => {
  assert.match(apiDocs, /href="\/docs\/api-keys"/);
  assert.match(apiDocs, /href="\/docs\/bulk"/);
  assert.doesNotMatch(apiDocs, /API keys for higher-volume tiers are\s+planned/);
});
