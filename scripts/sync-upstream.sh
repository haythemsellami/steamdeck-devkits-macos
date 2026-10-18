#!/usr/bin/env bash
# sync-upstream.sh [upstream-ref]
#
# Re-vendor Valve's steamos-devkit client core into src/steamos_devkit/_vendor/
# and apply the local patch series from patches/ in order (docs/decisions.md D1).
#
# With no argument, the ref pinned in _vendor/UPSTREAM_COMMIT is re-applied.
# With an argument (branch, tag, or sha), that ref is vendored and becomes the
# new pin. The script is idempotent: every run starts from a pristine copy of
# the upstream ref and reapplies the full patch series.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM_DIR="$REPO_ROOT/upstream/steamos-devkit"
UPSTREAM_URL="https://gitlab.steamos.cloud/devkit/steamos-devkit.git"
VENDOR_DIR="$REPO_ROOT/src/steamos_devkit/_vendor"
PATCHES_DIR="$REPO_ROOT/patches"

log() { printf 'sync-upstream: %s\n' "$*" >&2; }

if [[ $# -gt 1 ]]; then
    echo "usage: $0 [upstream-ref]" >&2
    exit 2
fi

# --- resolve the ref to vendor --------------------------------------------
if [[ $# -eq 1 ]]; then
    REQUESTED_REF="$1"
elif [[ -f "$VENDOR_DIR/UPSTREAM_COMMIT" ]]; then
    REQUESTED_REF="$(tr -d '[:space:]' < "$VENDOR_DIR/UPSTREAM_COMMIT")"
else
    log "no ref given and $VENDOR_DIR/UPSTREAM_COMMIT is missing"
    exit 2
fi

# --- fetch upstream --------------------------------------------------------
if [[ ! -d "$UPSTREAM_DIR/.git" ]]; then
    log "cloning $UPSTREAM_URL into upstream/steamos-devkit (gitignored, audit-only)"
    git clone --quiet "$UPSTREAM_URL" "$UPSTREAM_DIR"
fi
log "fetching upstream"
git -C "$UPSTREAM_DIR" fetch --quiet origin

RESOLVED_SHA="$(git -C "$UPSTREAM_DIR" rev-parse "$REQUESTED_REF^{commit}")"
log "vendoring $RESOLVED_SHA"
git -C "$UPSTREAM_DIR" checkout --quiet --detach "$RESOLVED_SHA"

# --- pristine re-copy ------------------------------------------------------
rm -rf "$VENDOR_DIR/devkit_client" "$VENDOR_DIR/devkit-utils"
mkdir -p "$VENDOR_DIR/devkit_client"
cp -p "$UPSTREAM_DIR/client/devkit_client/__init__.py" \
      "$UPSTREAM_DIR/client/devkit_client/proxy.py" \
      "$UPSTREAM_DIR/client/devkit_client/captured_popen.py" \
      "$UPSTREAM_DIR/client/devkit_client/custom_terminal.py" \
      "$VENDOR_DIR/devkit_client/"
cp -R -p "$UPSTREAM_DIR/client/devkit-utils" "$VENDOR_DIR/devkit-utils"
printf '%s\n' "$RESOLVED_SHA" > "$VENDOR_DIR/UPSTREAM_COMMIT"

# --- apply the patch series ------------------------------------------------
# Patch paths are relative to _vendor/ with a/ and b/ prefixes (see
# patches/README.md), so they apply from inside the vendor directory.
shopt -s nullglob
PATCHES=("$PATCHES_DIR"/*.patch)
if (( ${#PATCHES[@]} > 0 )); then
    for patch in "${PATCHES[@]}"; do
        log "applying $(basename "$patch")"
        git -C "$VENDOR_DIR" apply --whitespace=nowarn "$patch"
    done
else
    log "no patches to apply"
fi

log "done: _vendor now at $RESOLVED_SHA"
