import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = await readFile(new URL("./scoutAccess.ts", import.meta.url), "utf8");
const javascript = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const compiled = { exports: {} };
new Function("exports", "module", javascript)(compiled.exports, compiled);

const { isDarkScoutRoute } = compiled.exports;

test("disabled Scout routes fail dark, including nested paths", () => {
  assert.equal(isDarkScoutRoute("/scout", undefined), true);
  assert.equal(isDarkScoutRoute("/scout", "false"), true);
  assert.equal(isDarkScoutRoute("/scout/evidence", "false"), true);
});

test("only an exact true flag enables Scout and other routes are unaffected", () => {
  assert.equal(isDarkScoutRoute("/scout", "true"), false);
  assert.equal(isDarkScoutRoute("/scout", "TRUE"), true);
  assert.equal(isDarkScoutRoute("/scouting", undefined), false);
  assert.equal(isDarkScoutRoute("/", undefined), false);
});
