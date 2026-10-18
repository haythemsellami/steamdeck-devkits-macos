# Decisions — finalized open questions

Date: 2026-10-18. Basis: `overview.md` + `docs/audit-upstream.md`. These are decided unless explicitly marked reversible; changing one requires editing this file with the reason.

## D1 — Core: vendor and harden upstream's Python core (build-vs-reuse)

**Decision:** Keep Python. Vendor upstream at a pinned commit — `client/devkit_client/__init__.py`, `proxy.py`, `captured_popen.py`, and the complete `client/devkit-utils/` device-side tree — into our package as the foundation of a `devkit_core` library. Device-side helpers are used **verbatim** (they are the protocol). MIT attribution preserved.

Mechanics:
- `vendor/` snapshot + recorded upstream commit + a `scripts/sync-upstream.sh` that re-fetches and re-applies our patch series. Upstream arrives as squashed "code drops", so a patch-series workflow fits how Valve publishes.
- Our changes land as layered patches on the vendored core, then as wrappers above it: typed API surface, host adapters, job model. Worthwhile fixes (Darwin key perms, terminal adapter) are offered upstream but never block us.
- Do **not** vendor: gui2, msvsmon, gpuvis.exe, Windows/Linux packaging.

Rationale: the audit shows a genuinely GUI-free, pseudo-CLI-shaped core and a platform-neutral device protocol — reuse cost is low, upstream parity is the main long-term asset, and a rewrite buys nothing.

Reversible: yes, at the wrapper boundary — if upstream restructures, the typed API + adapter layer isolates frontends.

## D2 — GUI: native SwiftUI shell over a bundled headless bridge

**Decision:** The GUI is a new **SwiftUI** macOS app. It contains **zero device logic**: it launches a bundled headless bridge process (our own MCP stdio server / JSON-RPC over pipes — the same core, the same typed API) and renders from its structured results. The Python runtime (python-build-standalone, 3.12) and the vendored core ship inside the .app, signed and notarized with hardened runtime; `devkit-utils/` lives in `Contents/Resources`.

Rationale: the upstream ImGui/SDL2/OpenGL GUI is unmaintainable on macOS (audit §1) and the goal is a state-of-the-art Mac app. Out-of-process core gives: the same-core guarantee by construction (GUI, CLI, MCP all speak the one protocol), crash isolation, and a single artifact to sign. Native app gives correct TCC permission identity, Spotlight/Dock integration, and a credible notarization story.

Risk & mitigation: embedding Python in a notarized bundle is the known-hard part. A **packaging spike happens before the GUI build** (milestone M0.5): minimal SwiftUI shell + embedded runtime + rsync call, signed and notarized end-to-end. If that spike fails, the pre-agreed fallback is **PySide6 in-process** packaged with Briefcase — cross-platform, single-language, still notarizable.

Reversible: yes — the bridge protocol is shared with MCP, so swapping the GUI technology does not touch the core.

## D3 — Networking & permissions: explicit-IP is the guaranteed path; discovery is progressive enhancement

