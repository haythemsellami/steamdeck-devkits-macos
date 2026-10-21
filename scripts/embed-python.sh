#!/usr/bin/env bash
# embed-python.sh — download a pinned python-build-standalone CPython 3.12
# (aarch64-apple-darwin) into app/.runtime/ (gitignored), verify its SHA-256,
# and install it into <app>/Contents/Resources/python/.
#
# Usage: scripts/embed-python.sh [app-path]   (default: app/build/SteamOSDevkit.app)
#
# Idempotent: the tarball is cached, extraction is stamped, and the bundle
# install is skipped when the installed copy already matches the pinned sha.
set -euo pipefail

# --- pinned runtime (update both together, then re-run) ----------------------
PBS_TAG="20261013"
PBS_PYTHON_VERSION="3.12.15"
PBS_ASSET="cpython-${PBS_PYTHON_VERSION}+${PBS_TAG}-aarch64-apple-darwin-install_only.tar.gz"
PBS_SHA256="ba32036ad8f4c434d97a1ee7feef8b7edc0fac16777639b97e39c74a108e4440"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS_ASSET}"
# -----------------------------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="$REPO_ROOT/app/.runtime"
TARBALL="$RUNTIME_DIR/cache/$PBS_ASSET"
EXTRACT_DIR="$RUNTIME_DIR/extract/cpython-${PBS_PYTHON_VERSION}+${PBS_TAG}"
APP_DIR="${1:-$REPO_ROOT/app/build/SteamOSDevkit.app}"
DEST="$APP_DIR/Contents/Resources/python"
STAMP_NAME=".devkit-runtime-sha256"

echo "==> embed-python: CPython $PBS_PYTHON_VERSION ($PBS_TAG) aarch64-apple-darwin"

[[ -d $APP_DIR ]] || {
    echo "error: $APP_DIR does not exist — run scripts/build-app.sh first" >&2
    exit 1
}

mkdir -p "$RUNTIME_DIR/cache" "$RUNTIME_DIR/extract"

verify_tarball() {
    echo "$PBS_SHA256  $TARBALL" | shasum -a 256 --check --status
}

if [[ -f $TARBALL ]] && verify_tarball; then
    echo "==> embed-python: cached tarball verified ($PBS_ASSET)"
else
    echo "==> embed-python: downloading $PBS_URL"
    curl -fL --retry 3 --connect-timeout 20 -o "$TARBALL.tmp" "$PBS_URL"
    mv "$TARBALL.tmp" "$TARBALL"
    if ! verify_tarball; then
        rm -f "$TARBALL"
        echo "error: SHA-256 mismatch for $PBS_ASSET (deleted); expected $PBS_SHA256" >&2
        exit 1
    fi
    echo "==> embed-python: checksum OK ($PBS_SHA256)"
fi

if [[ -x $EXTRACT_DIR/python/bin/python3 && -f $EXTRACT_DIR/.extract-ok ]] \
    && [[ $(cat "$EXTRACT_DIR/.extract-ok") == "$PBS_SHA256" ]]; then
    echo "==> embed-python: reusing verified extraction at $EXTRACT_DIR"
else
    echo "==> embed-python: extracting to $EXTRACT_DIR"
    rm -rf "$EXTRACT_DIR"
    mkdir -p "$EXTRACT_DIR"
    tar -xzf "$TARBALL" -C "$EXTRACT_DIR"
    [[ -x $EXTRACT_DIR/python/bin/python3 ]] || {
        echo "error: unexpected archive layout — $EXTRACT_DIR/python/bin/python3 missing" >&2
        exit 1
    }
    echo "$PBS_SHA256" >"$EXTRACT_DIR/.extract-ok"
fi

if [[ -f $DEST/$STAMP_NAME ]] && [[ $(cat "$DEST/$STAMP_NAME") == "$PBS_SHA256" ]]; then
    echo "==> embed-python: bundle copy already matches pinned sha — skipping install"
else
    echo "==> embed-python: installing into $DEST"
    rm -rf "$DEST.tmp"
    ditto "$EXTRACT_DIR/python" "$DEST.tmp"
    rm -rf "$DEST"
    mv "$DEST.tmp" "$DEST"
    echo "$PBS_SHA256" >"$DEST/$STAMP_NAME"
fi

echo "==> embed-python: smoke test (bundled path, isolated env)"
INSTALLED_VERSION="$(env -i "$DEST/bin/python3" -c 'import sys,platform;print(sys.version.split()[0]);print(platform.machine())')"
echo "${INSTALLED_VERSION//$'\n'/$'\n'    }"
[[ $(echo "$INSTALLED_VERSION" | head -1) == "$PBS_PYTHON_VERSION" ]] || {
    echo "error: bundled python reported unexpected version" >&2
    exit 1
}
echo "==> embed-python: OK"
