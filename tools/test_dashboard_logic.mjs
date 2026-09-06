/**
 * tools/test_dashboard_logic.mjs — behavioural tests for the dashboard's
 * pure view logic.
 *
 * lint_dashboard.mjs proves the inline JS parses and has no undefined names.
 * It cannot tell you that a grouping function groups correctly. This runs the
 * real functions — extracted from the HTML, not reimplemented — against
 * result shapes taken from an actual scan.
 *
 *     node tools/test_dashboard_logic.mjs
 */
import { readFileSync } from "node:fs";

const HTML = process.argv[2] || "RaanuTradingBot.html";
const src = readFileSync(HTML, "utf8");

/** Pull one top-level `function name(...)  {...}` out of the page by
 *  brace-matching, so the test executes the shipped code rather than a copy
 *  that can drift away from it. */
function extractFunction(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`function ${name} not found in ${HTML}`);
  let depth = 0, i = src.indexOf("{", start);
  const open = i;
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`unbalanced braces in ${name}`);
}

function extractConst(name) {
  const m = new RegExp(`^const ${name} = .*?;$`, "m").exec(src);
  if (!m) throw new Error(`const ${name} not found`);
  return m[0];
}

// Stubs for the presentation helpers these functions lean on. Deliberately
// dumb: the tests assert on grouping, coalescing and ordering, not on markup.
const preamble = `
  const SORDER = ['s1','s2','s3'];
  const stag  = k => '[' + String(k).toUpperCase() + ']';
  const money = n => '$' + Number(n).toFixed(2);
  const pct   = n => Number(n).toFixed(2) + '%';
  const cls   = n => (n >= 0 ? 'up' : 'down');
`;

// paintSignals talks to the DOM; a two-element stub is enough to observe
// what it writes and which filter it read.
const domStub = `
  const _dom = { sig: { innerHTML: "" }, scanFilter: { value: "all" } };
  const el = id => _dom[id];
`;

const body = [
  preamble,
  domStub,
  extractConst("firstOf"),
  extractConst("attr"),
  extractFunction("renderScanRows"),
  extractFunction("multiGroups"),
  extractFunction("renderMultiRows"),
  extractFunction("paintSignals"),
  "let scanResults = []; let scanPaintKey = null;",
  `return { multiGroups, renderMultiRows, firstOf, attr, paintSignals, _dom,
            setResults: r => { scanResults = r; },
            resetKey: () => { scanPaintKey = null; } };`,
].join("\n");

const api = new Function(body)();

let failures = 0;
function check(label, cond, detail = "") {
  if (cond) return;
  failures++;
  console.error(`  FAIL  ${label}${detail ? "\n        " + detail : ""}`);
}

// Shapes lifted from a real scan: S2 genuinely omits mom_1m and atr_pct,
// and the shared numeric fields are identical across strategies.
const row = (ticker, strategy, score, extra = {}) => ({
  ticker, strategy, score, name: ticker + " Inc",
  price: 100, rsi: 55, mom_3m: 12.5, rel_strength: 4.2,
  reasons: [`${strategy.toUpperCase()} reason for ${ticker}`],
  ...extra,
});

const SCAN = [
  row("AMGN", "s1", 62, { mom_1m: 5.2, atr_pct: 3.2 }),
  row("AMGN", "s2", 61, { mom_1m: null, atr_pct: null }),
  row("NVDA", "s2", 88, { mom_1m: null }),
  row("NVDA", "s1", 74, { mom_1m: 9.1 }),
  row("SOLO", "s1", 70, { mom_1m: 1.0 }),      // single strategy — excluded
  row("DEEP", "s3", 65, { mom_1m: -8.0 }),     // single strategy — excluded
];

console.log(`dashboard logic (${HTML})`);

// ── multiGroups ────────────────────────────────────────────────────────────
const groups = api.multiGroups(SCAN);
check("only multi-strategy tickers are kept", groups.length === 2,
      `got ${groups.length}: ${groups.map(g => g[0].ticker).join(",")}`);
check("single-strategy tickers are excluded",
      !groups.flat().some(r => ["SOLO", "DEEP"].includes(r.ticker)));

// Discriminating pair, deliberately: LOPSIDE has one strong and one marginal
// score, EVENPAIR has two middling ones. Max puts LOPSIDE first, average puts
// EVENPAIR first — so this catches a silent switch to averaging, which an
// earlier version of this test did not.
//    LOPSIDE  90 / 50  -> max 90, avg 70
//    EVENPAIR 75 / 74  -> max 75, avg 74.5
const ordering = api.multiGroups([
  row("EVENPAIR", "s1", 75), row("EVENPAIR", "s2", 74),
  row("LOPSIDE", "s1", 90), row("LOPSIDE", "s2", 50),
]);
check("groups are ordered by the BEST score, not the average",
      ordering[0][0].ticker === "LOPSIDE",
      `max-ordering puts LOPSIDE (90) first, averaging puts EVENPAIR (74.5) first; got ${ordering[0][0].ticker}`);

