"""raanu.api.routes.static"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from raanu import paths

log = logging.getLogger("raanu.api.routes.static")

router = APIRouter()


# ---------- STATIC ----------
@router.get("/kite")
def kite_alias():
    """The prototype now IS the dashboard. Kept so existing /kite links land
    somewhere sensible instead of 404ing."""
    return RedirectResponse("/", status_code=307)


# GET /privacy and GET /.well-known/assetlinks.json were removed on
# 31 Aug 2026 with the Android apps.
#
# privacy.html existed for the Play Console Data safety declaration;
# assetlinks.json was Digital Asset Links, which proved the site and the TWA
# shared an owner so the wrapper opened without a browser address bar.
# Neither has a consumer now, and assetlinks in particular was a route that
# 503'd on every request because TWA_SHA256_FINGERPRINT was never set.


# ---------- PWA ----------
# These sit outside /api/ so they are not behind the token gate: a service
# worker and a manifest must be fetchable before the user has authenticated,
# or the app cannot install and the unlock screen itself would not render
# offline.
@router.get("/manifest.webmanifest")
def pwa_manifest():
    p = paths.WEB_MANIFEST
    if not p.exists():
        return JSONResponse({"error": "manifest not found"}, status_code=404)
    return FileResponse(p, media_type="application/manifest+json")


@router.get("/sw.js")
def service_worker():
    """Served from the ROOT path on purpose.

    A service worker may only control pages at or below its own path, so one
    served from /static/sw.js could not intercept "/". Cache-Control: no-cache
    lets browsers pick up a new worker without waiting out a cached copy.
    """
    p = paths.SERVICE_WORKER_JS
    if not p.exists():
        return JSONResponse({"error": "sw.js not found"}, status_code=404)
    return FileResponse(p, media_type="application/javascript",
                        headers={"Cache-Control": "no-cache",
                                 "Service-Worker-Allowed": "/"})


@router.get("/icons/{name}")
def pwa_icon(name: str):
    # Basename only — an icon path is never a reason to walk the filesystem.
    p = (paths.ICONS_DIR / Path(name).name)
    if p.suffix.lower() != ".png" or not p.exists():
        return JSONResponse({"error": "icon not found"}, status_code=404)
    return FileResponse(p, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})


@router.get("/legacy")
def legacy_dashboard():
    """The previous dark dashboard. Retained because it still has controls the
    new one does not: editable exit-rule inputs, the PIN gate, per-signal
    Execute, the equity chart and the S2/S3 scan streams. Reach for it when you
    need one of those; everything else lives at /."""
    p = paths.LEGACY_DASHBOARD_HTML
    if not p.exists():
        return JSONResponse({"error": "RaanuTradingBot.legacy.html not found."}, status_code=404)
    return FileResponse(p)


@router.get("/")
def root():
    html_path = paths.DASHBOARD_HTML
    if not html_path.exists():
        return JSONResponse({"error": "RaanuTradingBot.html not found."}, status_code=404)
    return FileResponse(html_path)
