/**
 * tools/lint_dashboard.mjs — lint the dashboard's inline JavaScript.
 *
 * RaanuTradingBot.html carries ~1,100 lines of JS in <script> blocks that no
 * Python tooling sees. `node --check` only validates syntax, which is how a
 * temporal-dead-zone bug shipped: a `const shown` declared late in a block
 * shadowed an outer `shown` read earlier in that same block, so every poll
 * tick threw ReferenceError before it could render results or stop its
 * interval. The scan worked; the UI froze on its first frame and polled
 * forever. Syntax was perfectly valid.
 *
 *     node tools/lint_dashboard.mjs
 */
import { readFileSync, writeFileSync, rmSync } from "node:fs";
import { ESLint } from "eslint";

// Optional argument lets the test suite lint a scratch copy.
const HTML = process.argv[2] || "RaanuTradingBot.html";
const html = readFileSync(HTML, "utf8");

// Keep line numbers aligned with the HTML so reported locations are useful.
const lines = html.split("\n");
const out = new Array(lines.length).fill("");
let inScript = false;
lines.forEach((line, i) => {
  if (/<script>/.test(line)) { inScript = true; return; }
  if (/<\/script>/.test(line)) { inScript = false; return; }
  if (inScript) out[i] = line;
});

// Written inside the repo, not a tmpdir: ESLint refuses to lint files
// outside its base path. Gitignored, and removed again below.
const file = ".dashboard-inline.js";
writeFileSync(file, out.join("\n"));

const eslint = new ESLint({
  overrideConfigFile: true,
  overrideConfig: {
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "script",
      globals: Object.fromEntries(
        ["window","document","fetch","console","localStorage","setInterval",
         "clearInterval","setTimeout","navigator","location","alert","prompt",
         "EventSource","Notification","atob","btoa","Chart","caches","Intl",
         "requestAnimationFrame","URLSearchParams","confirm","matchMedia",
         "addEventListener","removeEventListener","Image","Blob","URL"].map(g => [g, "readonly"])),
    },
    rules: {
      // The one that would have caught it: reading a binding declared later
      // in the same block.
      "no-use-before-define": ["error", { variables: true, functions: false, classes: false }],
      "no-undef": "error",
      "no-dupe-keys": "error",
      "no-unreachable": "error",
      "no-const-assign": "error",
      "no-dupe-args": "error",
      "no-cond-assign": "error",
      "no-self-compare": "error",
    },
  },
});

let results;
try {
  results = await eslint.lintFiles([file]);
} finally {
  rmSync(file, { force: true });
}
let problems = 0;
for (const r of results) {
  for (const m of r.messages) {
    problems++;
    console.log(`${HTML}:${m.line}:${m.column}  ${m.ruleId ?? "parse"}  ${m.message}`);
  }
}
if (problems) {
  console.log(`\n${problems} problem(s) in the dashboard's inline JS`);
  process.exit(1);
}
console.log("dashboard JS: clean");
