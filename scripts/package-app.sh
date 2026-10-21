#!/usr/bin/env bash
# package-app.sh — one-shot packaging pipeline for SteamOSDevkit.app:
#   build-app → embed-python → embed-core → bundle-rsync → sign
#
# Idempotent: a content fingerprint of every pipeline input (Swift sources,
# Info.plist, entitlements, uv.lock, pyproject.toml, the pipeline scripts
# themselves, and the rsync source binary) is sealed into the bundle at
# Contents/Resources/.devkit-package-stamp by sign.sh's bundle seal. When the
# fingerprint still matches, all build stages are skipped and only the final
# verification runs — the no-op fast path.
#
# Exits non-zero on any stage or verification failure.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="SteamOSDevkit"
APP_DIR="$REPO_ROOT/app/build/$APP_NAME.app"
STAMP_FILE="$APP_DIR/Contents/Resources/.devkit-package-stamp"
PKG_DIR="$APP_DIR/Contents/Resources/python-packages"
BUNDLE_PYTHON="$APP_DIR/Contents/Resources/python/bin/python3"
BUNDLE_RSYNC="$APP_DIR/Contents/Resources/bin/rsync"

fingerprint() {
    local sources=() f
    while IFS= read -r f; do sources+=("$f"); done \
        < <(find "$REPO_ROOT/app/Sources" -type f -name '*.swift' | sort)
    {
        ((${#sources[@]} > 0)) && cat "${sources[@]}"
        cat "$REPO_ROOT/app/Info.plist" "$REPO_ROOT/app/entitlements.plist" \
            "$REPO_ROOT/uv.lock" "$REPO_ROOT/pyproject.toml" \
            "$REPO_ROOT/scripts/build-app.sh" "$REPO_ROOT/scripts/embed-python.sh" \
            "$REPO_ROOT/scripts/embed-core.sh" "$REPO_ROOT/scripts/bundle-rsync.sh" \
            "$REPO_ROOT/scripts/sign.sh"
        shasum -a 256 "${DEVKIT_RSYNC:-/opt/homebrew/bin/rsync}"
    } | shasum -a 256 | awk '{print $1}'
}

FINGERPRINT="$(fingerprint)"

if [[ -f $STAMP_FILE ]] && [[ $(cat "$STAMP_FILE") == "$FINGERPRINT" ]]; then
    echo "==> package-app: all inputs unchanged — skipping build stages (fast path)"
else
    "$REPO_ROOT/scripts/build-app.sh"
    "$REPO_ROOT/scripts/embed-python.sh"
    "$REPO_ROOT/scripts/embed-core.sh"
    "$REPO_ROOT/scripts/bundle-rsync.sh"
    # Stamp before signing so the fingerprint is covered by the bundle seal.
    mkdir -p "$(dirname "$STAMP_FILE")"
    echo "$FINGERPRINT" >"$STAMP_FILE"
    "$REPO_ROOT/scripts/sign.sh"
fi

echo "==> package-app: ================= verification ================="

echo "--- bundle tree summary"
for component in \
    "Contents/MacOS" \
    "Contents/Resources/python" \
    "Contents/Resources/python-packages" \
    "Contents/Resources/bin" \
    "Contents/Resources/lib"; do
    [[ -d $APP_DIR/$component ]] && du -sh "$APP_DIR/$component" | sed 's/^/    /'
done
du -sh "$APP_DIR" | sed 's/^/    TOTAL /'

echo "--- codesign --verify --deep --strict"
codesign --verify --deep --strict --verbose=2 "$APP_DIR" 2>&1 | sed 's/^/    /'
echo "    codesign verdict: PASS"

echo "--- bundled python imports the embedded core (env -i, bundle paths)"
IMPORT_OUT="$(env -i HOME="$HOME" PATH=/usr/bin:/bin \
    PYTHONPATH="$PKG_DIR" \
    "$BUNDLE_PYTHON" -c "import steamos_devkit, paramiko, pydantic, mcp; from steamos_devkit.cli.mcp import run; from steamos_devkit.mcp_server import create_server; create_server(); print('embedded-core-ok')")"
echo "    $IMPORT_OUT"
[[ $IMPORT_OUT == *embedded-core-ok* ]] || {
    echo "error: bundled-python import check failed" >&2
    exit 1
}

# (No "| head -1": pipefail turns rsync's resulting SIGPIPE into exit 141.)
echo "--- bundled rsync"
RSYNC_VERSION_FULL="$(env -i "$BUNDLE_RSYNC" --version)"
echo "    ${RSYNC_VERSION_FULL%%$'\n'*}"

echo "==> package-app: OK — $APP_DIR is built, embedded, bundled and signed"
