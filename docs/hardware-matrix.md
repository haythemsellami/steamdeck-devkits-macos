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
| (template) | e.g. 26.7.1 (25.x) | arm64 | e.g. 20251022.100 (3.7.17) | `abcdef12` | YYYY-MM-DD | name |

## Operation checklist (one row per run)

Result values: `pass`, `fail`, `blocked`, `n/a` (with a note). Record the run
letter/number from the machine matrix in the Result cell, e.g. `pass (1)`.

| Operation | Covers | Result | Notes |
|---|---|---|---|
| Pair | mDNS + explicit IP; on-device approval; 403 declined path | | |
| Status | `devkit status` fields sane vs device | | |
| Deploy Linux-native | `zz_devkit_test` Linux x86_64 test app upload + register | | |
| Deploy Proton | `zz_devkit_test` Windows test app via `compat_tool=proton-*` | | |
| Launch | `run-game` accepted; result distinguishes upload/register/launch, readiness `unknown` | | |
| Logs | steam + proton log pull lands as artifacts, excerpt readable | | |
| Screenshot | modes 1–4 as gamescope allows; artifact retrievable | | |
| Failed-launch handling | broken binary: launch accepted, honest failure surfaced, no crash | | |
| Disconnect mid-deploy | network drop during upload → job fails cleanly, no state corruption | | |
| Interrupted upload + resume | kill -9 / Ctrl-C mid-upload → `interrupted` journal, resume completes via `--partial` | | |
| GPU trace | `gpu-trace` capture → zip artifact | | |
| RGP | trigger → stability loop → verified `.rgp` artifact | | |
| MangoHud | perf overlay on/off; frametimes CSV pull | | |
| RenderDoc | layer enable/disable; replay server start/status/stop | | |
| Proton logs | `PROTON_LOG` on/off; `steam-*.log` filtered pull | | |
| Session controls | session select gamescope/plasma; steam-mode OS/OS_DEV; reload steam | | |

## Run log

Append one subsection per hardware run. Template:

### Run N — YYYY-MM-DD

- Machine row: (number from the matrix above)
- Device: Deck / Frame (devkit service port 32000)
- Result summary: N pass / N fail / N blocked
- Failures and follow-ups: …
