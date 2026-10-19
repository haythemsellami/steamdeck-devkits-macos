# Technical Specification — SteamOS Devkit for macOS

Status: ratified for implementation. Date: 2026-10-19.
Basis: `overview.md` (product direction), `docs/audit-upstream.md` (protocol evidence), `docs/decisions.md` (D1–D5).

This document is the coding-sprint contract: module boundaries, data models, CLI and MCP surfaces, job semantics, packaging, and the milestone plan. Citations like `:846` refer to `upstream/steamos-devkit/client/devkit_client/__init__.py` at the vendored commit.

## 1. Naming and identifiers

| Thing | Value |
|---|---|
| Python package | `steamos_devkit` |
| Python distribution | `steamos-devkit-mac` |
| CLI command | `devkit` |
| MCP server | `devkit mcp` (stdio), also installed as `devkit-mcp` console script |
| GUI display name | `SteamOS Devkit` |
| GUI bundle id | `dev.steamosdevkit.mac` (placeholder — replace with a domain we control before signing) |
| Vendored upstream root | `src/steamos_devkit/_vendor/` |
| State root (macOS) | `~/Library/Application Support/steamos-devkit/` |
| Branding | TBD; all user-facing strings route through one constants module |

## 2. Architecture

```
┌─────────────┐   ┌─────────────┐   ┌─────────────────────────┐
│    CLI      │   │  MCP server │   │  GUI (SwiftUI, later)   │
│  (typer)    │   │  (FastMCP)  │   │  MCP client over stdio  │
└──────┬──────┘   └──────┬──────┘   └───────────┬─────────────┘
       └─────────────────┴──────────────────────┘
                         │  one typed API
                ┌────────▼─────────┐
                │   DevkitCore     │  models, jobs, validation, results
                └────────┬─────────┘
        ┌────────────────┼─────────────────┐
┌───────▼───────┐ ┌──────▼──────┐ ┌────────▼────────┐
│ HostAdapter   │ │ Job runner  │ │ Vendored core   │
│ (mac/linux/   │ │ + journal   │ │ (upstream)      │
│  windows)     │ │             │ │ + devkit-utils  │
└───────────────┘ └─────────────┘ └────────┬────────┘
                                           │ paramiko / rsync / zeroconf
                                    SteamOS device
```

Rules (from overview §"one shared implementation"):

- CLI, MCP, and GUI contain **no** device logic; they call `DevkitCore` methods and render `Result` objects.
- MCP and CLI import no GUI or graphics dependencies. The GUI talks to the core exclusively as an MCP client (`devkit mcp` stdio subprocess).
- All core methods take typed models and return typed results; none print, raise bare exceptions across the boundary, or read process-global state.
- Device-side helpers (`devkit-utils/`) are used verbatim from the vendored snapshot and uploaded to `~/devkit-utils` on identify, exactly as upstream `:1919-1938`.

### 2.1 Repository layout

```
pyproject.toml            # uv project, deps, ruff/mypy/pytest config
uv.lock
src/steamos_devkit/
  __init__.py
  version.py              # generated at build; source runs report "0.0.0-dev"
  constants.py            # branding, ports, regexes, timeouts
  errors.py               # §6
  models.py               # §5 pydantic models
  config.py               # state root, settings, migration
  core.py                 # DevkitCore facade
  discovery.py            # core-owned zeroconf service
  pairing.py              # keys + register flow
  device.py               # DeviceHandle: resolve/connect/status/capabilities
  transfer.py             # rsync builder, preview, upload, download
  titles.py               # list/inspect/configure/register/launch/delete
  diagnostics.py          # logs/screenshot/controller-config/device info
  profiling.py            # gpu-trace, RGP, mangohud, renderdoc, proton logs
  session.py              # steam mode, session select, reload steam
  jobs/
    __init__.py
    store.py              # JSONL journal, ULID ids, recovery sweep
    runner.py             # thread pool, cancellation, timeouts
    progress.py           # rsync --info=progress2 parser
  adapters/
    base.py               # HostAdapter protocol
    macos.py
    linux.py
    windows.py            # best-effort; not a release gate
  cli.py                  # typer app
  mcp_server.py           # FastMCP tools/resources
  _vendor/
    UPSTREAM_COMMIT       # full upstream sha
    devkit_client/        # __init__.py, proxy.py, captured_popen.py, custom_terminal.py
    devkit-utils/         # device-side tree, verbatim
scripts/
  sync-upstream.sh        # re-fetch + re-apply patch series
  run-gates.sh            # ruff + mypy + pytest --cov, same as CI
  build-app.sh            # M0.5 spike: swiftc .app build
  embed-python.sh         # M0.5 spike: pinned python-build-standalone into the bundle
  bundle-rsync.sh         # M0.5 spike: rsync 3.x + dylib closure into the bundle
  sign.sh                 # M0.5 spike: hardened-runtime signing (Developer ID or ad-hoc)
  notarize.sh             # M0.5 spike: notarytool submission via keychain profile
patches/                  # quilt-style series applied by sync-upstream.sh
tests/
  unit/
  integration/            # mocked SSH
  hardware/               # marked, skipped without DEVKIT_TEST_DEVICE
app/                      # SwiftUI Xcode project (M7)
docs/
  overview.md is at repo root; this file; audit; decisions
```

