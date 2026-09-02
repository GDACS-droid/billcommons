import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("./SiteHeader.tsx", import.meta.url), "utf8");

test("Scout navigation remains flag-gated and uses the restrained Beta marker in both menus", () => {
  assert.match(source, /NEXT_PUBLIC_SCOUT_NAV_ENABLED === "true"/);
  assert.match(source, /\{ href: "\/scout", label: "Scout", beta: true \}/);
  assert.match(source, /border-l border-blue-700\/70 pl-1 text-\[0\.625rem\] font-semibold uppercase/);
  assert.match(source, /const hasBetaNavigation = navigation\.some\(\(item\) => item\.beta\)/);
  assert.match(source, /Menu\s*\{hasBetaNavigation \?/);
  assert.equal(
    (source.match(/<NavigationLabel label=\{item\.label\} beta=\{item\.beta\} \/>/g) ?? []).length,
    2,
  );
});
