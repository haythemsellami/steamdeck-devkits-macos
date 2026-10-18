# Audit — Valve steamos-devkit (upstream baseline)

Source: `upstream/steamos-devkit` @ commit `a00ceb7d91ea44a0c3e714a91a06417d6e5cdb33` (2026-10-01 "Code drop"; ChangeLog latest v0.20261001.0). Citations are file:line relative to that root. Audited 2026-10-18.

Scope note: per project direction, existing macOS forks and third-party MCP servers were deliberately **not** used as reference. Upstream is the only baseline.

## 1. Surprises that reshape the plan

1. **The GUI is not GTK.** It is Dear ImGui (pyimgui) on SDL2 + PyOpenGL (`client/devkit_client/gui2/gui2.py:73-76`), ~4000 lines in one file, with a pinned git fork of pyimgui for Python 3.13 compatibility (`pyproject.toml:28-30`). pyimgui/PyOpenGL are effectively unmaintained and OpenGL is deprecated on macOS. Carrying this GUI forward is not viable for a "state of the art" macOS app; a new native GUI is justified by evidence, not preference.
2. **There is no CLI at all.** The only entry point is the GUI launcher (`client/devkit-gui.py`). But the core functions are written in pseudo-CLI style (each takes an `args` namespace), and the GUI synthesizes fake namespaces to call them (`gui2.py:173-190`) — so a CLI/MCP wrapper is mechanically straightforward.
3. **The core is already GUI-free.** `client/devkit_client/__init__.py` (2089 lines) imports only stdlib + paramiko + zeroconf + appdirs + signalslot — no imgui/sdl2. It is importable headless today, with three seams to fix (§6).
4. **No tests, no lint config, no CI test stage.** CI (`.gitlab-ci.yml`) only builds the Linux shiv bundle and Windows cx_Freeze zip. We start quality infrastructure from zero.
5. **SSH is dual-stack by design**: paramiko in-process for all remote commands/SFTP, system `rsync` over system `ssh` for bulk transfer. No sshpass, no password auth anywhere. This maps cleanly onto macOS.
6. **Device-side helpers are uploaded by the client** (`client/devkit-utils/` rsynced to `~/devkit-utils` on every identify, `__init__.py:1919-1938`). The protocol surface is platform-neutral Python over SSH — fully reusable verbatim.

## 2. Client architecture

```
client/devkit-gui.py            # 4-line launcher → devkit_client.gui2.main()
client/devkit_client/
  __init__.py                   # 2089 lines — the non-GUI core (device logic, ssh, rsync, zeroconf)
  gui2/gui2.py                  # 4049 lines — the whole GUI + a localhost REST API
  captured_popen.py, custom_terminal.py, proxy.py, icon.py
client/devkit-utils/            # DEVICE-side scripts (rsynced to ~/devkit-utils on device)
client/devkit-msvsmon/          # VS remote debugger installer (Windows-host-only feature)
setup/                          # shiv (Linux), cx_Freeze (Windows), gitlab CI — no macOS path
pyproject.toml, uv.lock         # uv-managed, [tool.uv] package=false — not pip-installable
```

- Distribution today: shiv `.pyz` (Linux) / cx_Freeze zip (Windows). `devkit_client/version.py` is generated only at packaging time.
- GUI↔core seam: `DevkitCommands` (`gui2.py:125-662`) wraps every core function in a `ThreadPoolExecutor`, returning futures; modal dialogs poll `future.done()`. Self-described in code as "very crufty" (`gui2.py:126-128`).
- A localhost REST API already exists in the GUI: `http.server` on `127.0.0.1:32010` — GET `/selected_devkit`, `/ssh_key_path`, `/title_settings`; POST `/post_event` (build-success → auto-upload webhook) (`gui2.py:3802-3879`). Evidence that programmatic clients of the core are a real use case.

## 3. Protocol: discovery, pairing, transports

**Discovery — mDNS/DNS-SD only** (python-zeroconf, no Avahi):
- Service type `_steamos-devkit._tcp.local.` (`__init__.py:128-134`); TXT `txtvers` must be `b'1'` (`:540-544`); TXT `settings` is JSON (incl. `sshd` flag), `login` carries the SSH user.
- IPv4 only (`socket.inet_ntoa`, `:561,599`).
- Explicit hostname/IP connect exists and is persisted (`gui2.py:1391-1418`, `resolve_machine` `:958-1085`: names containing `.` treated as DNS, else mDNS then address fallback).

