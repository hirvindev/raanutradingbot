"""raanu.api.routes.static"""

from __future__ import annotations

import logging
import os
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


@router.get("/privacy")
def privacy_policy():
    """Public privacy policy — Play Console requires a reachable URL for the
    Data safety declaration, and it must not sit behind the token gate because
    Google's reviewers fetch it without credentials."""
    p = paths.PRIVACY_HTML
    if not p.exists():
        return JSONResponse({"error": "privacy.html not found"}, status_code=404)
    return FileResponse(p, media_type="text/html")


@router.get("/.well-known/assetlinks.json")
def asset_links():
    """Digital Asset Links — proves this site and the Android app are the same owner.

    Without it the TWA still runs but opens with a browser address bar across the
    top, which is the giveaway that it is a wrapped web page rather than an app.
    Chrome fetches this over HTTPS at install and verifies the certificate
    fingerprint of the APK against the list here.

    TWA_SHA256_FINGERPRINT comes from the signing key created at build time, and
    from Play itself once Play App Signing re-signs the upload — those are
    DIFFERENT fingerprints, and both must be listed or the app verifies in
    testing and then shows the address bar in production. Comma-separate them.
    """
    fps = [f.strip().upper() for f in
           os.getenv("TWA_SHA256_FINGERPRINT", "").split(",") if f.strip()]
    pkg = os.getenv("TWA_PACKAGE_NAME", "app.raanu.twa").strip()
    if not fps:
        # Explicit over an empty list: an empty [] looks like a valid answer to
        # Chrome and fails verification silently.
        return JSONResponse(
            {"error": "TWA_SHA256_FINGERPRINT is not set — run the bubblewrap "
                      "build, then set it to the signing key's SHA-256"},
            status_code=503,
        )
    return JSONResponse([{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {"namespace": "android_app",
                   "package_name": pkg,
                   "sha256_cert_fingerprints": fps},
    }])


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