### 2.2 Vendoring mechanics (D1)

- `_vendor/UPSTREAM_COMMIT` pins the snapshot; `scripts/sync-upstream.sh` fetches that ref (or a newer one when asked), replaces `_vendor/`, and applies `patches/*.patch` in order.
- Patch series is kept minimal; current anticipated patches: none at sprint start — all Darwin fixes land in `adapters/macos.py` and wrapper code first. A patch is created only when a fix cannot live outside the vendored file (example candidate: `_fix_key_permissions` icacls crash, `:637-670` — handled instead by never calling it and doing chmod 0400 in the adapter).
- `LICENSE` attribution: MIT notice from upstream copied into `LICENSES/upstream-steamos-devkit-MIT.txt`; bundling rsync notices (GPLv3 + source offer) added at packaging time, following Valve's Windows-bundle precedent.

## 3. State layout (D5)

```
~/Library/Application Support/steamos-devkit/
  settings.json                 # app-level prefs (replaces upstream pickle)
  keys/devkit_rsa{,.pub}        # mode 0400, written with O_EXCL
  devices.json                  # Device registry (paired + added-by-IP)
  last_device                   # bare name, CLI default selector
  titles/<gameid>.json          # per-title deploy config
  jobs/<job_ulid>.jsonl         # job journals
  artifacts/<device>/<artifact-id>/…   # payload + meta.json
  logs/devkit-YYYYMMDD.log      # client's own structured logs
```

- `DEVKIT_STATE_DIR` env var overrides the root (used by tests and CI).
- All files JSON, UTF-8, one object per file except journals (one event per line).
- Migration: if `~/.devkit-client-gui/settings.pickle` or upstream `~/.config/steamos-devkit/` exist and our state root is empty, import keys and device list; never delete the source. Pickle is parsed read-only behind a `--migrate` CLI command, never automatically loaded (pickle = code execution risk).
- Config on Linux keeps upstream paths (`~/.config/steamos-devkit/`) so dual-use machines stay compatible.

## 4. Protocol constants (from audit, frozen here)

- mDNS service `_steamos-devkit._tcp.local.`, TXT `txtvers=1`, IPv4 only.
- Device HTTP service default port 32000 (32001 supported for port-forwarded kits); `GET /properties.json`, legacy `GET /login-name`, `POST /register`.
- SSH port 22; users: `deck` (Deck), `steamos` (Frame). No sudo, no passwords, key-only.
- CEF debug 8080 (probe) / 8081 (console); gdbserver 2345.
- gameid validation: `^[A-Za-z_][A-Za-z0-9_.]+$`; reserved: `steam`, `steamdeckard`, `steamvr`, `steamvrdeckard` (Valve-only; rejected for deploy with `UNSUPPORTED`).
- Device title root: `~/devkit-game/<gameid>/` with sidecars `<gameid>-argv.json`, `<gameid>-env.json`, `<gameid>-settings.json`.
- compat_tool aliases (upstream `:118-126`): `proton-stable`, `proton-experimental`, `SteamLinuxRuntime` (scout), `SteamLinuxRuntime_sniper`, `lepton`, `SteamLinuxRuntime_4-arm64`, `SteamLinuxRuntime_4`.
- Screenshot modes (gamescope ≥ 3.13): `1`=baseplane, `2`=all real layers, `3`=full composition, `4`=screen buffer.

## 5. Data models (`models.py`, pydantic v2)

All models serialize to JSON; field names snake_case; timestamps ISO-8601 UTC. IDs: `job_<ulid>`, `art_<ulid>` (ULID via `ulid-py`).

