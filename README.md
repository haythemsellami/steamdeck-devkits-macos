# SteamOS Devkit for macOS

An open-source SteamOS Devkit client for macOS (Apple Silicon first): pair
your Mac with a Steam Deck or Steam Frame, then deploy builds, launch titles,
pull logs and screenshots, and drive profiling from a scriptable CLI, an
MCP server, or a native SwiftUI app. One shared typed core (`DevkitCore`)
powers all three — no frontend contains device logic.

Upstream protocol and device-side helpers come from Valve's
[steamos-devkit](https://gitlab.steamos.cloud/devkit/steamos-devkit): the
protocol is implemented directly from the audited upstream source
(`docs/audit-upstream.md`), the vendored snapshot at
`src/steamos_devkit/_vendor/` is retained as the protocol reference, and its
`devkit-utils/` device-side helpers are rsynced verbatim to paired devices.

## Status

**Pre-1.0.** Milestone 1 is green on real hardware for the CLI and the MCP
surface (run 1 in [`docs/hardware-matrix.md`](docs/hardware-matrix.md): pair,
status, deploy, launch, logs, screenshot, failure modes). The SwiftUI GUI is
built and packaged (ad-hoc signed; Developer ID notarization is pending
credentials). Operations only report `supported_tested` once the matrix
records them green — everything else stays honestly `supported_untested`.

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

## GUI app

```bash
scripts/package-app.sh       # build → embed Python+core → bundle rsync → sign
open app/build/SteamOSDevkit.app
```

The app embeds its own Python runtime, the core wheel with pinned deps, and
rsync 3.x — nothing else to install. It spawns the MCP server as a subprocess
and renders from it; the bundle is its own macOS privacy identity, so Local
Network prompts attach to the app itself (not your terminal). Dev builds are
ad-hoc signed (Gatekeeper assessment fails by design until Developer ID
notarization — see `docs/packaging-spike.md`).

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

## Troubleshooting

**`discover` finds nothing / `pair` fails with "no route to host" (errno 65)
while the Deck is clearly online.** macOS Local Network privacy is blocking
the host app. Grant it under System Settings > Privacy & Security > Local
Network. Two traps: the permission attaches to the *responsible app* (your
terminal, or the MCP host app — e.g. Claude Desktop needs it too), and
**tmux breaks the chain**: the tmux server daemonizes under launchd, so the
grant never applies inside tmux. Run device commands outside tmux, or use a
Terminal window via `scripts/run-hardware-matrix.sh`. `devkit setup` runs a
short probe to surface the prompt early.

## Documentation map

- [`overview.md`](overview.md) — product direction and agreed architecture
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to contribute (gates, tests, commit style)
- [`SECURITY.md`](SECURITY.md) — how to report security issues
- [`docs/audit-upstream.md`](docs/audit-upstream.md) — protocol evidence from the upstream source
- [`docs/decisions.md`](docs/decisions.md) — locked decisions (D1–D6)
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
(`upstream-steamos-devkit-MIT.txt`), the GPLv3 text with the source offer for
the bundled rsync 3.x (`rsync-GPLv3.txt`), and the embedded Python runtime's
notices (`python-runtime/`).
