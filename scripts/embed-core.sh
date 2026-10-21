#!/usr/bin/env bash
# embed-core.sh — install the steamos_devkit wheel plus its pinned runtime
# dependencies into <app>/Contents/Resources/python-packages/, targeting the
# embedded CPython runtime installed by scripts/embed-python.sh.
#
# Usage: scripts/embed-core.sh [app-path]   (default: app/build/SteamOSDevkit.app)
#
# Steps:
#   1. uv build --wheel → app/.packaging/dist/
#   2. Pinned deps export: `uv export --no-dev --no-emit-project --locked`
#      (uv 0.11.x; requirements.txt format with hashes is the default). If
#      these flags ever change, the fallback is
#      `uv pip compile pyproject.toml` against the lock.
#   3. `<embedded cache python> -m pip install --no-compile --target
#      python-packages -r requirements-bundle.txt <wheel>` — network access at
#      BUILD time is fine; the installed tree runs fully offline.
#
# Launch-shape contract (the GUI/bridge codes against this): `pip install
# --target` creates NO console scripts, so the app spawns
#   Contents/Resources/python/bin/python3 \
#     -c "from steamos_devkit.cli.mcp import run; run()"
# with PYTHONPATH=Contents/Resources/python-packages and
# DEVKIT_RSYNC=Contents/Resources/bin/rsync.
#
# Idempotent: install is skipped while the stamp (sha256 of uv.lock +
# pyproject.toml + the built wheel) is unchanged; the import verification
# below runs on every invocation regardless.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${1:-$REPO_ROOT/app/build/SteamOSDevkit.app}"
PACKAGING_DIR="$REPO_ROOT/app/.packaging"
DIST_DIR="$PACKAGING_DIR/dist"
REQ_FILE="$PACKAGING_DIR/requirements-bundle.txt"
PKG_DIR="$APP_DIR/Contents/Resources/python-packages"
STAMP_NAME=".devkit-core-sha256"

echo "==> embed-core: target bundle $APP_DIR"

[[ -d $APP_DIR ]] || {
    echo "error: $APP_DIR does not exist — run scripts/build-app.sh first" >&2
    exit 1
}

BUNDLE_PYTHON="$APP_DIR/Contents/Resources/python/bin/python3"
[[ -x $BUNDLE_PYTHON ]] || {
    echo "error: $BUNDLE_PYTHON missing — run scripts/embed-python.sh first" >&2
    exit 1
}

# The embedded runtime's cache copy (extracted by embed-python.sh) runs pip.
# Re-derived from the extraction layout via glob so this script stays runnable
# without duplicating embed-python.sh's pin constants; exactly one extraction
# must exist.
CACHE_PYTHONS=("$REPO_ROOT"/app/.runtime/extract/cpython-*/python/bin/python3)
[[ ${#CACHE_PYTHONS[@]} -eq 1 && -x ${CACHE_PYTHONS[0]} ]] || {
    echo "error: expected exactly one extracted runtime under app/.runtime/extract/" >&2
    echo "       (run scripts/embed-python.sh) — found: ${CACHE_PYTHONS[*]:-none}" >&2
    exit 1
}
CACHE_PYTHON="${CACHE_PYTHONS[0]}"
echo "==> embed-core: pip driver = $CACHE_PYTHON"

mkdir -p "$DIST_DIR"

echo "==> embed-core: building project wheel (uv build --wheel)"
uv build --wheel --out-dir "$DIST_DIR" "$REPO_ROOT"
WHEEL="$(find "$DIST_DIR" -type f -name '*.whl' -exec stat -f '%m %N' {} + | sort -rn | head -1 | cut -d' ' -f2-)"
[[ -f $WHEEL ]] || { echo "error: uv build produced no wheel in $DIST_DIR" >&2; exit 1; }
echo "    wheel: ${WHEEL#"$REPO_ROOT"/}"

echo "==> embed-core: exporting pinned runtime deps (uv export --no-dev --no-emit-project --locked)"
# uv export takes no positional SRC (unlike uv build) — it reads the project
# from the current directory, so run it from the repo root.
(cd "$REPO_ROOT" && uv export --no-dev --no-emit-project --locked --output-file "$REQ_FILE")
echo "    requirements: ${REQ_FILE#"$REPO_ROOT"/} ($(grep -cE '^[a-zA-Z0-9_.-]+==' "$REQ_FILE") pinned packages)"

STAMP="$(cat "$REPO_ROOT/uv.lock" "$REPO_ROOT/pyproject.toml" "$WHEEL" | shasum -a 256 | awk '{print $1}')"

if [[ -f $PKG_DIR/$STAMP_NAME ]] && [[ $(cat "$PKG_DIR/$STAMP_NAME") == "$STAMP" ]]; then
    echo "==> embed-core: python-packages already matches uv.lock+pyproject.toml+wheel — skipping install"
else
    echo "==> embed-core: installing into $PKG_DIR (pip --target, --no-compile)"
    rm -rf "$PKG_DIR"
    mkdir -p "$PKG_DIR"
    # Two invocations: the requirements file pins+hashes every dependency, so
    # pip turns on --require-hashes mode; the just-built local wheel carries no
    # pin in that file and must therefore be installed separately, with
    # --no-deps (its dependency closure IS the requirements file).
    "$CACHE_PYTHON" -m pip install --no-compile --target "$PKG_DIR" -r "$REQ_FILE"
    "$CACHE_PYTHON" -m pip install --no-compile --target "$PKG_DIR" --no-deps "$WHEEL"
    echo "$STAMP" >"$PKG_DIR/$STAMP_NAME"
fi

echo "==> embed-core: verifying embedded core via the BUNDLE python (env -i)"
VERIFY_OUT="$(env -i HOME="$HOME" PATH=/usr/bin:/bin \
    PYTHONPATH="$PKG_DIR" \
    "$BUNDLE_PYTHON" -c "import steamos_devkit, paramiko, pydantic, mcp; from steamos_devkit.cli.mcp import run; from steamos_devkit.mcp_server import create_server; create_server(); print('embedded-core-ok')")"
echo "    $VERIFY_OUT"
[[ $VERIFY_OUT == *embedded-core-ok* ]] || {
    echo "error: embedded-core verification failed" >&2
    exit 1
}

echo "==> embed-core: OK"