**Device HTTP service** (default port **32000/TCP**; 32001 for port-forwarded kits):
- `GET /properties.json` → settings/login; legacy `GET /login-name`.
- `POST /register` — pairing.

**Pairing** (`register()`, `:1088-1125`):
- Key: RSA-2048 generated via paramiko on first use at `appdirs.user_config_dir('steamos-devkit')/devkit_rsa[.pub]` (`:673-700`); public comment `devkit-client:<user>@<host>`.
- POST body: pubkey line + hardcoded shared secret `MAGIC_PHRASE` (`:1088`) — code comments are explicit this is "luck reduction", not real security; device shows a Steam UI approval modal; 30 s timeout; 403 carries JSON errors from device-side `ssh-approve-key` (part of the `steamos-devkit-service` package, not in this repo).
- Host keys are never verified: `AutoAddPolicy`, `StrictHostKeyChecking=no`, `UserKnownHostsFile=/dev/null`.

**Transports** (exact invocations):
- paramiko: `ssh.connect(addr, username=login, pkey=key, timeout=5, look_for_keys=False)`; `exec_command`; SFTP for small files (`:1607-1630`, `:1585-1599`).
- Bulk transfer, system rsync over system ssh (`rsync_transfer`, `:815-902`):
  ```
  rsync -av [-z] --chmod=Du=rwx,Dgo=rx,Fu=rwx,Fog=rx \
        -e "<ssh> [-o UserKnownHostsFile=<path>] -o StrictHostKeyChecking=no -i <keypath>" \
        [--delete --delete-excluded --delete-delay] [--update] [--checksum] [filters] \
        src/ user@ip:dst/
  ```
  `-z` default since v0.20260727.0. No `--partial`/`--inplace`/resume. No dry-run anywhere. Progress parsing is a commented TODO (`--info=progress2`, `:856`). Cancel = `Popen.terminate()` (`:808-813`).
- Interactive shells: system `ssh -t` launched in an external terminal (`:724-737`, `:1485-1563`).

**Ports**: mDNS 5353/UDP; device service 32000/TCP; SSH 22; CEF debug 8080 (probe) / 8081 (console); gdbserver 2345; host ClientAPI 127.0.0.1:32010.

## 4. Device-side operations catalog (→ our MCP tool surface)

All mutation is over SSH as the unprivileged user (`deck` on Deck, `steamos` default on Frame/"Deckard" — detected via `VARIANT_ID=vr` in `/etc/os-release`). No sudo; privileged ops go through polkit helpers. Helpers are invoked as `python3 ~/devkit-utils/<script>`.

