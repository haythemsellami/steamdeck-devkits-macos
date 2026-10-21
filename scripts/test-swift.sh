#!/usr/bin/env bash
# test-swift.sh — build and run the CLT-only Swift test harness for the GUI
# bridge (no Xcode/SPM/XCTest). Steps: validate fixtures against the real
# pydantic models, probe a compatible SDK, compile app/Sources/Bridge +
# app/Tests (plus app/Sources/App minus the @main entry when present), run.
# Extra arguments pass through to the test binary: `--hardware-only` runs just
# the hardware-gated group (needs DEVKIT_TEST_DEVICE, from a Terminal.app
# session — tmux shells fail LAN contact with errno 65).
#
# Idempotent: app/.build-tests is rebuilt in place every run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$REPO_ROOT/app/.build-tests"
OUT_BIN="$OUT_DIR/run-tests"
TARGET="arm64-apple-macosx14.0"

command -v swiftc >/dev/null 2>&1 || {
    echo "error: swiftc not found — install Xcode Command Line Tools" >&2
    exit 1
}

echo "==> test-swift: validating fixtures against real pydantic models"
(cd "$REPO_ROOT" && uv run python app/Tests/Fixtures/validate.py)

# The CLT's default SDK can be newer than its swiftc (e.g. swiftc 6.3.x cannot
# read a MacOSX27.0 SDK built by Swift 6.4). Probe candidates — newest first —
# with a real typecheck and use the first compatible one.
choose_sdk() {
    local probe candidates=() sdk
    probe="$(mktemp -t devkit-tests-sdkprobe).swift"
    echo 'import Foundation' >"$probe"
    if [[ -n ${SDK_PATH_OVERRIDE:-} ]]; then
        candidates+=("$SDK_PATH_OVERRIDE")
    fi
    candidates+=("$(xcrun --show-sdk-path 2>/dev/null || true)")
    for sdk in /Library/Developer/CommandLineTools/SDKs/MacOSX*.sdk \
        /Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX*.sdk; do
        candidates+=("$sdk")
    done
    local seen=" " c
    local sorted=()
    while IFS= read -r line; do sorted+=("$line"); done \
        < <(printf '%s\n' ${candidates[@]+"${candidates[@]}"} | sed '/^$/d' | sort -uV | sort -rV)
    for c in "${sorted[@]}"; do
        [[ -n $c && -d $c ]] || continue
        [[ $seen == *" $c "* ]] && continue
        seen+="$c "
        if swiftc -target "$TARGET" -sdk "$c" -typecheck "$probe" >/dev/null 2>&1; then
            rm -f "$probe"
            echo "$c"
            return 0
        fi
        echo "    test-swift: skipping incompatible SDK $c" >&2
    done
    rm -f "$probe"
    return 1
}

SDK_PATH="$(choose_sdk)" || {
    echo "error: no SDK compatible with $(swiftc --version | head -1)" >&2
    exit 1
}
echo "==> test-swift: using SDK $SDK_PATH"

mkdir -p "$OUT_DIR"

SOURCES=()
while IFS= read -r f; do SOURCES+=("$f"); done \
    < <(find "$REPO_ROOT/app/Sources/Bridge" -name '*.swift' | sort)
while IFS= read -r f; do SOURCES+=("$f"); done \
    < <(find "$REPO_ROOT/app/Tests" -name '*.swift' | sort)
# The app layer joins the test module once a second agent adds it; its @main
# entry point is excluded so main.swift stays the harness entry.
if [[ -d $REPO_ROOT/app/Sources/App ]]; then
    while IFS= read -r f; do SOURCES+=("$f"); done \
        < <(find "$REPO_ROOT/app/Sources/App" -name '*.swift' ! -name 'SteamOSDevkitApp.swift' | sort)
fi

echo "==> test-swift: compiling ${#SOURCES[@]} sources (-swift-version 6 -target $TARGET)"
swiftc \
    -swift-version 6 \
    -target "$TARGET" \
    -sdk "$SDK_PATH" \
    -module-name BridgeTests \
    -o "$OUT_BIN" \
    ${SOURCES[@]+"${SOURCES[@]}"}

echo "==> test-swift: running $OUT_BIN $*"
"$OUT_BIN" "$@"
