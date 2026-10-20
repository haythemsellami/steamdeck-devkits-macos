"""Extra unit tests for mcp_server.py — M4: read_only_hint honesty.

Two complementary assertions:

1. A documented static mapping: the set of tools annotated read-only is
   exactly the set whose call graph never mutates the device or local state
   (never reaches ``DeviceHandle.ensure_utils``'s devkit-utils upload, never
   creates state-dir layout or probe files).
2. A runtime sweep: every read-only-annotated tool is invoked under fakes
   while ``DeviceHandle.ensure_utils`` is patched to record calls; any call
   means the annotation lies.

Chosen over a pure call-graph walk because the sweep exercises the real
in-process MCP dispatch path, and the static mapping catches a tool that is
read-only today being re-annotated without review.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import paramiko
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from steamos_devkit.device import DeviceHandle
from steamos_devkit.mcp_server import create_server
from steamos_devkit.models import Device

GAMEID = "mcp_game"

# The honest read-only set (M4). Members never mutate device or local state:
#   devkit_discover      — passive mDNS listen only
#   devkit_list_devices  — reads the local registry file
#   devkit_preview_deploy — rsync -n dry run; never calls ensure_utils
#   devkit_job_status / devkit_list_jobs — read job journals
#   devkit_list_artifacts / devkit_read_artifact — read the artifact store
# Everything else mutates: status/list_titles/inspect_title/capabilities reach
# ensure_utils (devkit-utils upload, at least once per process) and doctor/
# capabilities create the state-dir layout plus a probe file.
EXPECTED_READ_ONLY = frozenset(
    {
        "devkit_discover",
        "devkit_list_devices",
        "devkit_preview_deploy",
        "devkit_job_status",
        "devkit_list_jobs",
        "devkit_list_artifacts",
        "devkit_read_artifact",
    }
)


def _fake_rsync(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "rsync"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf 'rsync  version 3.5.1  protocol version 33\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@pytest.fixture
def prepared(state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """State dir with a pairing key, a registered device, and a fake rsync."""
    monkeypatch.setenv("DEVKIT_RSYNC", str(_fake_rsync(tmp_path / "bin")))
    from steamos_devkit.core import DevkitCore

    core = DevkitCore(state_dir=state_dir)
    core.state.ensure_layout()
    paramiko.RSAKey.generate(2048).write_private_key_file(str(core.state.key_path))
    core.state.devices.upsert(
        Device(name="deck", address="192.0.2.10", login="deck", added_via="explicit")
    )
    return state_dir


@pytest.fixture
def server(prepared: Path) -> MCPServer[None]:
    return create_server(state_dir=prepared)


def run_async(coro: Any) -> Any:
    return asyncio.run(coro)


def test_read_only_annotation_set_matches_documented_mapping(server: MCPServer[None]) -> None:
    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            tools = await client.list_tools()
            read_only = {
                t.name
                for t in tools.tools
                if t.annotations is not None and t.annotations.read_only_hint is True
            }
            assert read_only == EXPECTED_READ_ONLY

    run_async(scenario())


def test_read_only_tools_never_reach_ensure_utils(
    prepared: Path, server: MCPServer[None], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ensure_utils_calls: list[str] = []

    def record(self: DeviceHandle, force: bool = False) -> None:
        ensure_utils_calls.append(self.device.address)

    monkeypatch.setattr(DeviceHandle, "ensure_utils", record)
    monkeypatch.setattr("steamos_devkit.discovery.discover", lambda core, timeout_s=5.0: [])

    source = tmp_path / "build"
    source.mkdir(exist_ok=True)
    (source / "game.sh").write_text("#!/bin/sh\necho hello\n", encoding="utf-8")

    calls: list[tuple[str, dict[str, Any]]] = [
        ("devkit_discover", {"timeout_s": 1}),
        ("devkit_list_devices", {}),
        (
            "devkit_preview_deploy",
            {"device": "deck", "gameid": GAMEID, "source_dir": str(source)},
        ),
        ("devkit_job_status", {"job_id": "job_01ARZ3NDEKTSV4RRFFQ69G5FAV"}),  # isError ok
        ("devkit_list_jobs", {}),
        ("devkit_list_artifacts", {}),
        ("devkit_read_artifact", {"artifact_id": "art_01ARZ3NDEKTSV4RRFFQ69G5FAV"}),  # isError ok
    ]

    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            for tool, args in calls:
                await client.call_tool(tool, args)  # error results are fine here

    run_async(scenario())
    assert ensure_utils_calls == []
