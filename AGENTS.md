# AGENTS.md — SteamOS Devkit for macOS

Open-source SteamOS Devkit client for macOS (Apple Silicon first): shared Python core with CLI, MCP server, and (later) a SwiftUI GUI that talks to the core over the MCP stdio bridge. Product direction: `overview.md`. Protocol evidence: `docs/audit-upstream.md`. Locked decisions: `docs/decisions.md`. Build contract: `docs/technical-spec.md` — **the spec is authoritative; if reality must diverge, update the spec in the same commit.**

## Ground rules

- One shared implementation: CLI/MCP/GUI contain no device logic; everything goes through `DevkitCore` (`src/steamos_devkit/core.py`) returning typed pydantic results. Core code never prints and never raises bare exceptions across the API boundary — only `DevkitError` with an `ErrorCode`.
- Vendored upstream lives in `src/steamos_devkit/_vendor/` and is **read-only** in normal work. Changes to it go through `patches/` + `scripts/sync-upstream.sh` (see `docs/decisions.md` D1). `_vendor/UPSTREAM_COMMIT` records the pinned sha.
- Device-side helpers in `_vendor/devkit-utils/` are the protocol — used verbatim, never "improved" — and are the only vendored code that runs (rsynced to `~/devkit-utils` by `DeviceHandle.ensure_utils`). The vendored client core (`_vendor/devkit_client/`) is **reference-only**: production modules implement the protocol directly per `docs/audit-upstream.md`; the snapshot stays as protocol evidence and is parity-tested for importability in `tests/unit/test_vendor.py` (D1 addendum, 2026-10-20).
- Never commit secrets, private keys, or real device IPs/hostnames in fixtures (use `192.0.2.x` TEST-NET addresses).
- Capability honesty: `supported_tested` may only be asserted for operations recorded green in `docs/hardware-matrix.md`. No faking device behaviors the upstream protocol lacks (stop-title, readiness, stdout streaming) — report `unsupported_upstream`.

## Commands

```bash
uv sync                          # install locked env (uv required)
uv run pytest                    # unit + integration (hardware tests skip by default)
uv run pytest --cov=steamos_devkit --cov-report=term-missing   # with coverage
uv run ruff check . && uv run ruff format --check .
uv run mypy src                  # strict; _vendor excluded via overrides
scripts/run-gates.sh             # all of the above, same as CI
scripts/test-swift.sh            # Swift GUI tests (fixtures + bridge + view models; CLT swiftc)
scripts/package-app.sh           # build → embed → bundle → sign SteamOSDevkit.app
```

Swift GUI conventions: `-swift-version 6` strict concurrency, macOS 14 target, no Xcode/SPM/XCTest on the build machine (custom harness in `app/Tests/`); `app/Sources/Bridge/DevkitBridge.swift` is the integrator-owned GUI↔core contract — feature agents report gaps instead of editing it.

Coverage gate: ≥ 85% on `src/steamos_devkit/` excluding `_vendor/` (configured in `pyproject.toml`, enforced in CI).

Hardware tests: `tests/hardware/` runs only when `DEVKIT_TEST_DEVICE=<name-or-ip>` is set; results get recorded in `docs/hardware-matrix.md` (macOS build, SteamOS build, commit, date).

## Repo layout

- `src/steamos_devkit/` — package (`core.py`, `models.py`, `errors.py`, `config.py`, `constants.py`, `device.py`, `artifacts.py`, `jobs/`, `adapters/`, `cli/`, `mcp_server.py`, `_vendor/`)
- `tests/` — `unit/`, `integration/` (fake-SSH harness), `hardware/`, `fixtures/` (golden files)
- `scripts/` — `sync-upstream.sh`, `run-gates.sh`, packaging/notarization scripts
- `app/` — SwiftUI GUI: `Sources/Bridge/` (contract + MCP stdio client), `Sources/App/` (UI, zero device logic), `Tests/` (custom harness), packaging plists
- `docs/` — audit, decisions, technical spec, hardware matrix, packaging spike notes
- `upstream/` — audit-only clone of Valve's repo, **gitignored**

## Conventions

- Python 3.11–3.14, uv-managed (`uv.lock` committed), pydantic v2, typer CLI, FastMCP server.
- Style: ruff defaults + repo config; mypy `--strict` on `src/` (excluding `_vendor`).
- Commits: small and logical, `type(scope): summary` (`feat`, `fix`, `test`, `docs`, `chore`, `refactor`). Commit early and often; never amend or rebase shared history without asking.
- Testing: pytest; CLI via `typer.testing.CliRunner`; MCP via FastMCP in-process client; SSH via the fake harness in `tests/integration/` — no real device contact in unit/integration tests.
- When multiple agents work concurrently: stage only files you own, and wrap every `git add`/`git commit` in a retry loop (up to 30 s) to ride out `index.lock` contention.
- Keep `docs/` in sync with behavior in the same commit; update this file when structure, commands, or conventions change.

## Parallel-work conventions (multiple agents share this tree)

- **Feature modules** (`discovery.py`, `titles.py`, `diagnostics.py`, …) expose functions taking `core: DevkitCore` as the first parameter. `DevkitCore` itself stays thin; the integrator wires delegates. Do not add methods to `core.py` from feature work.
- **Shared substrate owned by the integrator** (don't restructure it in feature work; report gaps instead): `core.py`, `device.py` (`resolve_device`, `DeviceHandle` paramiko substrate), `jobs/` (`JobStore`, `JobRunner`, `JobContext`; store method is `list_jobs`), `artifacts.py`, `cli/common.py`, `cli/__init__.py` (except the marked registration section).
- **CLI commands** live in `cli/<area>.py` modules exposing `register(app: typer.Typer)`. Wire them by appending one import + one `register(app)` line inside the marked section of `cli/__init__.py` — append-only, never reorder. If an Edit anchor fails because another agent appended first, re-read and retry.
