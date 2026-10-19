"""Unit tests for the MCP server surface (M4, spec section 11).

The server is driven in-process: the SDK's high-level ``Client`` in ``legacy``
mode connects to the ``MCPServer`` instance over the SDK's in-memory streams
(real JSON-RPC framing + initialize handshake, no sockets). Device contact goes
through the fake-SSH harness, device HTTP through the ``pairing._urlopen``
seam, and bulk transfer through a fake rsync selected with ``DEVKIT_RSYNC`` —
the same seams as the CLI tests, so no test touches a real device or LAN.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import select
import subprocess
import sys
from pathlib import Path
from typing import Any

import paramiko
import pytest
import ulid
from fake_ssh import make_paramiko_patch
from mcp import Client
from mcp.server.mcpserver import MCPServer
from typer.testing import CliRunner

from steamos_devkit import cli, pairing
from steamos_devkit.artifacts import register_artifact
from steamos_devkit.config import StateStore
from steamos_devkit.mcp_server import create_server
from steamos_devkit.models import Device, DiscoveredDevice, JobKind

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
GAMEID = "mcp_game"
LIST_GAMES_JSON = json.dumps([{"gameid": GAMEID}, {"gameid": "spacegame_dev"}])
PREPARE_JSON = json.dumps({"user": "deck", "directory": f"/home/deck/devkit-game/{GAMEID}"})
STATUS_JSON = (FIXTURES / "steamos-get-status.json").read_bytes()

EXPECTED_TOOLS = {
    "devkit_doctor",
    "devkit_discover",
    "devkit_list_devices",
    "devkit_forget_device",
    "devkit_pair",
    "devkit_status",
    "devkit_capabilities",
    "devkit_list_titles",
    "devkit_inspect_title",
    "devkit_configure_title",
    "devkit_preview_deploy",
    "devkit_deploy",
    "devkit_register_title",
    "devkit_launch_title",
    "devkit_delete_title",
    "devkit_fetch_logs",
    "devkit_screenshot",
    "devkit_controller_config",
    "devkit_gpu_trace",
    "devkit_rgp_capture",
    "devkit_perf_overlay",
    "devkit_frametimes",
    "devkit_renderdoc_layer",
    "devkit_renderdoc_server",
    "devkit_proton_logging",
    "devkit_set_steam_mode",
    "devkit_set_session",
    "devkit_reload_steam",
    "devkit_job_status",
    "devkit_job_cancel",
    "devkit_list_jobs",
    "devkit_list_artifacts",
    "devkit_read_artifact",
}


# --- harness --------------------------------------------------------------------


def run_async(coro: Any) -> Any:
    """Drive one async scenario per test; a fresh loop per call."""
    return asyncio.run(coro)


async def _collect(message: Any, bucket: list[Any]) -> None:
    bucket.append(message)


def _fake_rsync(bin_dir: Path, argv_log: Path, transfer_body: str, *, gate: bool = True) -> Path:
    """A fake rsync: answers --version, logs argv, runs transfer_body.

    With ``gate`` the body runs only for the progress2 deploy invocation — the
    devkit-utils upload just exits 0 (same shape as the integration harness).
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    if gate:
        body_block = f'case " $* " in\n  *" --info=progress2 "*)\n{transfer_body}\n    ;;\nesac\n'
    else:
        body_block = f"{transfer_body}\n"
    path = bin_dir / "rsync"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf 'rsync  version 3.5.1  protocol version 33\\n'\n"
        "  exit 0\n"
        "fi\n"
        f'printf \'%s\\n\' "$@" >> "{argv_log}"\n'
        f"{body_block}"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@pytest.fixture
