# Hardware test matrix

Results of manual hardware runs (`tests/hardware/`, see its README.md for how
to run). This file is the promotion gate for capability reporting per
AGENTS.md: an operation may report `supported_tested` **only** when it is
recorded green here for the current devkit commit; everything else stays
`supported_untested` (or its honest downgrade: `dependency_required`,
`capture_export_only`, `unavailable_host`, `unavailable_device`,
`unsupported_upstream`).

## Test machine matrix

| Run | macOS build | Arch | SteamOS build | Devkit commit | Date | Tester |
|---|---|---|---|---|---|---|
| 1 | 26.7.1 (25G241) | arm64 | 3.8.28 (20260922.1), client 1791918634, stable | `488424b` | 2026-10-20 | haythem |

## Operation checklist (one row per run)

Result values: `pass`, `fail`, `blocked`, `n/a` (with a note). Record the run
letter/number from the machine matrix in the Result cell, e.g. `pass (1)`.

| Operation | Covers | Result | Notes |
|---|---|---|---|
| Pair | mDNS + explicit IP; on-device approval; 403 declined path | pass (1) | explicit IP + approval; idempotent re-pair. mDNS discovery not exercised (host LAN permission blocked at the time) |
| Status | `devkit status` fields sane vs device | pass (1) | SteamOS 3.8.28, stable branch, client 1791918634; fields parse into DeviceStatus incl. enum-prefix and list args |
| Deploy Linux-native | `zz_devkit_test` Linux x86_64 test app upload + register | pass (1) | configure → preview → deploy → registered; found+fixed: sidecar write before `~/devkit-game` exists (488424b) |
| Deploy Proton | `zz_devkit_test` Windows test app via `compat_tool=proton-*` | | not covered in run 1 |
| Launch | `run-game` accepted; result distinguishes upload/register/launch, readiness `unknown` | pass (1) | launch accepted; readiness honestly `unknown` |
| Logs | steam + proton log pull lands as artifacts, excerpt readable | pass (1) | steam logs artifact; found+fixed: excerpt reads file inside directory artifact (488424b) |
| Screenshot | modes 1–4 as gamescope allows; artifact retrievable | pass (1) | mode 3 PNG verified by magic bytes |
| Failed-launch handling | broken binary: launch accepted, honest failure surfaced, no crash | pass (1) | device refuses at registration (entrypoint validation) — honest early failure; test updated to accept it |
| Disconnect mid-deploy | network drop during upload → job fails cleanly, no state corruption | pass (1) | unreachable-IP fail-fast covered (`DEVICE_UNREACHABLE` < 20 s); true mid-upload network drop not covered |
| Interrupted upload + resume | kill -9 / Ctrl-C mid-upload → `interrupted` journal, resume completes via `--partial` | pass (1) | cancelled mid-transfer, journal `cancelled`, redeploy to success |
| GPU trace | `gpu-trace` capture → zip artifact | | not covered in run 1 |
| RGP | trigger → stability loop → verified `.rgp` artifact | | not covered in run 1 |
| MangoHud | perf overlay on/off; frametimes CSV pull | | not covered in run 1 |
| RenderDoc | layer enable/disable; replay server start/status/stop | | not covered in run 1 |
| Proton logs | `PROTON_LOG` on/off; `steam-*.log` filtered pull | | not covered in run 1 |
| Session controls | session select gamescope/plasma; steam-mode OS/OS_DEV; reload steam | | not covered in run 1 |

Run 1 notes: also fixed from this run — connection-shaped rsync failures now
map to `DEVICE_UNREACHABLE` (were masked as `REMOTE_COMMAND_FAILED`), and both
rsync rsh strings carry `ConnectTimeout=10` so dead hosts fail fast (488424b).
The `[hardware-matrix]` stdout lines were captured by pytest and lost; records
now also append to `$DEVKIT_STATE_DIR/hardware-records.jsonl` (conftest fix).

## MCP transport runs

The overview's example workflow driven through the MCP server (stdio) by
`scripts/mcp_smoke.py` — a model-agnostic driver using the official mcp SDK
client, no LLM host required.

| Run | Devkit commit | Date | Result | Steps |
|---|---|---|---|---|
| 1 | `2872c25` + `scripts/mcp_smoke.py` | 2026-10-20 | pass | tools surface (33), device present, status, configure_title, preview_deploy (1 file, 41 B), deploy job succeeded, launch accepted (readiness `unknown`), fetch_logs job + artifact, screenshot job + artifact with PNG verified via the `devkit://artifact/<id>` resource, cleanup delete |

## GUI bridge runs (M7)

The GUI's exact data path: Swift `MCPBridge` (`app/Sources/Bridge/Internal/`) ↔ `devkit-mcp` over stdio ↔ device, driven by `app/Tests/BridgeTests/HardwareFlowTests.swift` via `scripts/test-swift.sh --hardware-only` from a TCC-granted Terminal session.

| Run | Devkit commit | Date | Result | Steps |
|---|---|---|---|---|
| 1 | `ba3f039` | 2026-10-21 | pass | listDevices + status (hostname `steamdeck`, SteamOS 20260922.1, steam OS) → configureTitle → previewDeploy (2 files, 45 B) → deploy job succeeded with 5 push progress events on the GUI notification channel → launch accepted (readiness `unknown`) → fetch_logs artifact listed + read → screenshot job → PNG (1,028,477 B, magic bytes verified) via `resources/read` → cleanup delete. A same-day earlier attempt failed fast with `DEVICE_UNREACHABLE`/`REMOTE_COMMAND_FAILED` while the Deck was asleep — the bridge decoded both structured error payloads correctly, so the error path is hardware-proven too. |

## Run log

Append one subsection per hardware run. Template:

### Run N — YYYY-MM-DD

- Machine row: (number from the matrix above)
- Device: Deck / Frame (devkit service port 32000)
- Result summary: N pass / N fail / N blocked
- Failures and follow-ups: …
