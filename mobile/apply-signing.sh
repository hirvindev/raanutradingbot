#!/bin/bash
# Re-applies release signing after `expo prebuild`, which regenerates android/
# from scratch and discards any hand edits. Idempotent.
set -e
KS=$(grep '^NATIVE_KEYSTORE_PASSWORD=' ../.env | cut -d= -f2)
[ -f android/app/raanu-native.keystore ] || cp ../mobile-keystore-backup/raanu-native.keystore android/app/ 2>/dev/null || true
python3 - "$KS" <<'PY'
import sys, pathlib
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
    # Anchor on the buildTypes block, NOT on the first "release {" in the file.
    # The previous regex matched the signingConfigs.release block this script had
    # just inserted, then walked forward to the first signingConfigs.debug — which
    # lives in buildTypes.debug. It patched that one and left buildTypes.release
    # signing with the DEBUG key, which Google Play rejects on upload.
    old_bt = """    buildTypes {
        debug {
            signingConfig signingConfigs.debug
        }
        release {
            // Caution! In production, you need to generate your own keystore file.
            // see https://reactnative.dev/docs/signed-apk-android.
            signingConfig signingConfigs.debug"""
    new_bt = """    buildTypes {
        debug {
            signingConfig signingConfigs.debug
        }
        release {
            signingConfig signingConfigs.release"""
    assert b.count(old_bt) == 1, "buildTypes block not in the expected template state"
    b = b.replace(old_bt, new_bt, 1)
    bg.write_text(b)
print("  signing re-applied")
PY
echo "sdk.dir=$HOME/android-sdk" > android/local.properties
