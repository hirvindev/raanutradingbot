"""Every module must import cleanly *on its own*.

This exists because a circular import shipped past a manual check: importing
the modules in one order worked, so the cycle
(``market.prices`` -> ``strategies`` package -> ``strategies.pullback`` ->
``market.prices``) stayed invisible until something imported them the other
way round. Each module here is imported in a **fresh subprocess**, so import
order can never hide a cycle again.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

MODULES = [
    "raanu.config",
    "raanu.paths",
    "raanu.indicators",
    "raanu.state",
    "raanu.state.backends",
    "raanu.market.prices",
    "raanu.market.universe",
    "raanu.market.broker",
    "raanu.strategies",
    "raanu.strategies.pullback",
    "raanu.strategies.breakout",
    "raanu.strategies.leader_dip",
    "raanu.scanning.engine",
    "raanu.scanning.job",
    "raanu.trading.trader",
    "raanu.trading.exits",
    "raanu.trading.sizing",
    "raanu.trading.picks_log",
    "raanu.notify.telegram",
    "raanu.notify.push",
    "raanu.clock",
    "raanu.market.cache",
    "raanu.market.rest",
    "raanu.trading.schedule",
    "raanu.trading.reports",
    "raanu.api.auth",
    "raanu.api.app",
    "raanu.api.routes.health",
    "raanu.api.routes.account",
    "raanu.api.routes.orders",
    "raanu.api.routes.auto",
    "raanu.api.routes.scan",
    "raanu.api.routes.push",
    "raanu.api.routes.picks",
    "raanu.api.routes.strategy",
    "raanu.api.routes.reports",
    "raanu.api.routes.notify",
    "raanu.api.routes.exits",
    "raanu.api.routes.webhooks",
    "raanu.api.routes.stocks",
    "raanu.api.routes.static",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_standalone(module):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"{module} failed to import:\n{result.stderr}"


@pytest.mark.parametrize("module", MODULES)
def test_import_has_no_network_or_state_side_effects(module):
    """Importing a module must not read state or call out to the network.

    The flat codebase imported ``auto_trader`` and thereby read the whole
    trade log from DynamoDB — on every Lambda cold start, for every request,
    including ones that never touched trading.
    """
    probe = f"""
import socket, sys
def _boom(*a, **k):
    raise AssertionError("network access at import time")
socket.socket.connect = _boom
import {module}
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "STATE_BACKEND": "file",
             "DATA_DIR": "/tmp/raanu-import-probe"},
    )
    assert result.returncode == 0, f"{module} has import-time side effects:\n{result.stderr}"
