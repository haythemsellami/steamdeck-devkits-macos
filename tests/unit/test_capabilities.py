"""Unit tests for the capabilities matrix (spec section 9).

Host probes use scripted rsync binaries; device probes use the fake SSH layer
plus the steamos-get-status fixtures. Nothing is ever supported_tested here:
that status is earned only via docs/hardware-matrix.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fake_ssh import make_paramiko_patch

from steamos_devkit import capabilities, constants
from steamos_devkit.core import DevkitCore
from steamos_devkit.device import DeviceHandle
from steamos_devkit.models import Capabilities, Device

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

RSYNC_GOOD = "rsync  version 3.5.1  protocol version 35\\n"
OPENRSYNC = "openrsync: protocol version 29\\n"

ALL_OPERATIONS = {
    # host-local
    "doctor", "discover", "list_devices", "forget_device", "pair", "capabilities",
    "job_status", "job_cancel", "list_jobs", "list_artifacts", "read_artifact",
    "migrate", "version", "shell",
    # device operations the upstream protocol supports
    "status", "list_titles", "inspect_title", "configure_title", "preview_deploy",
    "deploy", "register_title", "launch_title", "delete_title", "fetch_logs",
    "screenshot", "controller_config", "perf_overlay", "frametimes",
    "renderdoc_layer", "renderdoc_server", "proton_logging", "set_steam_mode",
    "set_session", "reload_steam", "gpu_trace", "rgp_capture",
    # never-faked honest gaps (audit section 7)
    "stop_title", "launch_readiness", "title_stdout_streaming",
    # Windows-host-only upstream feature
    "vs_remote_debug",
}


@pytest.fixture(autouse=True)
def skip_utils_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DeviceHandle, "ensure_utils", lambda self: None)


@pytest.fixture
def core(state_dir: Path) -> DevkitCore:
    core = DevkitCore(state_dir=state_dir)
    core.state.ensure_layout()
    import paramiko

    paramiko.RSAKey.generate(2048).write_private_key_file(str(core.state.key_path))
    return core


def _fake_rsync(directory: Path, output: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "rsync"
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _register(core: DevkitCore, name: str = "deck", address: str = "192.0.2.10") -> Device:
    device = Device(name=name, address=address, login="deck", added_via="explicit")
    core.state.devices.upsert(device)
    return device


def _script_device(
    monkeypatch: pytest.MonkeyPatch,
    fixture: str,
    *,
    gpu_trace: bool = True,
    mangohudctl: bool = True,
    renderdoccmd: bool = True,
    extra_commands: list[tuple[str, bytes, bytes, int]] | None = None,
) -> None:
    commands: list[tuple[str, bytes, bytes, int]] = [
        (r"steamos-get-status --json", (FIXTURES / fixture).read_bytes(), b"", 0),
        (r"command -v gpu-trace", b"/usr/bin/gpu-trace\n" if gpu_trace else b"", b"", 0 if gpu_trace else 127),
        (r"command -v mangohudctl", b"/usr/bin/mangohudctl\n" if mangohudctl else b"", b"", 0 if mangohudctl else 127),
        (r"command -v renderdoccmd", b"/usr/bin/renderdoccmd\n" if renderdoccmd else b"", b"", 0 if renderdoccmd else 127),
    ]
    commands.extend(extra_commands or [])
    make_paramiko_patch(monkeypatch, commands=commands)


# --- host-only -----------------------------------------------------------------


def test_host_only_capabilities(core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync(core.state.root / "bin", RSYNC_GOOD)))
    caps = capabilities.capabilities(core)
    assert caps.device is None
    assert caps.host.platform == "darwin"
    assert caps.host.context == "cli"
    assert caps.host.rsync.ok is True
    assert caps.host.state_dir_writable is True
    assert caps.host.local_network == "unknown"  # TCC is not readable
    assert set(caps.operations) == ALL_OPERATIONS
    assert caps.operations["deploy"].status == "supported_untested"
    assert caps.operations["shell"].status == "supported_untested"
    # never tested until the hardware matrix records it
    assert not any(op.status == "supported_tested" for op in caps.operations.values())


def test_broken_rsync_marks_dependent_operations(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync(core.state.root / "bin", OPENRSYNC)))
    caps = capabilities.capabilities(core)
    for name in ("deploy", "preview_deploy", "fetch_logs", "status", "list_titles"):
        op = caps.operations[name]
        assert op.status == "dependency_required", name
        assert op.dependency == "rsync 3.x"
        assert "rsync" in (op.reason or "")
    # paramiko/SFTP-only operations do not depend on rsync
    assert caps.operations["gpu_trace"].status == "supported_untested"
    assert caps.operations["launch_title"].status == "supported_untested"
    assert caps.operations["pair"].status == "supported_untested"


def test_honest_gaps_are_unsupported_upstream(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync(core.state.root / "bin", RSYNC_GOOD)))
    caps = capabilities.capabilities(core)
    for name in ("stop_title", "launch_readiness", "title_stdout_streaming"):
        op = caps.operations[name]
        assert op.status == "unsupported_upstream", name
        assert op.reason is not None and "audit-upstream.md section 7" in op.reason
    vs = caps.operations["vs_remote_debug"]
    assert vs.status == "unavailable_host"
    assert vs.reason is not None and "Windows" in vs.reason


def test_capabilities_json_roundtrip(core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync(core.state.root / "bin", RSYNC_GOOD)))
    caps = capabilities.capabilities(core)
    assert Capabilities.model_validate_json(caps.model_dump_json()) == caps


# --- device probes ---------------------------------------------------------------


def test_deck_capabilities(core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync(core.state.root / "bin", RSYNC_GOOD)))
    _register(core)
    _script_device(monkeypatch, "steamos-get-status.json")
    caps = capabilities.capabilities(core, "deck")
    device = caps.device
    assert device is not None
    assert device.family == "deck"
    assert device.os_version == "20251022.100"
    assert device.steam_branch == "stable"
    assert device.steam_version == 1761150938
    assert device.has_gpu_trace is True
    assert device.has_mangohudctl is True
    assert device.session_select == "holo-session-select"  # reported by status raw
    assert device.cef_debugging_enabled is False
    assert caps.operations["gpu_trace"].status == "supported_untested"
    assert caps.operations["rgp_capture"].status == "supported_untested"
    assert caps.operations["perf_overlay"].status == "supported_untested"


def test_frame_disables_deck_only_captures(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync(core.state.root / "bin", RSYNC_GOOD)))
    _register(core, name="frame", address="192.0.2.20")
    _script_device(monkeypatch, "steamos-get-status-deckard.json")
    caps = capabilities.capabilities(core, "frame")
    assert caps.device is not None
    assert caps.device.family == "frame"
    assert caps.device.steam_version == 1760054400  # string builddate parsed to int
    for name in ("gpu_trace", "rgp_capture"):
        assert caps.operations[name].status == "unavailable_device", name
        assert "Deck-only" in (caps.operations[name].reason or "")


def test_missing_gpu_trace_binary_downgrades_captures(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync(core.state.root / "bin", RSYNC_GOOD)))
    _register(core)
    _script_device(monkeypatch, "steamos-get-status.json", gpu_trace=False)
    caps = capabilities.capabilities(core, "deck")
    assert caps.device is not None
    assert caps.device.has_gpu_trace is False
    assert caps.operations["gpu_trace"].status == "unavailable_device"
    assert caps.operations["gpu_trace"].reason == "gpu-trace not found on the device"


def test_missing_mangohudctl_downgrades_overlay_and_frametimes(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync(core.state.root / "bin", RSYNC_GOOD)))
    _register(core)
    _script_device(monkeypatch, "steamos-get-status.json", mangohudctl=False)
    caps = capabilities.capabilities(core, "deck")
    assert caps.device is not None
    assert caps.device.has_mangohudctl is False
    assert caps.operations["perf_overlay"].status == "unavailable_device"
    assert caps.operations["frametimes"].status == "unavailable_device"


def test_missing_renderdoccmd_downgrades_renderdoc_ops(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync(core.state.root / "bin", RSYNC_GOOD)))
    _register(core)
    _script_device(monkeypatch, "steamos-get-status.json", renderdoccmd=False)
    caps = capabilities.capabilities(core, "deck")
    assert caps.operations["renderdoc_layer"].status == "unavailable_device"
    assert caps.operations["renderdoc_server"].status == "unavailable_device"


def _minimal_status() -> bytes:
    return json.dumps(
        {
            "hostname": "olddeck",
            "os_name": "SteamOS",
            "os_version": "3.6.0",
            "is_deckard": False,
            "session_status": "gamescope",
            "steam_status": "SteamStatus.OS",
            "cef_debugging_enabled": False,
            "renderdoc_layer_enabled": False,
            "renderdoc_replay_server_running": False,
            "user_password_is_set": False,
        }
    ).encode()


def test_session_select_probed_when_status_lacks_it(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync(core.state.root / "bin", RSYNC_GOOD)))
    _register(core)
    make_paramiko_patch(
        monkeypatch,
        commands=[
            (r"steamos-get-status --json", _minimal_status(), b"", 0),
            (r"command -v holo-session-select", b"/usr/bin/holo-session-select\n", b"", 0),
        ],
    )
    caps = capabilities.capabilities(core, "deck")
    assert caps.device is not None
    assert caps.device.session_select == "holo-session-select"


def test_session_select_falls_back_to_legacy(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync(core.state.root / "bin", RSYNC_GOOD)))
    _register(core)
    make_paramiko_patch(
        monkeypatch,
        commands=[
            (r"steamos-get-status --json", _minimal_status(), b"", 0),
            # holo-session-select absent (127 default), legacy helper present
            (r"command -v steamos-session-select", b"/usr/bin/steamos-session-select\n", b"", 0),
        ],
    )
    caps = capabilities.capabilities(core, "deck")
    assert caps.device is not None
    assert caps.device.session_select == "steamos-session-select"


def test_session_select_unknown_when_neither_present(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync(core.state.root / "bin", RSYNC_GOOD)))
    _register(core)
    make_paramiko_patch(
        monkeypatch, commands=[(r"steamos-get-status --json", _minimal_status(), b"", 0)]
    )
    caps = capabilities.capabilities(core, "deck")
    assert caps.device is not None
    assert caps.device.session_select == "unknown"
