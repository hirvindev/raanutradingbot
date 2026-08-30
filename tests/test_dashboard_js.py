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
