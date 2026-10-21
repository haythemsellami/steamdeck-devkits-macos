#!/usr/bin/env bash
# build-app.sh — compile every Swift source under app/Sources/ with swiftc
# (no .xcodeproj) and assemble app/build/SteamOSDevkit.app.
#
# All of app/Sources/**/*.swift is compiled into one module (sorted for
# reproducibility); exactly one of them must declare @main. The script carries
# no payload-specific logic: whichever sources are present are what gets built.
#
# Idempotent: the bundle directory is rebuilt from scratch on every run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_SRC_DIR="$REPO_ROOT/app/Sources"
INFO_PLIST="$REPO_ROOT/app/Info.plist"
BUILD_DIR="$REPO_ROOT/app/build"
APP_NAME="SteamOSDevkit"
APP_DIR="$BUILD_DIR/$APP_NAME.app"
TARGET="arm64-apple-macosx14.0"

echo "==> build-app: assembling $APP_DIR"

command -v swiftc >/dev/null 2>&1 || {
    echo "error: swiftc not found — install Xcode Command Line Tools (xcode-select --install)" >&2
    exit 1
}

[[ -d $APP_SRC_DIR ]] || { echo "error: missing $APP_SRC_DIR" >&2; exit 1; }
[[ -f $INFO_PLIST ]] || { echo "error: missing $INFO_PLIST" >&2; exit 1; }

plutil -lint "$INFO_PLIST"

# The CLT's default SDK can be newer than its swiftc (e.g. swiftc 6.3.x cannot
# read a MacOSX27.0 SDK built by Swift 6.4). Probe candidates — newest first —
# with a real typecheck and use the first compatible one.
choose_sdk() {
    local probe candidates=() sdk
    probe="$(mktemp -t steamosdevkit-sdkprobe).swift"
    echo 'import SwiftUI' >"$probe"
    if [[ -n ${SDK_PATH_OVERRIDE:-} ]]; then
        candidates+=("$SDK_PATH_OVERRIDE")
    fi
    candidates+=("$(xcrun --show-sdk-path 2>/dev/null || true)")
    for sdk in /Library/Developer/CommandLineTools/SDKs/MacOSX*.sdk \
        /Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX*.sdk; do
        candidates+=("$sdk")
    done
    local seen=" " c
    # de-duplicated, newest SDK version first
    local sorted=()
    while IFS= read -r line; do sorted+=("$line"); done \
        < <(printf '%s\n' ${candidates[@]+"${candidates[@]}"} | sed '/^$/d' | sort -uV | sort -rV)
    for c in "${sorted[@]}"; do
        [[ -n $c && -d $c ]] || continue
        [[ $seen == *" $c "* ]] && continue
        seen+="$c "
        if swiftc -swift-version 6 -target "$TARGET" -sdk "$c" -typecheck "$probe" >/dev/null 2>&1; then
            rm -f "$probe"
            echo "$c"
            return 0
        fi
        echo "    build-app: skipping incompatible SDK $c" >&2
    done
    rm -f "$probe"
    return 1
}

SDK_PATH="$(choose_sdk)" || {
    echo "error: no SDK compatible with $(swiftc --version | head -1)" >&2
    exit 1
}
echo "==> build-app: using SDK $SDK_PATH"

SOURCES=()
while IFS= read -r f; do SOURCES+=("$f"); done \
    < <(find "$APP_SRC_DIR" -type f -name '*.swift' | sort)
((${#SOURCES[@]} > 0)) || { echo "error: no Swift sources under $APP_SRC_DIR" >&2; exit 1; }
echo "==> build-app: ${#SOURCES[@]} Swift source(s):"
printf '    %s\n' "${SOURCES[@]#"$REPO_ROOT"/}"

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

echo "==> build-app: compiling (-swift-version 6 -target $TARGET)"
# Single-module build; exactly one source declares @main. -parse-as-library is
# required: with a single source file, swiftc otherwise treats top-level
# statements as the entry point and rejects @main.
swiftc -O \
    -swift-version 6 \
    -parse-as-library \
    -target "$TARGET" \
    -sdk "$SDK_PATH" \
    -module-name "$APP_NAME" \
    -o "$APP_DIR/Contents/MacOS/$APP_NAME" \
    "${SOURCES[@]}"

cp "$INFO_PLIST" "$APP_DIR/Contents/Info.plist"

echo "==> build-app: bundle structure:"
find "$APP_DIR" -type f | sed "s|$BUILD_DIR/||"

echo "==> build-app: OK ($APP_DIR)"
echo "    next: scripts/embed-python.sh && scripts/embed-core.sh && scripts/bundle-rsync.sh && scripts/sign.sh"