**Decision:** Connect-by-hostname/IP is a first-class, always-available flow in every frontend (it is also upstream's persisted feature, `gui2.py:1391-1418`). mDNS discovery via zeroconf ships in all frontends but is treated as enhancement, never as a prerequisite.

Concretely:
- The .app declares `NSLocalNetworkUsageDescription` and `NSBonjourServices` = `_steamos-devkit._tcp` from day one; first-run UX explains why.
- MCP: the server inherits its host app's TCC identity, so a silent discovery failure in a desktop MCP host is expected behavior on macOS, not a bug. MCP tools therefore treat `device` as an explicit parameter (name/IP) with discovery as a helper tool; docs tell users to grant their MCP host Local Network access.
- IPv4-only, matching upstream; IPv6 support is out of scope until upstream's TXT/address handling changes.
- Deliverable test matrix (run per release): discovery + pairing + deploy from (a) Terminal CLI, (b) packaged GUI, (c) real desktop MCP host. Hardware-verified, not mocked, and recorded in the release notes matrix.

Rationale: Apple is tightening LAN privacy (TN3179); designing around permission ambiguity makes the tool deterministic to operate and script.

## D4 — Dependencies: pinned Python via uv; rsync 3.x enforced; key-only SSH

**Decision:**
- **Python 3.11–3.14, uv-managed with a committed lockfile.** Core deps: paramiko, zeroconf, appdirs (replaces none), plus `pydantic`-style typed models for results; CLI via `typer`; MCP via the official Python MCP SDK (FastMCP). Quality: ruff + mypy + pytest from day one (upstream has none).
- **rsync 3.x is a hard requirement.** A `doctor`/capabilities check runs `rsync --version` at startup: openrsync (protocol 29, macOS ≥ 15 system default) is detected and rejected with an actionable message (`brew install rsync`). CLI/MCP: Homebrew rsync. Packaged GUI: **bundle rsync 3.x inside the .app** — GPLv3 attribution and source-offer notices included, following Valve's own precedent of bundling rsync.exe in the Windows zip (audit §5). Bundled path is preferred over PATH when running from a bundle.
- **SSH: paramiko for everything non-interactive; system `ssh` only for interactive shells.** No sshpass, ever. Keys: RSA-2048 (device-side `ssh-approve-key` compatibility is proven upstream); Ed25519 offered later only after verification against device-side pairing. Host-key policy stays compatible with upstream (AutoAdd) in v1, with a trust-on-first-use recording planned so MCP can report device identity honestly.
- Interactive shell on macOS: Terminal.app via `osascript` adapter (iTerm2 detected opportunistically); in-app PTY is a later GUI enhancement.
- **No new dependency is accepted without a license check and a lockfile entry.**

## D5 — State, jobs, results: JSON state in Mac-conventional locations; persistent job store

**Decision:**

State (macOS):
- Root: `~/Library/Application Support/steamos-devkit/` (appdirs convention, matches upstream-on-macOS).
  - `keys/devkit_rsa[.pub]` — mode 0400, POSIX chmod on Darwin (fixes upstream's icacls crash, audit §6.1).
  - `devices.json` — registry: name, address, port, login, pairing date, device family, last-seen.
  - `titles/<gameid>.json` — per-title deploy config (folder, filters, runtime, args).
  - `jobs/<ulid>.jsonl` — job journal (below).
  - `artifacts/<device>/…` — pulled logs, screenshots, traces with a sidecar `meta.json` (job id, device, command, timestamps, sizes, sha256).
- **No pickle.** Migration: if legacy `~/.devkit-client-gui/settings.pickle` or upstream config is found, offer a one-way import.

Jobs:
- Every long operation (upload, log/capture pull, gpu trace, RGP) returns a persistent **ULID job id** immediately and runs on a worker.
- State machine: `queued → running → succeeded | failed | cancelled | interrupted`, with `progress {phase, bytes_done, bytes_total, items_done}` where derivable, timestamps, structured error, and `artifacts[]` references.
- Progress: parse rsync `--info=progress2` (upstream TODO `:856` — we implement it) plus per-phase events from the device helpers.
- Cancellation: terminate the child process group; rsync gets `--partial` so a resumed upload continues rather than restarts. Cleanup semantics are defined per operation in the spec (e.g. RGP trigger file removed on cancel).
- Recovery: on startup, any journal left in `queued|running` is marked `interrupted`; uploads are resumable, captures are re-runnable, never silently retried.
- Timeouts: per-operation defaults with explicit override; distinct from cancellation in results.

Results: all core APIs return typed, JSON-serializable result objects (status, device/title identity, errors with codes, exit codes where a remote command ran, artifact refs). Upload completion, title registration, process launch, and app readiness are **separate fields**, per the overview — noting that upstream offers no readiness signal (audit §7), so readiness is reported as `unknown` until we build a device-side probe; we never fake it.

## Stack summary (locked)

| Layer | Choice |
|---|---|
| Language | Python 3.11–3.14 (core), Swift (GUI) |
| Core origin | Vendored upstream @ pinned commit + patch series |
| CLI | typer, JSON output, meaningful exit codes |
| MCP | official Python SDK (FastMCP), stdio transport |
| GUI | SwiftUI ↔ bundled MCP-bridge process |
| Env/deps | uv + lockfile; Homebrew rsync 3.x (bundled in .app) |
| Quality | ruff, mypy, pytest, hardware test matrix |
| Packaging | signed + notarized .app (Developer ID), spike before GUI build |