def prepared(state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """State dir with a pairing key, a registered device, and a fake rsync."""
    monkeypatch.setenv(
        "DEVKIT_RSYNC", str(_fake_rsync(tmp_path / "bin", tmp_path / "rsync-argv.log", ""))
    )
    from steamos_devkit.core import DevkitCore

    core = DevkitCore(state_dir=state_dir)
    core.state.ensure_layout()
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(str(core.state.key_path))
    # pairing.pair reads the .pub half; write it the way ensure_key does
    core.state.pubkey_path.write_text(pairing.public_key_line(key), encoding="utf-8")
    core.state.devices.upsert(
        Device(name="deck", address="192.0.2.10", login="deck", added_via="explicit")
    )
    return state_dir


@pytest.fixture
def server(prepared: Path) -> MCPServer[None]:
    return create_server(state_dir=prepared)


def _structured(result: Any) -> Any:
    """The typed-model JSON a tool returned (unwraps the list-wrapped forms)."""
    assert not result.is_error, f"unexpected is_error: {result.content}"
    return result.structured_content


def _error_payload(result: Any) -> dict[str, Any]:
    """The JSON payload of an isError result (after the SDK's text prefix)."""
    assert result.is_error, f"expected is_error, got {result.structured_content!r}"
    text = result.content[0].text
    return json.loads(text[text.index("{") :])


def _source_tree(tmp_path: Path) -> Path:
    source = tmp_path / "build"
    source.mkdir(exist_ok=True)
    (source / "game.sh").write_text("#!/bin/sh\necho hello\n", encoding="utf-8")
    return source


async def _wait_for_job(client: Client, job_id: str, *, timeout_s: float = 20.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        result = await client.call_tool("devkit_job_status", {"job_id": job_id})
        info = _structured(result)
        if info["state"] not in ("queued", "running"):
            return info
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"job {job_id} never reached a terminal state: {info}")
        await asyncio.sleep(0.1)


# --- surface ---------------------------------------------------------------------


def test_tools_list_full_surface(server: MCPServer[None]) -> None:
    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            tools = await client.list_tools()
            names = {t.name for t in tools.tools}
            assert names == EXPECTED_TOOLS
            by_name = {t.name: t for t in tools.tools}
            # delete_title: confirm is required and must be literally true
            delete_schema = by_name["devkit_delete_title"].input_schema
            assert "confirm" in delete_schema["required"]
            confirm_schema = delete_schema["properties"]["confirm"]
            assert confirm_schema["const"] is True
            # gameid inputs carry the upstream regex (spec section 4)
            deploy_schema = by_name["devkit_deploy"].input_schema
            assert deploy_schema["properties"]["gameid"]["pattern"] == r"^[A-Za-z_][A-Za-z0-9_.]+$"
            # mode Literals are enum-constrained
            assert by_name["devkit_set_session"].input_schema["properties"]["session"]["enum"] == [
                "gamescope",
                "plasma-x11",
                "plasma-wayland",
            ]
            # destructive hint on delete, read-only on status
            assert by_name["devkit_delete_title"].annotations.destructive_hint is True
            assert by_name["devkit_status"].annotations.read_only_hint is True

    run_async(scenario())


def test_resources_listed(server: MCPServer[None]) -> None:
    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            resources = await client.list_resources()
            assert [str(r.uri) for r in resources.resources] == ["devkit://devices"]
            templates = await client.list_resource_templates()
            assert {t.uri_template for t in templates.resource_templates} == {
                "devkit://device/{name}/titles",
                "devkit://job/{job_id}",
                "devkit://artifact/{artifact_id}",
            }

    run_async(scenario())


# --- simple tool flows -------------------------------------------------------------


def test_doctor_reports_host(prepared: Path, server: MCPServer[None]) -> None:
    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool("devkit_doctor", {})
            report = _structured(result)
            assert report["ok"] is True
            assert report["state_dir"] == str(prepared)
            checks = {c["name"]: c["status"] for c in report["checks"]}
            assert checks["rsync"] == "ok"  # the DEVKIT_RSYNC fake probes fine

    run_async(scenario())


def test_pair_then_list_devices(
    prepared: Path, server: MCPServer[None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("getpass.getuser", lambda: "testuser")
    monkeypatch.setattr("socket.gethostname", lambda: "testhost")
    properties = (FIXTURES / "properties.json").read_bytes()

    def fake_urlopen(request: Any, timeout: float) -> bytes:
        url = request.full_url
        if url.endswith("/properties.json"):
            return properties
        if url.endswith("/register"):
            return b""
        raise AssertionError(url)

    monkeypatch.setattr(pairing, "_urlopen", fake_urlopen)

    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool("devkit_pair", {"device": "192.0.2.55"})
            payload = _structured(result)
            assert payload["ok"] is True
            assert payload["message"] == "paired"
            assert payload["device"]["name"] == "192.0.2.55"

            result = await client.call_tool("devkit_list_devices", {})
            devices = _structured(result)["result"]
            names = {d["name"] for d in devices}
            assert names == {"deck", "192.0.2.55"}

            # the devices resource reflects the same registry
            read = await client.read_resource("devkit://devices")
            text = read.contents[0].text
            assert {d["name"] for d in json.loads(text)} == names

    run_async(scenario())


def test_discover_via_tool(server: MCPServer[None], monkeypatch: pytest.MonkeyPatch) -> None:
    found = [
        DiscoveredDevice(name="deck", address="192.0.2.10", login="deck", sshd=True, via="mdns")
    ]
    monkeypatch.setattr("steamos_devkit.discovery.discover", lambda core, timeout_s=5.0: found)

    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool("devkit_discover", {"timeout_s": 1})
            devices = _structured(result)["result"]
            assert devices[0]["name"] == "deck"
            assert devices[0]["address"] == "192.0.2.10"

    run_async(scenario())


def test_status_and_list_titles(server: MCPServer[None], monkeypatch: pytest.MonkeyPatch) -> None:
    ssh = make_paramiko_patch(
        monkeypatch,
        commands=[
            (r"steamos-get-status --json", STATUS_JSON, b"", 0),
            (r"steamos-list-games", LIST_GAMES_JSON, b"", 0),
        ],
    )

    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool("devkit_status", {"device": "deck"})
            status = _structured(result)
            assert status["hostname"] == "steamdeck"
            assert status["steam_status"] == "OS"

            result = await client.call_tool("devkit_list_titles", {"device": "deck"})
            titles = _structured(result)["result"]
            assert [t["gameid"] for t in titles] == [GAMEID, "spacegame_dev"]

            # the same titles are exposed as a resource
            read = await client.read_resource("devkit://device/deck/titles")
            assert json.loads(read.contents[0].text)[0]["gameid"] == GAMEID

    run_async(scenario())
    assert "python3 ~/devkit-utils/steamos-get-status --json" in ssh.commands_run


def test_capabilities_host_only(server: MCPServer[None]) -> None:
    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool("devkit_capabilities", {})
            caps = _structured(result)
            assert caps["device"] is None
            assert caps["operations"]["deploy"]["status"] == "supported_untested"
            # never faked upstream gaps (spec section 9)
            assert caps["operations"]["stop_title"]["status"] == "unsupported_upstream"

    run_async(scenario())


def test_configure_and_launch_title(
    server: MCPServer[None], monkeypatch: pytest.MonkeyPatch
) -> None:
    ssh = make_paramiko_patch(monkeypatch, commands=[(r"run-game", b"success\n", b"", 0)])

    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool(
                "devkit_configure_title",
                {
                    "device": "deck",
                    "gameid": GAMEID,
                    "entrypoint": "run.sh",
                    "args": ["-v"],
                    "runtime": "proton-stable",
                },
            )
            assert _structured(result)["ok"] is True

            result = await client.call_tool(
                "devkit_launch_title", {"device": "deck", "gameid": GAMEID}
            )
            launch = _structured(result)
            assert launch["launched"] is True
            assert launch["readiness"] == "unknown"  # never faked (audit section 7)

    run_async(scenario())
    fs = ssh.filesystem
    assert json.loads(fs[f"/home/deck/devkit-game/{GAMEID}-argv.json"]) == ["run.sh", "-v"]


def test_toggle_tools(server: MCPServer[None], monkeypatch: pytest.MonkeyPatch) -> None:
    ssh = make_paramiko_patch(monkeypatch, commands=[(r".", b"", b"", 0)])

    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            for tool, args in (
                ("devkit_perf_overlay", {"device": "deck", "enabled": True}),
                ("devkit_frametimes", {"device": "deck", "enabled": False}),
                ("devkit_renderdoc_layer", {"device": "deck", "enabled": True}),
                ("devkit_renderdoc_server", {"device": "deck", "action": "status"}),
                ("devkit_proton_logging", {"device": "deck", "mode": "on"}),
                ("devkit_set_steam_mode", {"device": "deck", "mode": "OS_DEV"}),
                ("devkit_set_session", {"device": "deck", "session": "gamescope"}),
            ):
                result = await client.call_tool(tool, args)
                assert _structured(result)["ok"] is True, tool

    run_async(scenario())
    assert "mangohudctl set no_display false" in ssh.commands_run
    assert any(
        "holo-session-select" in c or "steamos-session-select" in c for c in ssh.commands_run
    )


def test_forget_device(prepared: Path, server: MCPServer[None]) -> None:
    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool("devkit_forget_device", {"device": "deck"})
            assert _structured(result)["ok"] is True
            result = await client.call_tool("devkit_list_devices", {})
            assert _structured(result)["result"] == []
            # forgetting an unknown device is a structured DEVICE_NOT_FOUND error
            result = await client.call_tool("devkit_forget_device", {"device": "ghost"})
            assert _error_payload(result)["code"] == "DEVICE_NOT_FOUND"

    run_async(scenario())
    assert (
        not (prepared / "devices.json").exists()
        or '"devices": []' in (prepared / "devices.json").read_text()
    )


# --- validation and error paths -----------------------------------------------------


def test_delete_title_requires_literal_confirm(
    server: MCPServer[None], monkeypatch: pytest.MonkeyPatch
) -> None:
    ssh = make_paramiko_patch(monkeypatch, commands=[(r"steamos-delete", b"deleted\n", b"", 0)])

    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            # missing confirm: SDK argument validation rejects the call
            result = await client.call_tool(
                "devkit_delete_title", {"device": "deck", "gameid": GAMEID}
            )
            assert result.is_error
            assert "confirm" in result.content[0].text
            # confirm: false fails the Literal[True] constraint
            result = await client.call_tool(
                "devkit_delete_title", {"device": "deck", "gameid": GAMEID, "confirm": False}
            )
            assert result.is_error
            # confirm: true actually deletes
            result = await client.call_tool(
                "devkit_delete_title", {"device": "deck", "gameid": GAMEID, "confirm": True}
            )
            assert _structured(result)["ok"] is True

    run_async(scenario())
    assert f"python3 ~/devkit-utils/steamos-delete --delete-title {GAMEID}" in ssh.commands_run


def test_error_payload_carries_devkit_error_code(
    server: MCPServer[None], monkeypatch: pytest.MonkeyPatch
) -> None:
    # bare names fall back to mDNS; make the lookup miss deterministically
    monkeypatch.setattr("steamos_devkit.discovery.resolve_name", lambda core, name, **kw: None)

    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool("devkit_status", {"device": "livingroom"})
            payload = _error_payload(result)
            assert payload["code"] == "DEVICE_NOT_FOUND"
            assert "livingroom" in payload["message"]

            result = await client.call_tool(
                "devkit_job_status", {"job_id": "job_01ARZ3NDEKTSV4RRFFQ69G5FAV"}
            )
            assert _error_payload(result)["code"] == "JOB_NOT_FOUND"

    run_async(scenario())


def test_deploy_checksum_and_update_conflict_is_validation(
    server: MCPServer[None], tmp_path: Path
) -> None:
    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool(
                "devkit_deploy",
                {
                    "device": "deck",
                    "gameid": GAMEID,
                    "source_dir": str(_source_tree(tmp_path)),
                    "checksum": True,
                    "update": True,
                },
            )
            payload = _error_payload(result)
            assert payload["code"] == "VALIDATION"

    run_async(scenario())


def test_deploy_missing_source_dir_is_validation(server: MCPServer[None], tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool(
                "devkit_deploy",
                {"device": "deck", "gameid": GAMEID, "source_dir": str(tmp_path / "missing")},
            )
            assert _error_payload(result)["code"] == "VALIDATION"

    run_async(scenario())


def test_preview_deploy(
    server: MCPServer[None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rsync = _fake_rsync(
        tmp_path / "bin-preview",
        tmp_path / "argv-preview.log",
        '    cat "' + str(FIXTURES / "rsync-itemize-dry-run.txt") + '"\n'
        "    printf 'Total transferred file size: 44,654,742 bytes\\n'",
        gate=False,
    )
    monkeypatch.setenv("DEVKIT_RSYNC", str(rsync))

    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool(
                "devkit_preview_deploy",
                {"device": "deck", "gameid": GAMEID, "source_dir": str(_source_tree(tmp_path))},
            )
            preview = _structured(result)
            assert preview["files_added"] == 2
            assert preview["bytes_to_transfer"] == 44_654_742

    run_async(scenario())


# --- jobs through the MCP surface ----------------------------------------------------


def _deploy_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    make_paramiko_patch(
        monkeypatch,
        commands=[
            (r"steamos-prepare-upload", PREPARE_JSON, b"", 0),
            (r"create-shortcut", b'{"success": "registered with steam"}', b"", 0),
        ],
    )


def test_deploy_job_lifecycle(
    server: MCPServer[None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deploy_fakes(monkeypatch)
    rsync_log = tmp_path / "rsync-argv.log"
    monkeypatch.setenv(
        "DEVKIT_RSYNC",
        str(
            _fake_rsync(
                tmp_path / "bin-deploy",
                rsync_log,
                f'    cat "{FIXTURES / "rsync-progress2-success.txt"}"',
            )
        ),
    )

    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool(
                "devkit_deploy",
                {"device": "deck", "gameid": GAMEID, "source_dir": str(_source_tree(tmp_path))},
            )
            started = _structured(result)
            assert started["status"] == "queued"
            assert started["job_id"].startswith("job_")

            info = await _wait_for_job(client, started["job_id"])
            assert info["state"] == "succeeded"
            assert "deploy complete" in info["message"]
            assert info["kind"] == "deploy"
            assert info["device"] == "deck"

            # list_jobs sees the same journal; the job resource serves it
            result = await client.call_tool("devkit_list_jobs", {})
            jobs = _structured(result)["result"]
            assert [j["id"] for j in jobs] == [started["job_id"]]
            read = await client.read_resource(f"devkit://job/{started['job_id']}")
            assert json.loads(read.contents[0].text)["state"] == "succeeded"

            # a terminal job cancel is a no-op Result, not an error
            result = await client.call_tool("devkit_job_cancel", {"job_id": started["job_id"]})
            assert _structured(result)["ok"] is True

    run_async(scenario())
    argv_lines = rsync_log.read_text(encoding="utf-8").splitlines()
    assert "--info=progress2" in argv_lines and "--partial" in argv_lines


def test_job_cancel_mid_transfer(
    server: MCPServer[None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_paramiko_patch(monkeypatch, commands=[(r"steamos-prepare-upload", PREPARE_JSON, b"", 0)])
    marker = tmp_path / "rsync-started"
    monkeypatch.setenv(
        "DEVKIT_RSYNC",
        str(
            _fake_rsync(
                tmp_path / "bin-slow",
                tmp_path / "argv-slow.log",
                f'    touch "{marker}"\n    sleep 60',
            )
        ),
    )

    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool(
                "devkit_deploy",
                {"device": "deck", "gameid": GAMEID, "source_dir": str(_source_tree(tmp_path))},
            )
            job_id = _structured(result)["job_id"]
            deadline = asyncio.get_running_loop().time() + 10
            while not marker.exists():
                if asyncio.get_running_loop().time() > deadline:
                    raise AssertionError("fake rsync never started")
                await asyncio.sleep(0.05)
            result = await client.call_tool("devkit_job_cancel", {"job_id": job_id})
            assert _structured(result)["ok"] is True
            info = await _wait_for_job(client, job_id)
            assert info["state"] == "cancelled"

    run_async(scenario())


def test_progress_notifications_bridged(
    server: MCPServer[None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deploy_fakes(monkeypatch)
    # a transfer that reports progress2 records, then lingers so the poller
    # observes the transferring phase before the job ends
    body = f'    cat "{FIXTURES / "rsync-progress2-success.txt"}"\n    sleep 2\n'
    monkeypatch.setenv(
        "DEVKIT_RSYNC",
        str(_fake_rsync(tmp_path / "bin-prog", tmp_path / "argv-prog.log", body)),
    )
    notifications: list[Any] = []

    async def scenario() -> None:
        async with Client(
            server, mode="legacy", message_handler=lambda m: _collect(m, notifications)
        ) as client:
            result = await client.call_tool(
                "devkit_deploy",
                {"device": "deck", "gameid": GAMEID, "source_dir": str(_source_tree(tmp_path))},
                meta={"progressToken": "mcp-test-token"},
            )
            job_id = _structured(result)["job_id"]
            info = await _wait_for_job(client, job_id)
            assert info["state"] == "succeeded"

    run_async(scenario())
    progress_notes = [n for n in notifications if type(n).__name__ == "ProgressNotification"]
    assert progress_notes, "no notifications/progress arrived"
    tokens = {n.params.progress_token for n in progress_notes}
    assert tokens == {"mcp-test-token"}
    # journal phases surface as progress messages
    messages = {n.params.message for n in progress_notes}
    assert messages & {"preparing", "transferring", "registering"}


def test_gpu_trace_job_lands_artifact(
    server: MCPServer[None], monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"fake-gpu-trace-zip"
    make_paramiko_patch(
        monkeypatch,
        commands=[
            (r"mktemp -p /tmp", b"/tmp/AAAA-trace.zip\n", b"", 0),
            (r"gpu-trace", b"", b"", 0),
            (r"rm", b"", b"", 0),
        ],
        filesystem={"/tmp/AAAA-trace.zip": payload},
    )

    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool("devkit_gpu_trace", {"device": "deck"})
            job_id = _structured(result)["job_id"]
            info = await _wait_for_job(client, job_id)
            assert info["state"] == "succeeded"
            assert len(info["artifacts"]) == 1

            result = await client.call_tool("devkit_list_artifacts", {"kind": "gpu_trace"})
            entries = _structured(result)["result"]
            assert len(entries) == 1
            entry = entries[0]
            assert entry["artifact"]["id"] == info["artifacts"][0]
            assert entry["resource_uri"] == f"devkit://artifact/{info['artifacts'][0]}"

            # binary kind: the resource serves the payload bytes as a blob
            read = await client.read_resource(entry["resource_uri"])
            blob = read.contents[0].blob
            assert base64.b64decode(blob) == payload
            assert read.contents[0].mime_type == "application/zip"

    run_async(scenario())


def test_diagnostics_tools_start_jobs(
    server: MCPServer[None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three diagnostics tools are lazy bridges into steamos_devkit.diagnostics;
    the seam is patched here so the test never depends on M3's internals."""
    import steamos_devkit.diagnostics as diagnostics

    monkeypatch.setattr(
        diagnostics,
        "fetch_logs",
        lambda core, sel, kinds=None, timeout_s=600: core.job_runner.submit(
            lambda ctx: "logs ok", kind=JobKind.FETCH_LOGS, device=sel
        ),
    )
    monkeypatch.setattr(
        diagnostics,
        "screenshot",
        lambda core, sel, mode=3, timeout_s=60: core.job_runner.submit(
            lambda ctx: "shot ok", kind=JobKind.SCREENSHOT, device=sel
        ),
    )
    monkeypatch.setattr(
        diagnostics,
        "controller_config",
        lambda core, sel, gameid, timeout_s=60: core.job_runner.submit(
            lambda ctx: "vdf ok", kind=JobKind.CONTROLLER_CONFIG, device=sel
        ),
    )

    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            for tool, args in (
                ("devkit_fetch_logs", {"device": "deck", "kinds": ["steam", "dumps"]}),
                ("devkit_screenshot", {"device": "deck", "mode": 2}),
                ("devkit_controller_config", {"device": "deck", "gameid": GAMEID}),
            ):
                result = await client.call_tool(tool, args)
                started = _structured(result)
                assert started["status"] == "queued"
                info = await _wait_for_job(client, started["job_id"])
                assert info["state"] == "succeeded", tool

    run_async(scenario())


# --- artifacts ------------------------------------------------------------------------


def _register(state_dir: Path, content: bytes, *, kind: str, media_type: str | None) -> Any:
    source = state_dir / "staging-payload"
    source.write_bytes(content)
    artifact = register_artifact(
        StateStore(state_dir),
        job_id=f"job_{ulid.new()}",
        device="deck",
        kind=kind,
        source=source,
        remote_source="/remote/source",
        media_type=media_type,
    )
    return artifact


def test_artifact_resource_text_is_bounded(prepared: Path, server: MCPServer[None]) -> None:
    lines = [f"log line {i} " + "x" * 40 for i in range(3000)]
    artifact = _register(
        prepared, ("\n".join(lines)).encode(), kind="logs", media_type="text/plain"
    )

    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            read = await client.read_resource(f"devkit://artifact/{artifact.id}")
            content = read.contents[0]
            assert content.mime_type == "text/plain"
            text = content.text
            # bounded: at most 2000 lines / 64 KiB (spec section 11)
            assert len(text.encode()) <= 64 * 1024
            assert len(text.splitlines()) <= 2000
            assert text.endswith(lines[-1])  # a tail, not the head

            result = await client.call_tool("devkit_read_artifact", {"artifact_id": artifact.id})
            payload = _structured(result)
            assert payload["resource_uri"] == f"devkit://artifact/{artifact.id}"
            assert payload["excerpt"].endswith(lines[-1])

    run_async(scenario())


def test_artifact_resource_binary_and_unknown(prepared: Path, server: MCPServer[None]) -> None:
    png = b"\x89PNG\r\n\x1a\nfakepixels"
    artifact = _register(prepared, png, kind="screenshot", media_type="image/png")

    async def scenario() -> None:
        async with Client(server, mode="legacy") as client:
            read = await client.read_resource(f"devkit://artifact/{artifact.id}")
            content = read.contents[0]
            assert content.mime_type == "image/png"
            assert base64.b64decode(content.blob) == png

            # read_artifact on a binary kind returns the link, not decoded text
            result = await client.call_tool("devkit_read_artifact", {"artifact_id": artifact.id})
            payload = _structured(result)
            assert payload["excerpt"] is None
            assert payload["resource_uri"] == f"devkit://artifact/{artifact.id}"

            # unknown artifacts are a protocol-level invalid-params error
            from mcp.shared.exceptions import MCPError

            with pytest.raises(MCPError):
                await client.read_resource("devkit://artifact/art_01ARZ3NDEKTSV4RRFFQ69G5FAV")

    run_async(scenario())


# --- hygiene: no secrets in results -----------------------------------------------------


def test_no_key_material_or_magic_phrase_in_results(
    prepared: Path, server: MCPServer[None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("getpass.getuser", lambda: "testuser")
    monkeypatch.setattr("socket.gethostname", lambda: "testhost")
    properties = (FIXTURES / "properties.json").read_bytes()
    monkeypatch.setattr(
        pairing,
        "_urlopen",
        lambda request, timeout: (
            properties if request.full_url.endswith("/properties.json") else b""
        ),
    )
    make_paramiko_patch(monkeypatch, commands=[(r"steamos-get-status --json", STATUS_JSON, b"", 0)])

    async def scenario() -> str:
        blob: list[str] = []
        async with Client(server, mode="legacy") as client:
            for tool, args in (
                ("devkit_pair", {"device": "192.0.2.77"}),
                ("devkit_status", {"device": "deck"}),
                ("devkit_list_devices", {}),
                ("devkit_doctor", {}),
                ("devkit_capabilities", {}),
            ):
                result = await client.call_tool(tool, args)
                blob.append(json.dumps(result.structured_content, default=str))
                blob.extend(getattr(c, "text", "") for c in result.content)
            read = await client.read_resource("devkit://devices")
            blob.append(read.contents[0].text)
            # errors are results too — include one
            monkeypatch.setattr(
                "steamos_devkit.discovery.resolve_name", lambda core, name, **kw: None
            )
            result = await client.call_tool("devkit_status", {"device": "ghost"})
            blob.append(result.content[0].text)
        return "\n".join(blob)

    serialized = run_async(scenario())
    private_key_text = (prepared / "keys" / "devkit_rsa").read_text(encoding="utf-8")
    assert pairing.MAGIC_PHRASE not in serialized
    assert "PRIVATE KEY" not in serialized
    assert private_key_text not in serialized


# --- CLI wiring --------------------------------------------------------------------------


def test_cli_mcp_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["mcp", "--help"])
    assert result.exit_code == 0


def test_cli_mcp_runs_server_with_state_dir(
    prepared: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`devkit mcp` runs the stdio server, honoring --state-dir (blocking, so
    the run itself is stubbed; the real stdio loop is covered below)."""
    from steamos_devkit import mcp_server

    monkeypatch.delenv("DEVKIT_CONTEXT", raising=False)  # the command sets it; keep tests hermetic
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        mcp_server, "run_stdio", lambda state_dir=None: seen.update(state_dir=state_dir)
    )
    runner = CliRunner()
    result = runner.invoke(cli.app, ["--state-dir", str(prepared), "mcp"])
    assert result.exit_code == 0
    assert seen == {"state_dir": prepared}
    assert os.environ.get("DEVKIT_CONTEXT") == "mcp_stdio"


def test_console_script_run_invokes_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `devkit-mcp` entry point runs the stdio server with default state."""
    from steamos_devkit import mcp_server
    from steamos_devkit.cli import mcp as cli_mcp

    monkeypatch.delenv("DEVKIT_CONTEXT", raising=False)
    called: list[Path | None] = []
    monkeypatch.setattr(mcp_server, "run_stdio", lambda state_dir=None: called.append(state_dir))
    cli_mcp.run()
    assert called == [None]
    assert os.environ.get("DEVKIT_CONTEXT") == "mcp_stdio"


def test_console_script_stdio_handshake(prepared: Path) -> None:
    """The `devkit-mcp` entry point speaks MCP over real stdio pipes."""
    env = {**os.environ, "DEVKIT_STATE_DIR": str(prepared)}
    proc = subprocess.Popen(
        [sys.executable, "-c", "from steamos_devkit.cli.mcp import run; run()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"},
                    },
                }
            )
            + "\n"
        )
        proc.stdin.flush()
        ready = select.select([proc.stdout], [], [], 15)
        assert ready[0], "server did not answer initialize within 15s"
        response = json.loads(proc.stdout.readline())
        assert response["result"]["serverInfo"]["name"] == "steamos-devkit"
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
        proc.stdin.flush()
        ready = select.select([proc.stdout], [], [], 15)
        assert ready[0], "server did not answer tools/list within 15s"
        response = json.loads(proc.stdout.readline())
        assert {t["name"] for t in response["result"]["tools"]} == EXPECTED_TOOLS
    finally:
        proc.kill()
        proc.wait(timeout=10)
