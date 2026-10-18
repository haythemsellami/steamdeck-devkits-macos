# M0.5 Packaging/Notarization Spike — Results

Date: 2026-10-19. Machine: macOS 26.7.1 (25G241), arm64, Xcode CLT only (Apple Swift 6.3.3), Homebrew rsync 3.5.1 at `/opt/homebrew/bin/rsync`. **No Apple Developer credentials on this machine.**

Purpose: prove decision D2's riskiest claim — a SwiftUI app embedding a Python runtime and a bundled rsync 3.x, code-signed with hardened runtime, ready for notarization.

## What exists

| Path | Role |
|---|---|
| `app/Sources/DevkitSpike.swift` | Minimal SwiftUI shell: one window ("Devkit bridge spike"), button 1 runs the bundled Python and shows stdout, button 2 runs the bundled rsync `--version` and shows its first line. `@main`, built for macOS 14+, arm64. |
| `app/Info.plist` | Bundle id `dev.steamosdevkit.mac`, `NSLocalNetworkUsageDescription`, `NSBonjourServices` = `_steamos-devkit._tcp`, `LSMinimumSystemVersion` 14.0. |
| `app/entitlements.plist` | `com.apple.security.cs.disable-library-validation` (required for the embedded runtimes' dylibs). |
| `scripts/build-app.sh` | Compiles with `swiftc` (no .xcodeproj), assembles `app/build/DevkitSpike.app`. Probes for an SDK compatible with the installed swiftc (see "Findings"). |
| `scripts/embed-python.sh` | Downloads pinned python-build-standalone CPython **3.12.15+20261013** (`aarch64-apple-darwin`, install_only), verifies SHA-256 `ba32036ad8f4c434d97a1ee7feef8b7edc0fac16777639b97e39c74a108e4440`, installs to `Contents/Resources/python/`. Idempotent (cache + stamps). |
| `scripts/bundle-rsync.sh` | Copies Homebrew rsync to `Contents/Resources/bin/rsync`, walks the full non-system dylib closure with `otool -L` (**8 dylibs**, incl. transitive `libunistring`/`libintl`), copies to `Contents/Resources/lib/`, rewrites everything to `@rpath` (+ `@executable_path/../lib` rpath). Verifies the bundled binary runs `rsync 3.x` under `env -i`. |
| `scripts/sign.sh` | Inside-out hardened-runtime signing of every Mach-O file (21 total) → main executable + bundle seal. Entitlements on all executables. Developer ID if present, else ad-hoc with a loud warning. |
| `scripts/notarize.sh` | Zips, submits via `xcrun notarytool --keychain-profile devkit-notarize --wait`, staples. Prints exact credential setup and exits 2 when the profile is absent. |

## Verification matrix (actually run on this machine)

| Claim | Result | Evidence |
|---|---|---|
| SwiftUI app builds with `swiftc`, no .xcodeproj | ✅ | `scripts/build-app.sh` → `DevkitSpike.app/Contents/MacOS/DevkitSpike` (Mach-O 64-bit arm64, `minos 14.0`, SDK 26.5) |
| App launches | ✅ | process ran 4 s with a live runloop in a shell context (`pid 71083`); window interaction not exercised headlessly |
| Bundled Python runs from bundle path | ✅ | `env -i Contents/Resources/python/bin/python3 -c …` → `3.12.15 (main, Oct 14 2026 …) [Clang 23.1.3 ]` / `arm64` |
| Bundled rsync runs from bundle path and is real rsync 3.x | ✅ | `env -i Contents/Resources/bin/rsync --version` → `rsync  version 3.5.1  protocol version 33` (not openrsync) |
| Bundled rsync does real work | ✅ | `rsync -a --delete -z` of an 8 MiB random file, `cmp` clean |
| Bundled Python spawning bundled rsync (the app's runtime shape) | ✅ | subprocess run → exit 0, first line `rsync  version 3.5.1 …` |
| Whole bundle signature validity | ✅ | `codesign --verify --deep --strict` → `valid on disk`, `satisfies its Designated Requirement`; sweep: 21/21 Mach-O files signed |
| Hardened runtime enabled | ✅ | `codesign -dv` → `flags=0x10002(adhoc,runtime)` |
| Entitlements embedded | ✅ | main executable and helper executables carry `com.apple.security.cs.disable-library-validation = true` |
| Gatekeeper assessment | ❌ expected | `spctl -a -vv` → `rejected` (exit 3). **Ad-hoc signatures can never pass Gatekeeper.** Requires Developer ID signature + notarized, stapled ticket. |
| Notarization | ⛔ blocked on credentials | `scripts/notarize.sh` exits 2 with setup instructions (keychain profile `devkit-notarize` absent; 0 signing identities in keychain) |

Reproduce: `scripts/build-app.sh && scripts/embed-python.sh && scripts/bundle-rsync.sh && scripts/sign.sh` (then `scripts/notarize.sh` once credentials exist). All scripts are idempotent and `shellcheck`-clean.

## Findings that shaped the implementation

1. **CLT/SDK skew on this machine:** CLT swiftc 6.3.3 cannot read its own MacOSX27.0 SDK (built by Swift 6.4). `build-app.sh` typecheck-probes the installed SDKs newest-first and uses the first compatible one (here: MacOSX26.5.sdk). Override with `SDK_PATH_OVERRIDE=…`.
2. **Ad-hoc + hardened runtime + bundled dylibs:** an ad-hoc-signed hardened-runtime process may **not** load ad-hoc-signed dylibs (dyld: "different Team IDs" — each ad-hoc cdhash is its own identity). The `disable-library-validation` entitlement on every bundled executable is therefore load-bearing even for local dev runs, not just for the Developer ID build. The spike caught this; `sign.sh` signs executables with entitlements (filetype detection handles `otool -h` printing `2` vs `EXECUTE`).
3. **python-build-standalone is mostly static:** `python3.12` links only system libraries at startup (libpython is statically linked in); only 3 `.so` extensions + Tcl/Tk dylibs need signing. `ssl` works (OpenSSL 3.5.9 statically linked).
4. **rsync closure is bigger than the direct deps:** `libidn2` pulls `libunistring` + `libintl`; total 8 dylibs. The BFS walk in `bundle-rsync.sh` handles this generically — no hardcoded list.
5. **SIGPIPE vs `set -o pipefail`:** `rsync --version | head -1` kills rsync with 141; capture-then-truncate instead.

## Blocked on user credentials (exact remaining steps)

1. **Developer ID Application certificate** (paid Apple Developer account):
   Xcode → Settings → Accounts → add Apple ID → select team → Manage Certificates… → "+" → "Developer ID Application".
   Verify: `security find-identity -v -p codesigning | grep "Developer ID Application"`.
2. **notarytool keychain profile `devkit-notarize`** — one of:
   - *App Store Connect API key (recommended):* create at <https://appstoreconnect.apple.com/access/integrations/api> (Team Keys; Developer role suffices for notarization) →
     `xcrun notarytool store-credentials devkit-notarize --key-path ~/path/AuthKey_<KEY_ID>.p8 --key-id <KEY_ID> --issuer <ISSUER_ID_UUID> --validate`
   - *App-specific password:* <https://appleid.apple.com> → Sign-In and Security → App-Specific Passwords →
     `xcrun notarytool store-credentials devkit-notarize --apple-id <you@example.com> --team-id <TEAM_ID> --password <app-specific-password> --validate`
3. Then: `scripts/sign.sh && scripts/notarize.sh` — success = `spctl -a -vv` prints `accepted … source=Notarized Developer ID`. That is the M0.5 exit check (fresh-user-account validation still requires a second machine/VM; not available here).

## Deviations & follow-ups

- Spec §2.1 lists a single `scripts/notarize-spike.sh`; the spike delivered five composable scripts instead (build/embed/bundle/sign/notarize). **Spec layout should be updated to match.**
- Bundle id `dev.steamosdevkit.mac` is the spec's placeholder — replace with a controlled domain before real distribution.
- D4 requires rsync GPLv3 attribution + source-offer notice in distributed bundles; not added in the spike (no distribution yet). Required before M8.
- The spike app is throwaway (D2); M7's real GUI reuses only the pipeline scripts, not the source.
- `app/.runtime/`, `app/build/`, `app/.signing/` are gitignored; no signing material is ever committed.
