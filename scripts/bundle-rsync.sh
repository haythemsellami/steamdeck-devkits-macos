#!/usr/bin/env bash
# bundle-rsync.sh — copy the Homebrew rsync 3.x binary into
# <app>/Contents/Resources/bin/, walk its non-system dylib closure with otool,
# copy those into Contents/Resources/lib/, and rewrite all load commands to
# @rpath so the binary runs without Homebrew present.
#
# Usage: scripts/bundle-rsync.sh [app-path]   (default: app/build/SteamOSDevkit.app)
#
# The payload is always rebuilt from the pristine Homebrew originals, so the
# script is idempotent (re-running never rewrites an already-rewritten file).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${1:-$REPO_ROOT/app/build/SteamOSDevkit.app}"
RSYNC_SRC="${DEVKIT_RSYNC:-/opt/homebrew/bin/rsync}"
BIN_DIR="$APP_DIR/Contents/Resources/bin"
LIB_DIR="$APP_DIR/Contents/Resources/lib"

echo "==> bundle-rsync: source = $RSYNC_SRC"

[[ -d $APP_DIR ]] || {
    echo "error: $APP_DIR does not exist — run scripts/build-app.sh first" >&2
    exit 1
}
[[ -x $RSYNC_SRC ]] || {
    echo "error: rsync not found at $RSYNC_SRC — brew install rsync" >&2
    exit 1
}

# Hard requirement (D4): real rsync 3.x, never openrsync.
# (No "| head -1" here: pipefail turns rsync's resulting SIGPIPE into exit 141.)
SRC_VERSION_FULL="$("$RSYNC_SRC" --version)"
SRC_VERSION_LINE="${SRC_VERSION_FULL%%$'\n'*}"
echo "    $SRC_VERSION_LINE"
if [[ $SRC_VERSION_LINE == *openrsync* ]]; then
    echo "error: $RSYNC_SRC is openrsync — brew install rsync" >&2
    exit 1
fi
if [[ ! $SRC_VERSION_LINE =~ ^rsync[[:space:]]+version[[:space:]]+3\. ]]; then
    echo "error: $RSYNC_SRC is not rsync 3.x" >&2
    exit 1
fi

is_system_dep() {
    case $1 in
        /usr/lib/* | /System/*) return 0 ;;
        *) return 1 ;;
    esac
}

# Non-system dependency paths recorded in a Mach-O file (own id excluded).
deps_of() {
    local file=$1 self_id
    self_id="$(otool -D "$file" 2>/dev/null | tail -n +2 | head -1 || true)"
    otool -L "$file" | tail -n +2 | awk '{print $1}' | while read -r dep; do
        [[ -n $dep && $dep != "$self_id" ]] || continue
        is_system_dep "$dep" || echo "$dep"
    done
}

echo "==> bundle-rsync: computing dylib closure"
declare -a CLOSURE=()
declare -a QUEUE=("$RSYNC_SRC")
while ((${#QUEUE[@]} > 0)); do
    CURRENT="${QUEUE[0]}"
    QUEUE=("${QUEUE[@]:1}")
    while read -r dep; do
        [[ -n $dep ]] || continue
        [[ -f $dep ]] || { echo "error: dependency $dep missing on disk" >&2; exit 1; }
        already=0
        for seen in ${CLOSURE[@]+"${CLOSURE[@]}"}; do [[ $seen == "$dep" ]] && already=1; done
        if [[ $already -eq 0 ]]; then
            CLOSURE+=("$dep")
            QUEUE+=("$dep")
        fi
    done < <(deps_of "$CURRENT")
done
echo "    non-system dylibs to bundle: ${#CLOSURE[@]}"
printf '      %s\n' ${CLOSURE[@]+"${CLOSURE[@]}"}

echo "==> bundle-rsync: installing into bundle (bin/ + lib/)"
rm -rf "$BIN_DIR" "$LIB_DIR"
mkdir -p "$BIN_DIR" "$LIB_DIR"
cp -L "$RSYNC_SRC" "$BIN_DIR/rsync"
for dep in ${CLOSURE[@]+"${CLOSURE[@]}"}; do
    cp -L "$dep" "$LIB_DIR/$(basename "$dep")"
done

rewrite() {
    local target=$1
    chmod u+w "$target"
    while read -r dep; do
        [[ -n $dep ]] || continue
        install_name_tool -change "$dep" "@rpath/$(basename "$dep")" "$target"
    done < <(deps_of "$target")
}

echo "==> bundle-rsync: rewriting load commands to @rpath"
for lib in "$LIB_DIR"/*.dylib; do
    install_name_tool -id "@rpath/$(basename "$lib")" "$lib"
    rewrite "$lib"
done
rewrite "$BIN_DIR/rsync"
# rsync lives in Contents/Resources/bin → the dylibs sit at ../lib relative to it.
install_name_tool -add_rpath "@executable_path/../lib" "$BIN_DIR/rsync"

# install_name_tool invalidates signatures; ad-hoc re-sign so the payload is
# runnable on arm64 before sign.sh applies the real hardened-runtime signature.
echo "==> bundle-rsync: ad-hoc re-signing modified Mach-O files"
for lib in "$LIB_DIR"/*.dylib; do
    codesign --sign - --force "$lib"
done
codesign --sign - --force "$BIN_DIR/rsync"

echo "==> bundle-rsync: bundled rsync linkage now:"
otool -L "$BIN_DIR/rsync" | sed 's/^/    /'

echo "==> bundle-rsync: verifying execution with a scrubbed environment (env -i)"
BUNDLED_VERSION_FULL="$(env -i "$BIN_DIR/rsync" --version)"
BUNDLED_VERSION_LINE="${BUNDLED_VERSION_FULL%%$'\n'*}"
echo "    $BUNDLED_VERSION_LINE"
if [[ ! $BUNDLED_VERSION_LINE =~ ^rsync[[:space:]]+version[[:space:]]+3\. ]] \
    || [[ $BUNDLED_VERSION_LINE == *openrsync* ]]; then
    echo "error: bundled rsync failed verification: $BUNDLED_VERSION_LINE" >&2
    exit 1
fi

echo "==> bundle-rsync: OK"
