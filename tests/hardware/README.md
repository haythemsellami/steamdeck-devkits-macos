# Hardware tests (real SteamOS device required)

This suite runs against a **real device** over the network. It is skipped
entirely unless `DEVKIT_TEST_DEVICE` is set, so it never runs in CI or in an
ordinary `uv run pytest`.

## Running

```bash
# name from `devkit devices list`, or a bare IPv4 address
DEVKIT_TEST_DEVICE=deck uv run pytest tests/hardware -v
DEVKIT_TEST_DEVICE=192.0.2.10 uv run pytest tests/hardware -v
```

Prerequisites: the device is on the same LAN, developer mode is enabled, and
the device is already paired (or the pairing prompt is within reach — the
pair test requires approving the on-device modal within the timeout).

Modules run in a fixed order (pair first, destructive sweep last), not in
pytest's alphabetical order; see `_MODULE_ORDER` in `conftest.py`. Every test
prints a one-line `[hardware-matrix] <test>: pass key=value...` summary AND
appends it as JSON to `$DEVKIT_STATE_DIR/hardware-records.jsonl` (stdout is
captured by pytest, so the JSONL file is the reliable channel) —
transcribe those lines into `docs/hardware-matrix.md` after the run.

## Safety rules

- Destructive tests (deploy, delete, failed-launch, disconnect, resume) use
  the throwaway gameid `zz_devkit_test` **only** — exposed here as the
  `hardware_gameid` fixture. Never point them at a title you care about.
  Every destructive test ends with `devkit titles delete <device>
  zz_devkit_test --confirm` cleanup, including on failure.
- Interrupt/resume tests kill local processes and drop connections; the
  device-side `~/devkit-game/zz_devkit_test/` tree is removed by the same
  cleanup path.
- The suite never touches reserved gameids (`steam`, `steamdeckard`,
  `steamvr*`) and never enables persistent settings without restoring them.

## Recording results

Every hardware run gets recorded in `docs/hardware-matrix.md`: macOS build,
architecture, SteamOS build, devkit commit, date, tester, and a per-operation
result. That matrix is the **only** source that may promote an operation to
`supported_tested` in the capabilities API (see AGENTS.md, "Capability
honesty"). A row that was not run green on hardware stays
`supported_untested` — no exceptions.

## MCP transport validation

`scripts/mcp_smoke.py <device>` drives the milestone-1 workflow (pair →
configure → preview → deploy → launch → logs → screenshot → cleanup) through
the MCP server over stdio, using the official `mcp` SDK client — the same
JSON-RPC an LLM host speaks, with no LLM involved. Results land in the "MCP
transport runs" section of `docs/hardware-matrix.md`.
