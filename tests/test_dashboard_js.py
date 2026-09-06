"""The dashboard's inline JavaScript.

RaanuTradingBot.html carries ~1,100 lines of JS that no Python tooling sees.
A temporal-dead-zone bug shipped through both `node --check` and the whole
Python suite: a `const shown` declared late in a block shadowed an outer
`shown` read earlier in that same block, so every scan-poll tick threw
ReferenceError before it could render results or clear its interval. The
scan completed fine; the UI froze on its first frame and polled forever.
Syntax was valid, so nothing caught it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LINTER = ROOT / "tools" / "lint_dashboard.mjs"
LOGIC = ROOT / "tools" / "test_dashboard_logic.mjs"


requires_node = pytest.mark.skipif(
    shutil.which("node") is None or not (ROOT / "node_modules" / "eslint").exists(),
    reason="node + `npm install` needed for the dashboard linter",
)


@requires_node
def test_dashboard_inline_js_is_clean():
    result = subprocess.run(
        ["node", str(LINTER)], cwd=ROOT, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, f"dashboard JS problems:\n{result.stdout}{result.stderr}"


@requires_node
def test_the_linter_actually_catches_a_use_before_define(tmp_path):
    """Guards the guard.

    A linter that silently stopped checking would let the next one through,
    so this reintroduces the exact defect shape in a scratch copy and
    asserts it is reported.
    """
    html = (ROOT / "RaanuTradingBot.html").read_text()
    marker = "  const tick = async () => {"
    assert marker in html, "scan poll structure changed — update this test"
    broken = html.replace(
        marker,
        marker + "\n    if (probe !== 1) { /* read before the const below */ }"
                 "\n    const probe = 1;",
        1,
    )
    scratch = ROOT / ".dashboard-lint-probe.html"
    try:
        scratch.write_text(broken)
        result = subprocess.run(
            ["node", str(LINTER), str(scratch.name)],
            cwd=ROOT, capture_output=True, text=True, timeout=180,
        )
        assert "no-use-before-define" in result.stdout, (
            "linter no longer detects use-before-define:\n" + result.stdout + result.stderr)
    finally:
        scratch.unlink(missing_ok=True)


@requires_node
def test_dashboard_view_logic_behaves():
    """The linter proves the JS parses and has no undefined names. It cannot
    prove that a grouping function groups correctly.

    This runs the shipped `multiGroups` / `renderMultiRows` / `firstOf` —
    extracted from the HTML by brace-matching, not reimplemented — against
    result shapes taken from a real scan.
    """
    result = subprocess.run(
        ["node", str(LOGIC)], cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"dashboard logic failures:\n{result.stdout}{result.stderr}"


@requires_node
def test_the_logic_harness_catches_a_silent_behaviour_change(tmp_path):
    """Guards the guard, same reasoning as the linter probe above.

    Averaging instead of taking the best score is the exact mistake worth
    catching: both orderings look plausible, and the first version of the
    harness used test data where they happened to agree — so it passed
    against a mutated file.
    """
    html = (ROOT / "RaanuTradingBot.html").read_text()
    marker = "const best = g => Math.max(...g.map(r => r.score || 0));"
    assert marker in html, "multiGroups ordering changed — update this test"
    broken = html.replace(
        marker, "const best = g => g.reduce((a,r)=>a+(r.score||0),0)/g.length;", 1)
    scratch = tmp_path / "mutated.html"
    scratch.write_text(broken)
    result = subprocess.run(
        ["node", str(LOGIC), str(scratch)], cwd=ROOT,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode != 0, "harness no longer detects a switch to averaging"
    assert "BEST score" in result.stdout + result.stderr
