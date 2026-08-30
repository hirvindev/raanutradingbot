"""
raanu.paths — the one place that knows where things live on disk
=================================================================
Before the package restructure, four modules each computed their own
``Path(__file__).parent`` and treated it as "the project root". That worked
only because every module sat at the repo root. Two of those were
load-bearing and would have broken silently once the code moved into a
package:

  * ``server.py`` served privacy.html / sw.js / manifest.webmanifest /
    icons/ / both dashboards relative to its own location
  * ``datadir.py`` decided whether state was persistent by testing for a
    ``.env`` file next to itself

Both now resolve through ``PROJECT_ROOT`` here, so the answer does not
depend on which module happens to be asking.
"""

from __future__ import annotations

from pathlib import Path

# raanu/paths.py -> raanu/ -> project root. On Lambda the whole tree is
# copied to /var/task, so this resolves to /var/task and the static assets
# sit beside it exactly as they do in the repo.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DASHBOARD_HTML = PROJECT_ROOT / "RaanuTradingBot.html"
LEGACY_DASHBOARD_HTML = PROJECT_ROOT / "RaanuTradingBot.legacy.html"
PRIVACY_HTML = PROJECT_ROOT / "privacy.html"
SERVICE_WORKER_JS = PROJECT_ROOT / "sw.js"
WEB_MANIFEST = PROJECT_ROOT / "manifest.webmanifest"
ICONS_DIR = PROJECT_ROOT / "icons"
DOTENV = PROJECT_ROOT / ".env"
