# SteamOS Devkit for macOS — Project Overview

## Goal

Build a maintained, open-source SteamOS Devkit client for current macOS, prioritizing Apple Silicon, with a usable GUI, scriptable CLI, and comprehensive MCP interface.

The Mac controls a real SteamOS device for development, deployment, and diagnostics. This is a standalone developer-tooling project and a foundation for later game-performance investigation.

Upstream: [Valve SteamOS Devkit](https://gitlab.steamos.cloud/devkit/steamos-devkit).

## Agreed architecture: one shared implementation

GUI, CLI, and MCP must invoke the same reusable application layer.

- **Shared core:** device identity, discovery, pairing, SSH communication, transfers, title lifecycle, diagnostics, captures, validation, jobs, and structured results.
- **Host adapters:** platform-specific paths, permissions, process/terminal launching, dependency discovery, and application integration.
- **GUI:** human-facing controls, device selection, progress, and results.
- **CLI:** scriptable operations, JSON output, and meaningful exit codes.
- **MCP:** discoverable tools and resources exposing the shared core directly.

Move reusable orchestration and validation out of GUI handlers. CLI and MCP must run without an open GUI or its graphics dependencies. Avoid separate implementations of the same workflow or using GUI automation as the MCP backend.

Reuse Valve's implementation and existing device facilities where practical, including discovery, pairing, SSH, rsync, and Devkit helper scripts. Evaluate retaining the existing Python core before considering a rewrite. Keep the core portable and preserve Windows/Linux behavior while prioritizing macOS support.

## Agreed MCP scope

These are capability requirements; exact tool names, schemas, and packaging belong in the technical specification.

| Area | Required capabilities |
| --- | --- |
| Discovery | Discover devices and connect through an explicit hostname or IP. |
| Connection | Pair/register, manage SSH identity and device trust, check connectivity, and inspect status. |
| Capabilities | Report host, device, and dependency support for each operation. |
| Development titles | List and inspect deployed Devkit titles and their launch configuration. |
| Deployment | Preview and perform incremental build uploads, configure runtime/entrypoint/arguments, and register titles with Steam. |
| Execution | Launch and stop titles; expose supported title deletion and Steam/session controls explicitly. |
| Diagnostics | Retrieve logs, screenshots, controller configuration, and relevant device information. |
| Profiling/debugging | Expose supported performance-data collection, GPU traces, RGP captures, and debugging/replay setup. |
| Jobs | Track progress, completion, failure, timeout, and cancellation for long operations. |
| Artifacts | List and retrieve capture outputs with metadata and links/references. |

### Interface behavior

- Use typed inputs and validate them in the shared core, including device selection, title identifiers, paths, and command arguments.
- Return structured status, target/title identity, errors, exit codes where applicable, and artifact references. Distinguish upload completion, title registration, process launch, and application readiness.
- Give uploads and captures persistent job identifiers for their lifetime, observable progress, timeouts, and cancellation. Define cleanup and retry behavior for interruptions.
- Report capabilities honestly: supported/tested, dependency required, capture/export only, unavailable on this host/device, or untested.
- Keep logs and traces as retrievable artifacts; provide bounded excerpts and summaries instead of flooding model context.
- Reuse user-approved pairing and SSH access. Keep private keys and credentials out of tool results. Make destructive cleanup/deletion explicit and scoped; ordinary authorized workflows should support autonomous execution.
- Expose meaningful Devkit operations rather than relying on one arbitrary-shell tool.

Example workflow: an LLM selects a paired Deck, uploads a build, launches it, retrieves startup logs and a screenshot, and checks whether the expected screen appeared.

## macOS delivery requirements

- Publish an explicit matrix of tested macOS, architecture, SteamOS, and Devkit versions.
- Validate native dependencies and host-specific paths/permissions on Apple Silicon.
- Test discovery and networking from Terminal, the packaged GUI, and the actual desktop MCP host; their macOS permission contexts can differ.
- Provide straightforward installation, reproducible dependency selection, release artifacts, and update instructions. Plan signing/notarization for a distributed Mac application.
- Validate complete Mac-to-Deck workflows on hardware. Distinguish hardware results from mocked or CI-only checks.

## Scope boundaries and compatibility

- This release focuses on Devkit workflows. An emulator, SteamOS fork, game manager, game reverse engineering, and automatic optimization are later applications.
- Devkit title inventory is distinct from the user's complete Steam library. Keep the API names and data model explicit; broader library support needs another adapter.
- A build must target the device's execution environment, such as Linux x86_64 or Windows through Proton. Porting the client does not port game binaries or compilers.
- Remote capture/download and local analysis are separate capabilities. Do not promise macOS support for every external viewer or Windows-specific debugger.
- Treat source snapshots and existing projects as starting evidence, not proof of compatibility with current releases. Preserve upstream attribution and applicable dependency licenses.

## Reuse and references

Review current upstream code and compare existing work before choosing a base or duplicating functionality:

- [Valve deployment documentation](https://partner.steamgames.com/doc/steamhardware/loadgames?language=english)
- [OatmealDome macOS fork](https://github.com/OatmealDome/steamos-devkit)
- [3Samourai macOS port](https://github.com/3Samourai/SteamOS-Devkit-Client-MacOS)
- [Existing Devkit MCP server](https://github.com/elliotttate/mcp-steamos-devkit)
- [Yuge Hasi macOS CLI](https://github.com/karad/yuge-hasi-dev)
- [Apple local-network privacy](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy)

The contribution is a maintained, integrated, tested offering; existing macOS ports and MCP implementations already cover parts of the idea.

## Handoff and initial success criterion

Use this overview as the agreed product direction. Audit current sources and dependencies, produce a technical specification and implementation plan, then implement and validate them. Resolve exact APIs, module boundaries, UI/packaging choices, supported versions, and release sequencing during that work.

The first complete milestone is: install on an Apple Silicon Mac, pair with a Deck, deploy and launch a build, then retrieve logs and a screenshot through both CLI and MCP, with the GUI using the same core.

Validate a native Linux test application and a Windows/Proton test application, plus failed launch, device disconnection, and interrupted-upload behavior. Extend coverage to profiling and debugging without changing the shared-implementation decision.