check("parts within a group are in strategy order regardless of input order",
      groups.every(g => g.map(r => r.strategy).join(",") === "s1,s2"),
      groups.map(g => g.map(r => r.strategy).join(",")).join(" | "));

check("an empty scan produces no groups", api.multiGroups([]).length === 0);
check("null-safe on junk rows",
      api.multiGroups([null, {}, row("A", "s1", 1)]).length === 0);

// A ticker under all three strategies stays one group.
const triple = api.multiGroups([row("X", "s1", 1), row("X", "s2", 2), row("X", "s3", 3)]);
check("three strategies fold into a single group",
      triple.length === 1 && triple[0].length === 3);

// ── firstOf: the coalesce that fills S2's gaps ─────────────────────────────
const amgn = groups.find(g => g[0].ticker === "AMGN");
check("coalesce takes the first non-null across the group",
      api.firstOf(amgn, "mom_1m") === 5.2,
      `got ${api.firstOf(amgn, "mom_1m")}`);
check("coalesce returns null when no member has the field",
      api.firstOf(amgn, "nonexistent") === null);

// ── renderMultiRows ────────────────────────────────────────────────────────
const html = api.renderMultiRows(groups);
check("both strategy tags appear in one row", html.includes("[S1] [S2]"));
check("both scores are shown side by side, not averaged", html.includes("74 / 88"),
      "expected NVDA's row to read 74 / 88");
check("no averaged score leaks into the markup", !html.includes("81") || !html.includes("81 "),
      "an averaged 81 should not appear as a score");
check("the top-scoring strategy's reason is the visible one",
      html.includes("S2 reason for NVDA"));
check("every strategy's reason survives in the tooltip",
      html.includes("S1 reason for NVDA"));
check("1M is filled from S1 even though S2 lacks it", html.includes("5.20%"));

const empty = api.renderMultiRows([]);
check("empty state explains itself rather than looking broken",
      empty.includes("more than") && empty.includes("not an error"), empty);

// ── attr: reasons land in a title attribute ────────────────────────────────
check("quotes cannot terminate the title attribute early",
      api.attr('a "b" c') === "a &quot;b&quot; c", api.attr('a "b" c'));
check("angle brackets are neutralised", api.attr("<img>") === "&lt;img>");
check("ampersands are escaped first, not doubly",
      api.attr('&"') === "&amp;&quot;", api.attr('&"'));

// ── paintSignals: filter dispatch and the repaint key ──────────────────────
api.setResults(SCAN);
const dom = api._dom;
const paint = (filter) => { dom.scanFilter.value = filter; api.paintSignals(); };

paint("all");
check("All shows every row, unmerged", (dom.sig.innerHTML.match(/<tr>/g) || []).length === SCAN.length,
      `expected ${SCAN.length} rows, got ${(dom.sig.innerHTML.match(/<tr>/g) || []).length}`);

paint("s3");
check("filtering to S3 shows only S3 rows",
      dom.sig.innerHTML.includes("DEEP") && !dom.sig.innerHTML.includes("NVDA"));

// The bug this guards: the old dedupe compared only the row COUNT, so
// switching filters left the previous rows on screen because the underlying
// result count had not moved.
paint("s1");
check("switching filter repaints even though the result count is unchanged",
      dom.sig.innerHTML.includes("NVDA") && !dom.sig.innerHTML.includes("DEEP"),
      "S3 rows still on screen after switching to S1");

// ...but an unchanged filter and count must NOT repaint, or every 1.5s poll
// would reset scroll position and hover mid-scan.
dom.sig.innerHTML = "SENTINEL";
api.paintSignals();
check("an unchanged filter and count does not repaint", dom.sig.innerHTML === "SENTINEL",
      "repainted when nothing changed — scroll and hover would reset every poll");

api.paintSignals(true);
check("force repaints anyway", dom.sig.innerHTML !== "SENTINEL");

paint("multi");
check("Multiple shows merged rows only",
      dom.sig.innerHTML.includes("[S1] [S2]") && !dom.sig.innerHTML.includes("DEEP"));

api.setResults([]);
api.resetKey();
paint("multi");
check("Multiple on an empty scan shows its own empty state",
      dom.sig.innerHTML.includes("not an error"));

if (failures) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("  all checks passed");
