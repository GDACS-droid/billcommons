import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import test from "node:test";

// Exercise the TypeScript implementation without adding a component-test dependency.
const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = await readFile(new URL("./funnel.ts", import.meta.url), "utf8");
const javascript = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;

function loadFunnel(track) {
  const compiled = { exports: {} };
  new Function("exports", "module", "require", javascript)(
    compiled.exports,
    compiled,
    (specifier) => {
      if (specifier === "@vercel/analytics") return { track };
      throw new Error(`Unexpected import: ${specifier}`);
    },
  );
  return compiled.exports;
}

test("funnel dimensions are allowlisted and remove sensitive checkout input", () => {
  const { sanitizeFunnelProperties } = loadFunnel(() => {});
  const properties = sanitizeFunnelProperties("checkout_intent", {
    product: "subscription",
    plan: "builder",
    interval: "monthly",
    email: "person@example.com",
    checkout_url: "https://checkout.stripe.com/c/pay/cs_private",
    session_id: "cs_private",
    api_key: "bc_live_secret",
    query: "sensitive legislative research",
  });

  assert.deepEqual(properties, { product: "subscription", plan: "builder", interval: "monthly" });
  const serialized = JSON.stringify(properties);
  for (const secret of ["person@example.com", "checkout.stripe.com", "cs_private", "bc_live_secret", "sensitive legislative research"]) {
    assert.equal(serialized.includes(secret), false);
  }
  assert.deepEqual(sanitizeFunnelProperties("magic_link_requested", { email: "person@example.com" }), { surface: "api_docs" });
  assert.deepEqual(sanitizeFunnelProperties("api_key_revealed", { operation: "reveal", key: "bc_live_secret" }), { operation: "reveal" });
});

test("tracking provider failures cannot break the calling flow", () => {
  const { trackFunnel } = loadFunnel(() => {
    throw new Error("analytics blocked");
  });

  assert.doesNotThrow(() => trackFunnel("checkout_redirect_created", {
    product: "snapshot",
    scope: "full",
    url: "https://checkout.stripe.com/c/pay/cs_private",
  }));
});

test("tracking receives only the sanitized event payload", () => {
  const calls = [];
  const { trackFunnel } = loadFunnel((event, properties) => calls.push({ event, properties }));

  trackFunnel("api_key_revealed", { operation: "rotate", key: "bc_live_secret" });
  assert.deepEqual(calls, [{ event: "api_key_revealed", properties: { operation: "rotate" } }]);
});
