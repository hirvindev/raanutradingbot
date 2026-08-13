#!/bin/bash
# Re-applies release signing after `expo prebuild`, which regenerates android/
# from scratch and discards any hand edits. Idempotent.
set -e
KS=$(grep '^NATIVE_KEYSTORE_PASSWORD=' ../.env | cut -d= -f2)
[ -f android/app/raanu-native.keystore ] || cp ../mobile-keystore-backup/raanu-native.keystore android/app/ 2>/dev/null || true
python3 - "$KS" <<'PY'
import sys, pathlib, re
ks = sys.argv[1]
gp = pathlib.Path("android/gradle.properties"); s = gp.read_text()
if "RAANU_STORE_FILE" not in s:
    s += (f"\nRAANU_STORE_FILE=raanu-native.keystore\nRAANU_KEY_ALIAS=raanunative\n"
          f"RAANU_STORE_PASSWORD={ks}\nRAANU_KEY_PASSWORD={ks}\n")
    gp.write_text(s)
bg = pathlib.Path("android/app/build.gradle"); b = bg.read_text()
if "RAANU_STORE_FILE" not in b:
    b = b.replace("""        debug {
            storeFile file('debug.keystore')""",
"""        release {
            if (project.hasProperty('RAANU_STORE_FILE')) {
                storeFile file(RAANU_STORE_FILE)
                storePassword RAANU_STORE_PASSWORD
                keyAlias RAANU_KEY_ALIAS
                keyPassword RAANU_KEY_PASSWORD
            }
        }
        debug {
            storeFile file('debug.keystore')""", 1)
    b = re.sub(r"(release \{\n(?:.*\n)*?\s*)signingConfig signingConfigs\.debug",
               r"\1signingConfig signingConfigs.release", b, count=1)
    bg.write_text(b)
print("  signing re-applied")
PY
echo "sdk.dir=$HOME/android-sdk" > android/local.properties
