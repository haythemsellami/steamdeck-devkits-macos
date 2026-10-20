# SteamOS Devkit for macOS

An open-source SteamOS Devkit client for macOS (Apple Silicon first): pair
your Mac with a Steam Deck or Steam Frame, then deploy builds, launch titles,
pull logs and screenshots, and drive profiling from a scriptable CLI or an
MCP server. One shared typed core (`DevkitCore`) powers the CLI, the MCP
server, and (later) a SwiftUI GUI — no frontend contains device logic.

Upstream protocol and device-side helpers come from Valve's
[steamos-devkit](https://gitlab.steamos.cloud/devkit/steamos-devkit): the
protocol is implemented directly from the audited upstream source
(`docs/audit-upstream.md`), the vendored snapshot at
`src/steamos_devkit/_vendor/` is retained as the protocol reference, and its
`devkit-utils/` device-side helpers are rsynced verbatim to paired devices.

## Status

**Pre-1.0.** The CLI/MCP surface and core are implemented; hardware validation
is pending — no operation reports `supported_tested` until
[`docs/hardware-matrix.md`](docs/hardware-matrix.md) records it green on a
real device. Interfaces may still change between milestones.

## Requirements

- macOS on Apple Silicon (Intel works, untested), Python 3.11–3.14
- **rsync 3.x** — macOS ≥ 15 ships `openrsync`, which is rejected: `brew install rsync`
- A SteamOS device with developer mode enabled on the same LAN

## Install

Once published: `uv tool install steamos-devkit-mac` (a Homebrew formula is
planned). For now, from a checkout:

```bash
uv sync                 # install the locked environment (uv required)
uv run devkit setup     # one-shot onboarding: keypair, rsync 3.x (installs via
                        # Homebrew if missing), and a probe that surfaces the
                        # macOS Local Network permission prompt
uv run devkit doctor    # re-verify probes at any time
```

## Quickstart

Every device-taking command accepts a selector (registry name, IPv4, or
hostname); once paired, the last-used device is the default and the selector
may be omitted. `--json` gives machine-readable output (automatic when stdout
is not a TTY).

```bash
devkit discover                          # mDNS lookup (optional; IP works too)
devkit pair 192.168.1.50                 # approve the modal on the device
devkit status                            # steamos-get-status of the last device

devkit deploy mygame ./build --launch    # upload, register with Steam, launch
# (deploy takes --device/-d instead of a positional: an optional leading
#  positional would be ambiguous with <gameid> <source_dir>)
devkit titles list
devkit logs --kind steam                 # lands as an artifact with an excerpt
devkit screenshot --mode 3               # full gamescope composition -> PNG
devkit perf-overlay on                   # MangoHud; `off` to disable
```

## MCP server

`devkit mcp` serves the full tool surface over stdio. Claude Desktop config:

```json
{
  "mcpServers": {
    "steamos-devkit": {
      "command": "devkit",
      "args": ["mcp"]
    }
  }
}
```

Note for MCP hosts on macOS: mDNS discovery may be silently blocked until the
host app is granted Local Network access (System Settings > Privacy & Security
> Local Network). Pairing by explicit IP/hostname always works.

## Capability honesty

`devkit capabilities` reports every operation as probed, never hardcoded:
`supported_tested` only for rows recorded green in the hardware matrix,
otherwise `supported_untested` (or `dependency_required`, `unavailable_host`,
`unavailable_device`). Operations the upstream protocol cannot do — stopping
a title, launch-readiness signals, title stdout streaming — are reported as
`unsupported_upstream` rather than faked.

## Documentation map

- [`overview.md`](overview.md) — product direction and agreed architecture
- [`docs/audit-upstream.md`](docs/audit-upstream.md) — protocol evidence from the upstream source
- [`docs/decisions.md`](docs/decisions.md) — locked decisions (D1–D5)
- [`docs/technical-spec.md`](docs/technical-spec.md) — authoritative build contract
- [`docs/packaging-spike.md`](docs/packaging-spike.md) — notarized-bundle spike notes
- [`docs/hardware-matrix.md`](docs/hardware-matrix.md) — recorded hardware runs
- [`tests/hardware/README.md`](tests/hardware/README.md) — running the hardware suite

## Development

```bash
uv sync
uv run pytest                    # unit + integration (hardware tests skip)
scripts/run-gates.sh             # ruff + ruff format + mypy --strict + pytest --cov
```

Hardware suite (real device required, skipped otherwise):

```bash
DEVKIT_TEST_DEVICE=deck uv run pytest tests/hardware -v
```

## License

MIT (this project). Third-party notices live in
[`LICENSES/`](LICENSES/README.md): upstream steamos-devkit attribution
(`upstream-steamos-devkit-MIT.txt`) and — for distributed builds that bundle
rsync 3.x — the GPLv3 text with the source offer (`rsync-GPLv3.txt`). The
embedded Python runtime's notices are still **pending**, tracked in
[`docs/packaging-spike.md`](docs/packaging-spike.md).
