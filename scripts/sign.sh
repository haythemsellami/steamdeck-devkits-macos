#!/usr/bin/env bash
# sign.sh — code-sign DevkitSpike.app inside-out with the hardened runtime:
#   nested dylibs/extensions → helper executables (python3, rsync) → bundle.
#
# Identity: the first "Developer ID Application" from the keychain. When none
# exists the script falls back to an AD-HOC signature (-) so the pipeline is
# still exercisable end-to-end — but such a build can never be notarized.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$REPO_ROOT/app/build/DevkitSpike.app"
ENTITLEMENTS="$REPO_ROOT/app/entitlements.plist"

[[ -d $APP_DIR ]] || {
    echo "error: $APP_DIR does not exist — run scripts/build-app.sh first" >&2
    exit 1
}
[[ -f $ENTITLEMENTS ]] || { echo "error: missing $ENTITLEMENTS" >&2; exit 1; }

IDENTITY_HASH="$(security find-identity -v -p codesigning \
    | awk -F'"' '$2 ~ /^Developer ID Application:/ {print $1}' \
    | awk '{print $2}' \
    | head -1)"

if [[ -n $IDENTITY_HASH ]]; then
    IDENTITY_DESC="$(security find-identity -v -p codesigning | awk -F'"' -v h="$IDENTITY_HASH" '$1 ~ h {print $2; exit}')"
    echo "==> sign: using Developer ID identity: $IDENTITY_DESC"
    TIMESTAMP_ARGS=(--timestamp)
else
    IDENTITY_HASH="-"
    TIMESTAMP_ARGS=(--timestamp=none)
    echo "==================================================================" >&2
    echo "  WARNING: no 'Developer ID Application' identity in keychain."     >&2
    echo "  Falling back to an AD-HOC signature. The resulting app:"           >&2
    echo "    - runs on this machine, but"                                     >&2
    echo "    - will FAIL Gatekeeper (spctl) assessment, and"                  >&2
    echo "    - CANNOT be notarized."                                          >&2
    echo "  Create a Developer ID certificate (Xcode → Settings → Accounts →"  >&2
    echo "  Manage Certificates…), then re-run this script."                   >&2
    echo "==================================================================" >&2
fi

is_macho() {
    local magic
    magic="$(head -c 4 "$1" | od -An -tx1 | tr -d ' \n')"
    case $magic in
        feedface | feedfacf | cefaedfe | cffaedfe | cafebabe | cafebabf) return 0 ;;
        *) return 1 ;;
    esac
}

is_executable_macho() {
    # otool -h renders the filetype column numerically (2 = MH_EXECUTE) on
    # newer CLTs and textually (EXECUTE / MH_EXECUTE) on others — accept all.
    otool -h "$1" 2>/dev/null | awk '
        /^[[:space:]]*(0x[0-9a-fA-F]+|MH_)/ {
            if ($5 == "2" || $5 == "EXECUTE" || $5 == "MH_EXECUTE") found = 1
        }
        END { exit !found }'
}

sign_one() {
    local path=$1 use_entitlements=$2
    local args=(--sign "$IDENTITY_HASH" --force --options runtime "${TIMESTAMP_ARGS[@]}")
    if [[ $use_entitlements == yes ]]; then
        args+=(--entitlements "$ENTITLEMENTS")
    fi
    codesign "${args[@]}" "$path"
    echo "    signed: ${path#"$APP_DIR"/}"
}

echo "==> sign: nested code under Contents/Resources (inside-out)"
while IFS= read -r f; do
    if is_executable_macho "$f"; then
        # Executables that may load third-party dylibs get the entitlements.
        sign_one "$f" yes
    else
        sign_one "$f" no
    fi
done < <(find "$APP_DIR/Contents/Resources" -type f ! -name '*.plist' | sort -r | while read -r f; do
    is_macho "$f" && echo "$f"
done)

echo "==> sign: main executable + bundle seal"
sign_one "$APP_DIR" yes

echo "==> sign: verifying (codesign --verify --deep --strict)"
codesign --verify --deep --strict --verbose=2 "$APP_DIR" 2>&1 | sed 's/^/    /'

echo "==> sign: embedded entitlements of main executable:"
codesign -d --entitlements - "$APP_DIR" 2>&1 | sed 's/^/    /'

echo "==> sign: Gatekeeper assessment (spctl -a -vv):"
if spctl -a -vv "$APP_DIR" 2>&1 | sed 's/^/    /'; then
    echo "==> sign: spctl accepted the app"
else
    echo "    (expected to fail for ad-hoc signatures and before notarization)"
fi

echo "==> sign: OK"
