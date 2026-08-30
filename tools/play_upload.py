#!/usr/bin/env python3
"""Upload an .aab to Google Play internal testing.

Called by deploy-mobile.sh. Needs a Play service-account key at
$PLAY_SERVICE_ACCOUNT_JSON (default ~/.secrets/play-service-account.json) —
see DEPLAY setup in DEPLOY.md; only the account owner can create it.

Deliberately targets the INTERNAL track and nothing else. Production is a
decision with a 12-tester/14-day gate behind it and real regulatory questions
about a public stock-signal app; it must never be something a deploy script can
do by accident.
"""
import os
import sys
from pathlib import Path

PACKAGE = "app.raanu.mobile"
TRACK = "internal"
KEY = Path(os.getenv("PLAY_SERVICE_ACCOUNT_JSON",
                     Path.home() / ".secrets" / "play-service-account.json"))


def main(aab: str, version_code: str) -> int:
    if not KEY.exists():
        print(f"✗ no Play service-account key at {KEY}\n"
              f"  One-time setup — see DEPLOY.md. Until then the build is on "
              f"your Desktop and can be uploaded by hand.")
        return 2

    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = service_account.Credentials.from_service_account_file(
        str(KEY), scopes=["https://www.googleapis.com/auth/androidpublisher"])
    api = build("androidpublisher", "v3", credentials=creds,
                cache_discovery=False)

    edits = api.edits()
    edit_id = edits.insert(body={}, packageName=PACKAGE).execute()["id"]

    up = edits.bundles().upload(
        packageName=PACKAGE, editId=edit_id,
        media_body=MediaFileUpload(aab, mimetype="application/octet-stream",
                                   resumable=True)).execute()
    uploaded_vc = up["versionCode"]
    print(f"  uploaded versionCode {uploaded_vc}")

    # ONE version code in the release. Uploading a bundle adds to the release
    # rather than replacing it, so a release can end up holding several — Play
    # then serves only the highest and errors on the rest as "completely
    # shadowed". Setting the list explicitly makes that impossible.
    edits.tracks().update(
        packageName=PACKAGE, editId=edit_id, track=TRACK,
        body={"track": TRACK,
              "releases": [{"versionCodes": [str(uploaded_vc)],
                            "status": "completed"}]}).execute()

    edits.commit(packageName=PACKAGE, editId=edit_id).execute()
    print(f"  ✓ live on the {TRACK} track — testers get it in a few minutes")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: play_upload.py <path-to-aab> <version-code>")
        sys.exit(1)
    try:
        sys.exit(main(sys.argv[1], sys.argv[2]))
    except Exception as e:
        # Never fail silently: the build succeeded and is on the Desktop, so
        # say so rather than leaving it looking like nothing happened.
        print(f"✗ upload failed: {type(e).__name__}: {e}")
        print("  The bundle is still on your Desktop and can be uploaded by hand.")
        sys.exit(1)