```python
DeviceSelector = str  # registry name, IPv4 literal, or DNS hostname; CLI defaults to last_device
GameId = constr(pattern=r"^[A-Za-z_][A-Za-z0-9_.]+$")

class DiscoveredDevice(BaseModel):
    name: str; address: IPv4Address; port: int = 32000
    login: str | None; sshd: bool | None; via: Literal["mdns", "explicit"]

class Device(BaseModel):  # registry entry
    name: str; address: str; port: int = 32000; login: str
    family: Literal["deck", "frame", "unknown"] = "unknown"
    paired_at: datetime | None; added_via: Literal["mdns", "explicit"]
    last_seen: datetime | None

class DeviceStatus(BaseModel):  # parsed steamos-get-status --json (audit §4)
    hostname: str; os_name: str; os_version: str; is_deckard: bool
    session_status: str                       # device sends str(enum), e.g. "SessionConfig.GAMESCOPE"
    steam_status: Literal["NOT_RUNNING","ERROR","OS","OS_DEV","SIDELOADED","UNKNOWN"]
        # device sends "SteamStatus.OS" etc. — parser strips the enum prefix (validator)
    steam_osclient_branch: str | None         # prefix-stripped by device: "stable", not "steamdeck_stable"
    steam_osclient_version: int | str | None  # int on Deck; raw builddate string on Frame
    cef_debugging_enabled: bool; renderdoc_layer_enabled: bool
    renderdoc_replay_server_running: bool; user_password_is_set: bool
    steam_default_args: list[str]; steam_current_args: list[str]   # device emits lists
    steam_launch_flags: dict[str, str]
    raw: dict  # unparsed extras preserved

class Title(BaseModel):          # from steamos-list-games + sidecars
    gameid: GameId; registered_with_steam: bool
    size_bytes: int | None; modified: datetime | None

class TitleConfig(BaseModel):
    entrypoint: str | None = None        # argv[0], relative to game dir
    args: list[str] = []                 # appended to argv
    env: dict[str, str] = {}
    runtime: CompatToolAlias | None = None
    settings: dict[str, str] = {}        # steam_play, gdbserver, user keys…

class FilterSpec(BaseModel):
    mode: Literal["exclude", "include", "raw"] = "exclude"
    patterns: list[str] = []

class DeploySpec(BaseModel):
    device: DeviceSelector; gameid: GameId; source_dir: Path
    filters: FilterSpec = FilterSpec()
    clean: bool = False          # --delete --delete-excluded --delete-delay
    checksum: bool = False       # --checksum (mutually exclusive with update)
    update: bool = False         # --update
    compress: bool = True        # -z
    register: bool = True        # run steam-client-create-shortcut after upload
    launch: bool = False         # run-game after successful register
    config: TitleConfig | None = None    # applied before register

class DeployPreview(BaseModel):  # from rsync -n --itemize-changes
    files_added: int; files_changed: int; files_deleted: int
    bytes_to_transfer: int; sample_changes: list[str]  # ≤ 200 itemized lines
    warnings: list[str]

class Artifact(BaseModel):
    id: str; job_id: str; device: str
    kind: Literal["logs","proton_log","screenshot","controller_config","gpu_trace","rgp","frametimes","dumps"]
    path: Path; bytes: int; sha256: str; media_type: str | None
    remote_source: str; created_at: datetime

class LaunchResult(BaseModel):
    gameid: GameId; launched: bool; device_response: str
    readiness: Literal["unknown"] = "unknown"   # no upstream readiness signal (audit §7)

class Capabilities(BaseModel):   # §9
    host: HostCapabilities; device: DeviceCapabilities | None
    operations: dict[str, OperationCapability]

class OperationCapability(BaseModel):
    status: Literal["supported_tested","supported_untested","dependency_required",
                    "capture_export_only","unavailable_host","unavailable_device",
                    "unsupported_upstream"]
    reason: str | None = None; dependency: str | None = None
```

Result envelope for every core call that does not start a job:

```python
class Result(BaseModel):         # T-specific payloads subclass this
    ok: bool; code: ErrorCode | None   # None when ok
    device: Device | None; message: str | None
    # deploy distinguishes, per overview:
    upload_complete: bool | None = None
    registration_complete: bool | None = None
    launch: LaunchResult | None = None
```

## 6. Error model (`errors.py`)

```python
class ErrorCode(StrEnum):
    DEVICE_NOT_FOUND; DEVICE_UNREACHABLE; AMBIGUOUS_DEVICE
    PAIRING_DECLINED; PAIRING_TIMEOUT; PAIRING_FAILED
    DEPENDENCY_MISSING; OPENRSYNC_DETECTED; PERMISSION_LOCAL_NETWORK; PERMISSION_KEY_FILE
    VALIDATION; UNSUPPORTED; REMOTE_COMMAND_FAILED
    TIMEOUT; CANCELLED; JOB_NOT_FOUND; ARTIFACT_NOT_FOUND; STATE_CORRUPT
```

`DevkitError(code, message, details: dict = {}, remote_exit_code: int | None)` — the only exception crossing the core boundary. Frontends map it to CLI exit codes / MCP `isError` payloads. Keys, passphrases, and the pairing magic phrase never appear in messages, details, logs, or tool results.

## 7. Jobs (`jobs/`)

### 7.1 Job record and journal

Journal = append-only JSONL at `jobs/<job_ulid>.jsonl`; one event per line:

```json
{"t":"created","ts":"…","job":{"id":"job_…","kind":"deploy","device":"deck","gameid":"…","spec":{…},"timeout_s":3600,"created_at":"…"}}
{"t":"started","ts":"…","pid":1234}
{"t":"progress","ts":"…","phase":"transferring","bytes_done":…,"bytes_total":…,"items_done":…,"rate_bps":…}
{"t":"artifact","ts":"…","artifact_id":"art_…"}
{"t":"succeeded","ts":"…","result":{…}}
```