| Operation | Remote invocation | Citation |
|---|---|---|
| Device status | `steamos-get-status --json` (rich JSON: os, session, steam_status, branch/version, CEF, renderdoc, launch flags…) | `:1943` |
| List titles | `steamos-list-games` (JSON) | `:1185` |
| Prepare upload | `steamos-prepare-upload --gameid {id} [--restart-steam] [--use-mask-unmask] [--prevent-auto-repair]` → `{user, directory}` | `:1196-1203` |
| Register title with Steam | `steam-client-create-shortcut --parms '<json>'` (gameid, directory, argv, env, settings) | `:1339` |
| Launch title | `steam-devkit-rpc run-game gameid={id}` → Steam pipe RPC | `:2087-2089` |
| Delete titles / reset Steam | `steamos-delete --delete-title X | --delete-all-titles | --reset-steam-client` | `:2022-2034` |
| Steam client mode | `steamos-set-steam-client --client {OS|OS_DEV|SIDELOADED} --gameid {id} [--args] [--gdbserver]` (writes `~/devkit-game/devkit-steam` trampoline) | `:1397-1401` |
| Session control | `holo-session-select` (OS ≥ 3.9) else `steamos-session-select {gamescope|plasma}` | `:1787-1799` |
| Screenshot (Deck) | `DISPLAY=:0 xprop -root -set GAMESCOPECTRL_DEBUG_REQUEST_SCREENSHOT {1-4}`, poll `/tmp/gamescope*.png`, SFTP get | `:1859-1917` |
| Screenshot (Frame) | `deckard-capture --json` | `:1804-1857` |
| Logs | rsync download of Steam logs, `/tmp/dumps`, then Proton `steam-*.log` filtered pull | `:1407-1458` |
| Controller config | `steamos-dump-controller-config [--appid N|--gameid name]` → `/tmp/config_*.vdf` via Steam pipe, rsync pull | `:1989-2020` |
| GPU trace (Deck) | `gpu-trace --capture --no-gpuvis -o {f}` (fallback `--start` + retry), SFTP get | `:1644-1679` |
| RGP capture (Deck) | `touch /tmp/rgp.trigger`, poll `/tmp/*.rgp` (10 s), stability loop (60 s), SFTP get + verify | `:1696-1767` |
| MangoHud | `mangohudctl set no_display …`, `log_session …`; frametimes → `mangoapp_*.csv` rsync pull | `gui2.py:2924-2960` |
| RenderDoc layer | write/remove `ENABLE_VULKAN_RENDERDOC_CAPTURE` in `$XDG_RUNTIME_DIR/steam/env/` | `:1770-1784` |
| RenderDoc replay server | `systemd-run --user --unit=devkit-renderdoc-replay renderdoccmd remoteserver`; stop via systemctl/killall | `:2057-2081` |
| Proton logging | `$XDG_RUNTIME_DIR/steam/env/PROTON_LOG` = `1` or WINEDEBUG string | `gui2.py:3266-3289` |
| CEF console | `touch ~/.steam/steam/.cef-enable-remote-debugging` + session restart → `http://{device}:8081` | `:1570-1582` |
| Set device password | interactive `passwd` via `steamos-set-password.sh` in tethered terminal | `:1566-1567` |
| VS remote debug (Windows host only) | rsync msvsmon tree, sftp `webservices.dll`, run `msvsmoninstall.py` on device | `:1228-1320` |

