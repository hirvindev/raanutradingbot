#!/bin/bash
# Build the Android app and push it to Play internal testing, in one command.
#
# The server half of this project already deploys itself: Railway builds on
# every push to main. The app did not, and every release meant hand-bumping a
# version code in two files, running gradle with the right JDK, then clicking
# through Play Console. That is where the mistakes came from — a release signed
# with the debug key, a version code that reverted on prebuild, three bundles
# stacked in one release.
#
#   ./deploy-mobile.sh            bump, build, upload to internal testing
#   ./deploy-mobile.sh --build    build only, no upload (prints the .aab path)
#
# One-time setup is in DEPLOY.md — it needs a Play service-account key that
# only the account owner can create.
set -euo pipefail
cd "$(dirname "$0")"

BUILD_ONLY=0
[ "${1:-}" = "--build" ] && BUILD_ONLY=1

# Pinned, not inherited: gradle silently picks whatever JDK is first on PATH,
# and this Mac has 17, 21 and 25 installed. The Android plugin wants 17.
export JAVA_HOME=/opt/homebrew/opt/openjdk@17
export PATH="$JAVA_HOME/bin:$PATH"
export ANDROID_HOME="$HOME/android-sdk"
[ -x "$JAVA_HOME/bin/java" ] || { echo "✗ JDK 17 not at $JAVA_HOME"; exit 1; }

echo "▸ bumping versionCode"
python3 - <<'PY'
import json, pathlib, re
# app.json is the source of truth: `expo prebuild` regenerates android/ from it,
# so a version code living only in build.gradle silently reverts to 1 and Play
# rejects the upload as a duplicate — long after the prebuild that caused it.
app = pathlib.Path("mobile/app.json"); d = json.loads(app.read_text())
cur = int(d["expo"]["android"].get("versionCode", 0))
new = cur + 1
d["expo"]["android"]["versionCode"] = new
app.write_text(json.dumps(d, indent=2) + "\n")

g = pathlib.Path("mobile/android/app/build.gradle"); s = g.read_text()
s2, n = re.subn(r"versionCode \d+", f"versionCode {new}", s, count=1)
assert n == 1, "versionCode not found in build.gradle"
g.write_text(s2)
print(f"  {cur} -> {new}")
PY

VC=$(python3 -c "import json;print(json.load(open('mobile/app.json'))['expo']['android']['versionCode'])")
VN=$(python3 -c "import json;print(json.load(open('mobile/app.json'))['expo']['version'])")

echo "▸ building (this takes a minute or two)"
( cd mobile/android && ./gradlew bundleRelease -q )
AAB="mobile/android/app/build/outputs/bundle/release/app-release.aab"
[ -f "$AAB" ] || { echo "✗ no bundle produced"; exit 1; }

echo "▸ verifying signature"
python3 - "$AAB" "$VC" <<'PY'
# Guards the two failures that actually happened here: a release signed with
# the DEBUG key (Play rejects it, with an error that names the symptom and not
# the cause), and a bundle whose version code did not match what was bumped.
import subprocess, sys, zipfile, os, re
aab, want_vc = sys.argv[1], sys.argv[2]
jt = "/opt/homebrew/opt/openjdk@17/bin/keytool"
ks = "mobile/android/app/raanu-native.keystore"
pw = next(l.split("=", 1)[1].strip() for l in open(".env")
          if l.startswith("NATIVE_KEYSTORE_PASSWORD="))
grab = lambda out: re.search(r"SHA256:\s*([0-9A-F:]+)", out).group(1)
a = grab(subprocess.run([jt, "-printcert", "-jarfile", aab],
                        capture_output=True, text=True).stdout)
k = grab(subprocess.run([jt, "-list", "-v", "-keystore", ks, "-storepass", pw],
                        capture_output=True, text=True).stdout)
assert a == k, f"✗ signed with the WRONG key\n  bundle {a}\n  expected {k}"
raw = zipfile.ZipFile(aab).read("base/manifest/AndroidManifest.xml")
i = raw.find(b"versionCode")
assert f'"{want_vc}"'.strip('"').encode() in raw[i:i+20], "✗ versionCode mismatch"
print(f"  release key ✓   versionCode {want_vc} ✓   {os.path.getsize(aab)//1048576} MB")
PY

cp "$AAB" ~/Desktop/"RaanuBot-v${VN}-vc${VC}.aab"
echo "  → ~/Desktop/RaanuBot-v${VN}-vc${VC}.aab"

if [ "$BUILD_ONLY" = "1" ]; then
  echo "▸ --build given, not uploading"; exit 0
fi

echo "▸ uploading to Play internal testing"
python3 tools/play_upload.py "$AAB" "$VC"