Terminal events: `succeeded | failed | cancelled | interrupted | timeout`. State machine: `queued → running → (terminal)`. Cancellation from `queued` removes the job without running.

### 7.2 Runner

- One `ThreadPoolExecutor(max_workers=4)` per process; jobs also runnable synchronously by the CLI (`--wait` default for CLI).
- `JobHandle.cancel()`: SIGTERM to the child **process group**, escalate to SIGKILL after 5 s, then run the per-kind cleanup hook (§7.4), then journal `cancelled`.
- Timeout: per-kind defaults (below), overridable per call; expiry = SIGTERM + journal `timeout`.
- Recovery: at process start, any journal whose last event is `created|started|progress` is marked `interrupted`. Uploads are resumable (rsync `--partial`); captures are re-runnable. Nothing auto-retries.

### 7.3 Progress

rsync runs with `--info=progress2` (implementing upstream's TODO `:856`). Parser extracts overall percent, bytes, rate from `progress2` lines and per-file lines for `items_done`. Deploy phases: `preparing → transferring → registering → launching? → done`. SFTP pulls report bytes from the known remote size (fetched via `stat` first). MCP exposes progress through `notifications/progress` when the caller supplies a `progressToken`; CLI renders a TTY bar or JSON progress events.

### 7.4 Cleanup hooks (on cancel/timeout)

| Job kind | Cleanup |
|---|---|
| deploy | kill rsync; remote partial files kept for resume (`--partial`) |
| fetch_logs | kill rsync; local partial dir removed, no artifact registered |
| screenshot | `rm -f /tmp/gamescope*.png` remotely; local partial deleted |
| gpu_trace | `rm -f` remote temp zip |
| rgp | `rm -f /tmp/*.rgp` remotely |
| controller_config | `rm -f /tmp/config_*.tmp /tmp/config_*.vdf` remotely |

### 7.5 Default timeouts

discover 5 s · pair 120 s (device approval may be slow) · status 15 s · deploy 3600 s · fetch_logs 600 s · screenshot 60 s · gpu_trace 300 s · rgp 180 s · controller_config 60 s.

## 8. Core API (`core.py` — the single typed surface)

```python
class DevkitCore:
    def __init__(self, state_dir: Path | None = None, adapter: HostAdapter | None = None)

    # discovery & registry
    def discover(self, timeout_s: float = 5.0) -> list[DiscoveredDevice]
    def devices(self) -> list[Device]
    def forget_device(self, sel: DeviceSelector) -> Result   # local registry only; device untouched
    def pair(self, sel: DeviceSelector, timeout_s: int = 120) -> Result
    def status(self, sel: DeviceSelector) -> DeviceStatus
    def capabilities(self, sel: DeviceSelector | None) -> Capabilities  # None → host-only
    def doctor(self) -> DoctorReport

    # titles
    def list_titles(self, sel) -> list[Title]
    def inspect_title(self, sel, gameid) -> TitleDetail   # incl. parsed argv/env/settings
    def configure_title(self, sel, gameid, config: TitleConfig) -> Result
    def preview_deploy(self, sel, spec: DeploySpec) -> DeployPreview
    def deploy(self, sel, spec: DeploySpec, timeout_s: int = 3600) -> JobHandle
    def register_title(self, sel, gameid) -> Result
    def launch_title(self, sel, gameid) -> LaunchResult
    def delete_title(self, sel, gameid, confirm: Literal[True]) -> Result  # destructive, explicit

    # diagnostics (all job-returning)
    def fetch_logs(self, sel, kinds: list[LogKind] = ["steam"], timeout_s=600) -> JobHandle
    def screenshot(self, sel, mode: Literal[1,2,3,4] = 3, timeout_s=60) -> JobHandle
    def controller_config(self, sel, gameid, timeout_s=60) -> JobHandle

    # profiling
    def gpu_trace(self, sel, timeout_s=300) -> JobHandle
    def rgp_capture(self, sel, timeout_s=180) -> JobHandle
    def set_perf_overlay(self, sel, enabled: bool) -> Result
    def set_frametime_logging(self, sel, enabled: bool) -> Result  # artifacts via fetch_logs(kinds=["frametimes"])
    def set_renderdoc_layer(self, sel, enabled: bool) -> Result
    def renderdoc_server(self, sel, action: Literal["start","stop","status"]) -> Result
    def set_proton_logging(self, sel, mode: Literal["off","on"] , winedebug: str | None = None) -> Result

    # session & steam
    def set_steam_mode(self, sel, mode: Literal["OS","OS_DEV"], gameid=None, args=None, gdbserver=False) -> Result
    def set_session(self, sel, session: Literal["gamescope","plasma-x11","plasma-wayland"]) -> Result
    def reload_steam(self, sel) -> Result

    # jobs & artifacts
    def job(self, job_id) -> JobInfo
    def jobs(self, device=None, state=None) -> list[JobInfo]
    def cancel(self, job_id) -> Result
    def artifacts(self, device=None, kind=None) -> list[Artifact]
    def artifact_path(self, artifact_id) -> Path
    def read_artifact_excerpt(self, artifact_id, tail_lines: int = 200) -> str  # ≤ 2000 lines / 64 KiB
```

`LogKind = Literal["steam","proton","dumps","frametimes"]`.

Device selector resolution order: exact registry name → IPv4 literal → hostname containing `.` → mDNS lookup (discover-capable contexts only) → `DEVICE_NOT_FOUND` / `AMBIGUOUS_DEVICE`.

Deploy pipeline (audit §3): `steamos-prepare-upload` → rsync (`:815-902` flags, plus our `--info=progress2 --partial`) → `steam-client-create-shortcut` when `register` → `steam-devkit-rpc run-game` when `launch`. Preview = rsync `-n --itemize-changes` only; it never calls `prepare-upload` (no device mutation in preview); remote dir assumed `~/devkit-game/<gameid>/`.

## 9. Capabilities (honest reporting)

Built from probes, never hardcoded:

- Host probes: rsync path/flavor/version (protocol ≥ 30 required; `openrsync` string → `OPENRSYNC_DETECTED`), ssh path/version, terminal emulator availability, platform/arch, GUI-bundle vs CLI context, Local Network heuristic (`unknown | likely_ok | blocked` — TCC is not programmatically readable; we infer from an mDNS probe).
- Device probes (from `steamos-get-status` + feature checks): family (`is_deckard`), os_version, steam branch/version, `gpu-trace` presence, gamescope version (screenshot modes), renderdoc layer/server state, CEF flag, mangohudctl presence, `holo-session-select` vs `steamos-session-select`.
- Every MCP tool and CLI command maps to one `operations` entry. Rules:
  - `supported_tested` only for operations recorded green in the current hardware matrix (§13); otherwise `supported_untested`.
  - Windows-host-only upstream features (msvsmon deploy) → `unavailable_host`.
  - Steam Frame-only vs Deck-only differences → `unavailable_device` with reason.
  - Things upstream cannot do (stop-title, launch readiness, title stdout streaming) → `unsupported_upstream`, with the reason string. **We never fake these.**

## 10. CLI (`cli.py`, typer)

Global flags: `--json` (auto-default when stdout is not a TTY), `--timeout`, `--state-dir`, `--verbose`. Long ops wait by default; `--no-wait` prints the job id and exits 0; `devkit jobs wait <id>` blocks and exits with the job's outcome code.

Selector convention (wave-3 standardization): every device command takes an OPTIONAL `<sel>` positional falling back to the last-device marker (`VALIDATION`, exit 2, when neither exists); `deploy` takes `--device/-d` instead (its two trailing positionals make an optional middle one ambiguous). The synopsis below shows the explicit `<sel>` forms.

```
devkit doctor
devkit discover [--timeout 5]
devkit devices list | forget <sel>
devkit pair <sel> [--timeout 120]
devkit status <sel>
devkit capabilities [sel]
devkit titles list|inspect <sel> [gameid]
devkit titles configure <sel> <gameid> [--entrypoint …] [--args …] [--env K=V]… [--runtime …] [--set K=V]…
devkit titles register <sel> <gameid>
devkit titles delete <sel> <gameid> --confirm
devkit deploy <sel> <gameid> <source_dir> [--preview] [--clean] [--checksum|--update]
              [--no-compress] [--filter-mode … --filter …] [--no-register] [--launch] [--no-wait]
devkit launch <sel> <gameid>
devkit logs <sel> [--kind steam|proton|dumps|frametimes]… [--tail 200]
devkit screenshot <sel> [--mode 1..4]
devkit controller-config <sel> <gameid>
devkit gpu-trace <sel>
devkit rgp <sel>
devkit perf-overlay <sel> on|off
devkit frametimes <sel> on|off
devkit renderdoc layer <sel> on|off
devkit renderdoc server <sel> start|stop|status
devkit proton-log <sel> on|off [--winedebug …]
devkit steam-mode <sel> os|os-dev [--gameid …] [--args …] [--gdbserver]
devkit session <sel> gamescope|plasma-x11|plasma-wayland
devkit reload-steam <sel>
devkit shell <sel>                          # interactive, Terminal.app adapter
devkit jobs list|status|wait|cancel
devkit artifacts list|get <artifact-id>
devkit mcp                                  # stdio server (GUI + desktop hosts)
devkit migrate                              # one-way import of legacy upstream state
devkit version
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | unexpected error |
| 2 | usage/validation |
| 3 | device not found / unreachable |
| 4 | pairing declined/failed |
| 5 | dependency problem (incl. openrsync) |
| 6 | local permission problem |
| 7 | remote operation failed / job failed |
| 8 | timeout |
| 9 | cancelled |
| 10 | unsupported capability |

`--json` output shape: the `Result`/model JSON, one object, nothing else on stdout; human text on stderr only.

## 11. MCP surface (`mcp_server.py`, FastMCP, stdio)

Tool names mirror the CLI nouns. Every tool returns the typed model JSON; job-starting tools return `{job_id, status:"queued"}` immediately. Long artifacts are exposed as MCP **resources**, never inline-flooded: `devkit://artifact/<id>` (bytes), with text kinds also available via `read_artifact_excerpt` semantics (bounded: ≤ 2000 lines / 64 KiB per read). Screenshots are image resources (fetched on demand, not pushed into context).

| Tool | Inputs (validated) | Returns |
|---|---|---|
| `devkit_doctor` | — | host report: rsync/ssh/terminal/permissions |
| `devkit_discover` | `timeout_s=5` | `DiscoveredDevice[]` |
| `devkit_list_devices` | — | registry `Device[]` |
| `devkit_forget_device` | `device` | Result (local-only) |
| `devkit_pair` | `device`, `timeout_s=120` | Result; message instructs on-device approval |
| `devkit_status` | `device` | `DeviceStatus` |
| `devkit_capabilities` | `device?` | `Capabilities` |
| `devkit_list_titles` | `device` | `Title[]` |
| `devkit_inspect_title` | `device`, `gameid` | TitleDetail |
| `devkit_configure_title` | `device`, `gameid`, `TitleConfig` fields | Result |
| `devkit_preview_deploy` | `device`, `gameid`, `source_dir`, `filters?`, `clean?` | `DeployPreview` |
| `devkit_deploy` | full `DeploySpec` | `{job_id}` (+progress notifications) |
| `devkit_register_title` | `device`, `gameid` | Result (`registration_complete`) |
| `devkit_launch_title` | `device`, `gameid` | `LaunchResult` (`readiness:"unknown"`) |
| `devkit_delete_title` | `device`, `gameid`, `confirm: true` | Result — destructive, flag required |
| `devkit_fetch_logs` | `device`, `kinds=["steam"]` | `{job_id}` → artifacts + bounded excerpt |
| `devkit_screenshot` | `device`, `mode=3` | `{job_id}` → image artifact resource |
| `devkit_controller_config` | `device`, `gameid` | `{job_id}` → VDF artifact |
| `devkit_gpu_trace` | `device` | `{job_id}` → zip artifact (`capture_export_only` on macOS host analysis) |
| `devkit_rgp_capture` | `device` | `{job_id}` → .rgp artifact |
| `devkit_perf_overlay` | `device`, `enabled` | Result |
| `devkit_frametimes` | `device`, `enabled` | Result |
| `devkit_renderdoc_layer` | `device`, `enabled` | Result |
| `devkit_renderdoc_server` | `device`, `action` | Result |
| `devkit_proton_logging` | `device`, `mode`, `winedebug?` | Result |
| `devkit_set_steam_mode` | `device`, `mode: OS\|OS_DEV`, `gameid?`, `args?`, `gdbserver?` | Result |
| `devkit_set_session` | `device`, `session` | Result |
| `devkit_reload_steam` | `device` | Result |
| `devkit_job_status` | `job_id` | JobInfo (state, progress, artifacts) |
| `devkit_job_cancel` | `job_id` | Result |
| `devkit_list_jobs` | `device?`, `state?` | JobInfo[] |
| `devkit_list_artifacts` | `device?`, `kind?` | Artifact[] with resource links |
| `devkit_read_artifact` | `artifact_id`, `tail_lines=200` | bounded text excerpt or resource link |

Resources: `devkit://devices`, `devkit://device/{name}/titles`, `devkit://job/{id}`, `devkit://artifact/{id}`.

Explicitly **not** exposed (audit §7): stop-title, launch-readiness polling, title stdout streaming, arbitrary remote shell. Rationale strings appear in `devkit_capabilities`.

Security posture in tool results: no private key material, no passphrases, no magic phrase; absolute host paths of artifacts are returned (the MCP host is local), remote paths included for transparency.

## 12. Host adapters (`adapters/`)

`HostAdapter` protocol: `state_dir()`, `rsync() -> RsyncInfo`, `ssh() -> Path`, `fix_key_permissions(path)`, `open_folder(path)`, `open_url(url)`, `terminal_launch(cmd) -> Popen`, `discover_tools() -> ToolAvailability`, `context() -> Literal["cli","bundled_app","mcp_stdio"]`.

macOS (`macos.py`):
- rsync resolution order: `$DEVKIT_RSYNC` → bundled `Contents/Resources/bin/rsync` when frozen → `/opt/homebrew/bin/rsync` → PATH scan → flavor check (`--version` output; reject `openrsync`, require protocol ≥ 30).
- ssh: `/usr/bin/ssh` (system); paramiko for non-interactive; `$DEVKIT_SSH` override.
- Key perms: `chmod 0400` (Darwin POSIX branch — fixes upstream crash without patching vendor).
- Interactive shell: Terminal.app via `osascript -e 'tell application "Terminal" to do script …'`; iTerm2 used if `/Applications/iTerm.app` exists.
- Open folder: `/usr/bin/open`; FileZilla lookup includes `/Applications/FileZilla.app`.
- Local Network: on zeroconf timeout in a bundled/MCP context, raise `PERMISSION_LOCAL_NETWORK` with guidance naming the responsible host app when determinable.

Linux/Windows adapters preserve upstream behavior (konsole/gnome-terminal/xterm search list, icacls, cygwin discovery) and are compile-gated, not release-gated.

## 13. Testing

- `tests/unit/` — models round-trip, journal/recovery sweep, progress2 parser (golden outputs), rsync builder flag matrix, validation errors, capability matrix from canned probes, CLI exit codes.
- `tests/integration/` — fake paramiko layer (scripted channels) covering deploy pipeline ordering, pairing 403 JSON parse, screenshot polling, RGP stability loop.
- `tests/hardware/` — requires `DEVKIT_TEST_DEVICE=<sel>`; skipped otherwise. Covers the overview milestone: pair → deploy Linux-native test app → launch → logs + screenshot; Windows/Proton test app; failed launch; device disconnect mid-deploy; Ctrl-C interrupted upload then resume. Results recorded in `docs/hardware-matrix.md` with macOS build, SteamOS build, devkit commit, date — this file is what promotes operations to `supported_tested`.
- Quality gates: `ruff check`, `ruff format --check`, `mypy --strict` on `src/` (vendor excluded), `pytest`. CI (GitHub Actions): unit+integration on macos-latest arm64; hardware suite manual-only.

## 14. Packaging and distribution

- CLI/MCP: `uv tool install steamos-devkit-mac` (PyPI) and a Homebrew formula; both validate rsync 3.x at first run via `doctor`.
- GUI (M7+): Xcode SwiftUI app embedding python-build-standalone 3.12 + wheelhouse + rsync 3.x binary in `Contents/Resources/bin/`; `Info.plist` carries `NSLocalNetworkUsageDescription` and `NSBonjourServices: [_steamos-devkit._tcp]`; Developer ID signature, hardened runtime, `notarytool` + staple. License notices incl. rsync GPLv3 + source offer.
- Versioning: `vYYYY.MM.DD.N` (matches upstream cadence style); 1.0 = overview milestone 1 green on hardware.

## 15. Milestones (sprint plan)

| ID | Deliverable | Exit check |
|---|---|---|
| M0 | Scaffold: uv project, vendor + sync script, models/errors/config, adapters/macos, `doctor` | `devkit doctor` green on clean Mac (brew rsync) |
| M0.5 | **Notarization spike**: throwaway SwiftUI shell + embedded Python + bundled rsync, signed + notarized + stapled | `spctl -a -vv` passes on a fresh user account |
| M1 | Discovery, registry, pair, status, capabilities; CLI | pair + status against real Deck from Terminal |
| M2 | Jobs (journal, progress, cancel, recovery) + deploy pipeline (preview/upload/register/launch) | deploy + launch via CLI; kill -9 mid-upload, resume |
| M3 | Diagnostics: logs, screenshot, controller config; artifact store | artifacts listed + retrievable via CLI |
| M4 | MCP server full surface §11 | Claude Desktop config: discover→pair→deploy→launch→logs→screenshot |
| M5 | Profiling: gpu-trace, RGP, mangohud, renderdoc, proton logs; session/steam-mode | captures land as artifacts on hardware |
| M6 | Hardware matrix + failure modes recorded | `docs/hardware-matrix.md` filled; milestone 1 of overview green |
| M7 | SwiftUI GUI on the MCP bridge | GUI performs the full milestone flow with zero device logic |
| M8 | Release: PyPI + formula + signed/notarized .app + docs | clean-machine install runbook passes |

Sequencing note: M0.5 runs in parallel with M1–M2; if the spike fails, D2's fallback (PySide6 in-process via Briefcase) activates before M7, with no core changes.

## 16. Explicitly out of scope (v1)

Emulator, SteamOS fork, game manager, reverse engineering, automatic optimization; full Steam-library access (devkit titles only); Windows/Proton binary porting; local analysis of captures (gpuvis/RGP/qrenderdoc are off-platform viewers); stop-title and readiness signals until device-side support exists; Ed25519 pairing keys (device-side verification pending); IPv6 discovery.

## Appendix A — ratified implementation deltas (2026-10-19, wave-1 review)

- §1: the `devkit-mcp` console script is registered at M4, when `mcp_server.py` lands (not M0).
- §2.2: `custom_terminal.py` is vendored too — the upstream core imports it at module load.
- §5: `DeviceStatus` matches real device output (see inline comments there); models also define `TitleDetail`, `RsyncInfo`, `ToolAvailability`, `HostContext`, `DoctorCheck`/`DoctorReport`, `JobProgress`, `JobKind`, which the spec referenced but never defined.
- §10: unmapped error codes assigned — `PAIRING_TIMEOUT`→8, `JOB_NOT_FOUND`/`ARTIFACT_NOT_FOUND`→2, `STATE_CORRUPT`→1.
- §12: `$DEVKIT_RSYNC` is authoritative when set — a bad override fails closed rather than falling through to Homebrew/PATH.
- §4: the upstream gameid regex requires ≥ 2 characters; single-char gameids are rejected (upstream-faithful).
- D4: rsync GPLv3 attribution + source-offer notice is required before M8 distribution (tracked in `docs/packaging-spike.md`).
- Hardware conftest scopes its skip marker to `tests/hardware/` only (a session-wide hook must not skip the whole suite).
- §2.1: `cli.py` is the `cli/` package — `common.py` (shared machinery), `meta.py` (doctor/version), one module per command area exposing `register(app)`, wired append-only in `cli/__init__.py`.
- §8 convention: feature modules expose functions taking `core: DevkitCore` as the first parameter; `DevkitCore` delegate methods are wired by the integrator. Shared substrate (owned by the integrator): `device.py` (`resolve_device`/`DeviceHandle`), `jobs/` (`JobStore.list_jobs`, `JobRunner`, `JobContext`), `artifacts.py`.
- §6/§10: `INTERNAL` error code added (unexpected job failures), CLI exit 1.
- §7.3: the itemize parser must treat `cd+++++++++` (directory creation) as an entry; only `>`/`<` leaders are transfers otherwise.
- rsync progress2 records are `\r`-separated; parsers split on both `\r` and `\n`.
- `types-paramiko` added as a dev dependency (paramiko ships no py.typed).

### Wave-2 review deltas (2026-10-19)

- §8 register: the `steam-client-create-shortcut` payload carries seven keys — `force_appid`, `lepton_args`, and `settings.compat_tool` are always present (`""` when unset) because the device script reads them unconditionally; `argv` is `None` when no local config exists so the device's saved entrypoint is never clobbered.
- §8 launch: launch detection checks stdout **and** stderr for the device-side success marker (the RPC helper logs it on stderr); `readiness` stays `"unknown"`.
- §7.2: cancellation is in-process only. A `--no-wait` CLI job dies with its process and is journaled `interrupted` by the next process's recovery sweep; the long-lived MCP server is the real no-wait/cancel context. Cross-process cancel would need a journal cancel-request event — deferred.
- §7.4 gpu-trace: retry once via `gpu-trace --start` on any capture failure (not only "command not found"); RGP stability is size-only (3 equal samples, 1 s interval, no fuser).
- §4 proton logging: `PROTON_LOG` is always `1` when enabled; the WINEDEBUG string goes to its own file (upstream's combined write appears to be an upstream bug).
- Toggles (`mangohudctl`, session-select) run check=True: silent remote failures surface as REMOTE_COMMAND_FAILED (stricter than upstream on purpose).
- `resolve_device` propagates discovery-infrastructure errors (e.g. PERMISSION_LOCAL_NETWORK) instead of masking them as DEVICE_NOT_FOUND; a pure mDNS miss is DEVICE_NOT_FOUND.
- SFTP sidecar writes use absolute paths (`/home/<login>/devkit-game/…`); shell command paths keep the tilde unquoted.
- SFTP-side `DeviceCapabilities` probes beyond the status model feed only the operations matrix.
- Open: CLI selector defaulting is inconsistent between command modules (device commands fall back to last-device; titles/deploy require a positional) — standardized to optional-with-last-device-fallback in wave 3.

### Wave-3 review deltas (2026-10-19)

- §10: selector convention standardized — optional `<sel>` positional with last-device fallback everywhere; `deploy` takes `--device/-d` (trailing positionals make an optional middle one ambiguous). Synopsis text updated.
- §11: built on `mcp` SDK 2.3.0 — FastMCP is renamed `MCPServer`; job progress bridges via connection-level `notifications/progress` polling the journal (0.5 s), not per-request callbacks (the SDK drops those after the tool response — the M7 GUI must consume progress via a notification handler); per-artifact MIME types via a `ResourceTemplate` subclass (no public instance registration in 2.3.0 — flagged in code, locked by tests).
- §8 diagnostics: `fetch_logs` registers one artifact per requested kind (keeps `artifacts list --kind` meaningful); Deck screenshot picks the lexicographic max of candidates (gamescope timestamped names sort chronologically — matters for mode 2's per-layer files); controller-config polls for `config_*.vdf` (≤ 10 s) before pulling.
- Known limitation: `JobHandle.wait()` rebuilds DevkitError from the journal and drops `remote_exit_code` (message survives).
- `cli/` duplication candidates noted for later promotion into `cli/common.py`: the wait-or-print-job-id helper and gameid positional shuffle.
