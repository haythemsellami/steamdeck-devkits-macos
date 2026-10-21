#!/usr/bin/env bash
# notarize.sh — submit SteamOSDevkit.app to Apple's notary service via
# `xcrun notarytool` using the keychain profile "devkit-notarize", then staple
# the ticket. If the profile is missing, print exact setup instructions and
# exit 2.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$REPO_ROOT/app/build/SteamOSDevkit.app"
ZIP_PATH="$REPO_ROOT/app/build/DevkitSpike-notarize.zip"
PROFILE="${NOTARY_PROFILE:-devkit-notarize}"

print_instructions() {
    cat >&2 <<'EOF'
======================================================================
 NOTARIZATION BLOCKED — missing credentials on this machine.

 1) Developer ID signing certificate (one time, needs a paid Apple
    Developer account):
      - Xcode → Settings → Accounts → add your Apple ID → select the
        team → "Manage Certificates…" → "+" → "Developer ID Application"
      - Verify: security find-identity -v -p codesigning \
                | grep "Developer ID Application"

 2) notarytool keychain profile named "devkit-notarize" (one time) —
    pick ONE of:

    a) App Store Connect API key (recommended; CI-friendly):
       - Create a key at
         https://appstoreconnect.apple.com/access/integrations/api
         (Team Keys; the "Developer" role is sufficient for notarization).
       - Download AuthKey_<KEY_ID>.p8, note the Key ID and Issuer ID.
       - Register the profile:
           xcrun notarytool store-credentials devkit-notarize \
               --key-path ~/path/to/AuthKey_<KEY_ID>.p8 \
               --key-id <KEY_ID> \
               --issuer <ISSUER_ID_UUID> \
               --validate

    b) App-specific password:
       - Generate one at https://appleid.apple.com → Sign-In and
         Security → App-Specific Passwords.
       - Register the profile:
           xcrun notarytool store-credentials devkit-notarize \
               --apple-id <you@example.com> \
               --team-id <TEAM_ID> \
               --password <app-specific-password> \
               --validate
       (Find your Team ID at https://developer.apple.com/account →
        Membership.)

 3) Then re-run:
       scripts/sign.sh && scripts/notarize.sh
======================================================================
EOF
}

[[ -d $APP_DIR ]] || {
    echo "error: $APP_DIR does not exist — run scripts/build-app.sh first" >&2
    exit 1
}

echo "==> notarize: checking keychain profile '$PROFILE'"
PROFILE_OK=0
# Local probe (no network): notarytool stores a generic-password item with
# service com.apple.gke.notary.tool and account = profile name.
if security find-generic-password -s "com.apple.gke.notary.tool" -a "$PROFILE" -w >/dev/null 2>&1; then
    PROFILE_OK=1
elif xcrun notarytool history --keychain-profile "$PROFILE" >/dev/null 2>&1; then
    # Authoritative fallback in case the keychain item shape changes.
    PROFILE_OK=1
fi

if [[ $PROFILE_OK -eq 0 ]]; then
    echo "==> notarize: keychain profile '$PROFILE' not found" >&2
    print_instructions
    exit 2
fi

if ! security find-identity -v -p codesigning | grep -q "Developer ID Application"; then
    echo "==> notarize: no 'Developer ID Application' identity — the app cannot" >&2
    echo "    have been properly signed, so notarization would be rejected." >&2
    print_instructions
    exit 2
fi

echo "==> notarize: creating submission zip"
rm -f "$ZIP_PATH"
ditto -c -k --sequesterRsrc --keepParent "$APP_DIR" "$ZIP_PATH"

echo "==> notarize: submitting to Apple (this can take minutes)…"
xcrun notarytool submit "$ZIP_PATH" --keychain-profile "$PROFILE" --wait

echo "==> notarize: stapling ticket"
xcrun stapler staple "$APP_DIR"
xcrun stapler validate "$APP_DIR"

echo "==> notarize: Gatekeeper assessment:"
spctl -a -vv "$APP_DIR"

echo "==> notarize: OK — $APP_DIR is signed, notarized, and stapled"