**Title data model on device** (`~/devkit-game/`):
```
<gameid>/                # uploaded build tree (rsync target)
  steam_appid.txt        # optional forced AppID
<gameid>-argv.json       # [0] = start command (relative to game dir)
<gameid>-env.json        # env dict
<gameid>-settings.json   # launch settings dict
```
- `gameid` must match `^[A-Za-z_][A-Za-z0-9_.]+$` (`gui2.py:91`); `steam`, `steamdeckard`, `steamvr*` are reserved Valve-only sideload names (`:86-88`).
- Settings keys: `steam_play`, `steam_play_debug` (must match Steam's ESteamPlayDebug), `compat_tool` (alias table `:118-126`: proton-stable/experimental, scout/sniper runtimes, lepton, arm64 runtimes), `gdbserver`, plus arbitrary user keys (`:394-429`).
- Steam registration goes through the device-local pipe protocol: `~/.steam/steam.pipe`, session token from `~/.steam/steam.token`, line format `devkit-1 steam://devkit-1/{token}/{cmd}` (`devkit_utils/__init__.py:82-101`); `list-shortcuts` response must be `version == 2` (`resolve.py:52`).
- The device Steam client interprets argv/env/settings at launch — that logic is device-side and not ours to port.

**Versioning/capability model**: discovery `txtvers=1` is the only handshake; everything else is runtime probing (e.g. `holo-session-select` fallback, `gpu-trace` absence via "command not found"). SteamOS-version gates from ChangeLog: polkit helpers ≥ 20220224.100, pairing modal ≥ 20220301, Proton log & RenderDoc ≥ 3.3.2, screenshot modes need gamescope ≥ 3.13, `holo-session-select` ≥ 3.9. Nothing device-side checks the client version.

## 5. Dependencies & licensing

- Project license: **MIT** (Valve + Collabora).
- Python `>=3.11,<3.15`; uv-managed, locked (`uv.lock`). Core-relevant: **paramiko 4.0.0** (LGPL-2.1+), **zeroconf 0.148.0** (LGPL-2.1+), **appdirs 1.4.4** (MIT), **signalslot 0.2.0** (MIT). GUI-only: imgui (BSD), pysdl2(-dll) (CC0/zlib), pyopengl (BSD), xdialog (MIT; supports macOS via AppleScript).
- `ifaddr`/`netifaces-plus` are declared but never imported — stale deps.
- `third-party-licenses/` contains **rsync GPLv3** + cygwin texts because the Windows bundle ships `rsync.exe`/`ssh.exe` — precedent that bundling rsync with attribution is an accepted practice for this codebase.

## 6. macOS friction points (evidence for decisions)

Hard breaks:
1. `_fix_key_permissions` treats every non-Linux OS as Windows and runs `icacls.exe` → crashes first connection on Darwin (`:637-670`). Fix: POSIX chmod 0400 for Darwin.
2. PyOpenGL dependency markers cover only linux/win32 → GUI import dies on Darwin (`pyproject.toml:11-12`). Moot if GUI is replaced.
3. rsync flags assume rsync 3.x; macOS ≥ 15 ships **openrsync** (protocol 29, 2.6.9-compatible) at `/usr/bin/rsync`. `--chmod`, `--delete-excluded`, `--delete-delay`, `-z` are divergent or absent. Confirmed on the dev machine (macOS 26.7.1): `/usr/bin/rsync` = openrsync; rsync 3.5.1 only via Homebrew.
4. Terminal search list is konsole/gnome-terminal/xterm only → Remote Shell / Set Password raise (`:1544-1557`). Needs Terminal.app/iTerm2 (`osascript`) adapter or in-app PTY.
5. `xdg-open` for "Open Folder" doesn't exist (`file_dialogs.py:31-34`) → use `open`.
6. Local Network privacy: mDNS from a packaged app needs `NSLocalNetworkUsageDescription` + `NSBonjourServices` declaring `_steamos-devkit._tcp`; behavior differs across Terminal / app bundle / MCP host contexts (Apple TN3179).

Packaging/behavioral:
7. No macOS packaging path (shiv linux-marked, cx_Freeze win32-marked; `setup/*` assert platform).
8. `ROOT_DIR` resource lookup assumes `devkit-utils/` sits next to the module (`:152-159`) → must redirect into `Contents/Resources` in an app bundle.
9. GUI settings are a **pickle** at hardcoded `~/.devkit-client-gui/settings.pickle` (`gui2.py:3744`) — replace with JSON in the appdirs location; on macOS keys already land at `~/Library/Application Support/steamos-devkit/` via appdirs (correct convention).
10. FileZilla lookup misses `/Applications/FileZilla.app` (`gui2.py:572-581`); gpuvis/RGP/qrenderdoc have no official arm64 macOS builds — scope says capture on device, analysis off-platform.
11. pysdl2-dll ships a universal2 SDL2 dylib — would need re-signing in a notarized bundle (moot with native GUI).

## 7. Gaps we must fill (verified absent upstream)

- **No upload preview/dry-run** (no rsync `-n`/`--itemize` anywhere) — MCP scope requires preview; we add it.
- **No progress reporting** — `--info=progress2` is a TODO comment; we parse it for job progress.
- **No resume** — no `--partial`/`--inplace`; interrupted upload behavior must be designed (see decisions).
- **No stop-title command and no per-title running/readiness query** — only `run-game` exists; observability is Steam-client-level. We must report this honestly in capability results, not fake it.
- **Title stdout/stderr is not streamed** — logs are pulled after the fact from Steam-side files.
- **Job model** — futures + modal polling, no IDs, no persistence, no timeouts; only rsync is cancellable.
- Dead code to not replicate: `DevkitClient.ssh_command` (`:739-806`), game-details store (`:1127-1177`), vestigial `devkit1` TXT property, `ValveSideLoad` stub (sideload path is Valve-internal-only and references constants missing from the public build).

## 8. Design implications

1. **Reuse the core as a library**, not the app: vendor `devkit_client/__init__.py` (+ `proxy.py`, `captured_popen.py`) and `client/devkit-utils/` at a pinned commit; the pseudo-CLI `args`-namespace style makes wrapping cheap.
2. **Three seams to abstract**: (a) zeroconf singleton owned by `DevkitsWindow` → core-owned discovery service; (b) signalslot/future/modal job protocol → real job model (IDs, progress, cancel, timeouts); (c) platform branches → host adapters (key perms, terminal, open-folder, rsync selection, tool discovery, paths).
3. **Replace the GUI outright**; reuse none of gui2 except behavioral reference.
4. **The MCP tool surface maps 1:1 onto §4** — the catalog is complete enough to write tool schemas directly from this audit.
5. **Capability honesty is machine-derivable**: device family (is_deckard), SteamOS version gates, and dependency presence are all probed at runtime by upstream — our capabilities API can be built from these same probes.
