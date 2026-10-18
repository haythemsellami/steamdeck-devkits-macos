"""Unit tests for device resolution and the SSH substrate (spec section 8)."""

import json
import subprocess
from pathlib import Path

import paramiko
import pytest
from fake_ssh import make_paramiko_patch

from steamos_devkit import constants
from steamos_devkit.core import DevkitCore
from steamos_devkit.device import resolve_device
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Device


@pytest.fixture
def core(state_dir: Path) -> DevkitCore:
    return DevkitCore(state_dir=state_dir)


@pytest.fixture
def paired_key(core: DevkitCore) -> Path:
    """A real RSA key at the expected state location."""
    core.state.ensure_layout()
    key_path = core.state.key_path
    paramiko.RSAKey.generate(2048).write_private_key_file(str(key_path))
    return key_path


def _registry_device(core: DevkitCore, name: str = "deck") -> Device:
    device = Device(name=name, address="192.0.2.10", login="deck", added_via="explicit")
    core.state.devices.upsert(device)
    return device


def test_resolve_from_registry(core: DevkitCore) -> None:
    _registry_device(core)
    resolved = resolve_device(core, "deck")
    assert resolved.address == "192.0.2.10"


def test_resolve_ipv4_literal_is_transient(core: DevkitCore) -> None:
    resolved = resolve_device(core, "192.0.2.55")
    assert resolved.name == "192.0.2.55"
    assert resolved.added_via == "explicit"
    assert resolved.login == "deck"


def test_resolve_hostname_with_dot(core: DevkitCore) -> None:
    resolved = resolve_device(core, "deck.local")
    assert resolved.added_via == "explicit"


def test_resolve_unknown_name_fails(core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep mDNS out of unit tests: simulate a discovery miss.
    monkeypatch.setattr("steamos_devkit.discovery.resolve_name", lambda *a, **kw: None)
    with pytest.raises(DevkitError) as exc_info:
        resolve_device(core, "livingroom")
    assert exc_info.value.code == ErrorCode.DEVICE_NOT_FOUND


def test_connect_without_key_fails(core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
    _registry_device(core)
    make_paramiko_patch(monkeypatch)
    handle = core.device_handle("deck")
    with pytest.raises(DevkitError) as exc_info:
        handle.run("true")
    assert exc_info.value.code == ErrorCode.PAIRING_FAILED


def test_run_roundtrip(core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _registry_device(core)
    fake = make_paramiko_patch(monkeypatch, commands=[(r"uname -a", b"Linux deck\n", b"", 0)])
    handle = core.device_handle("deck")
    result = handle.run("uname -a")
    assert result.exit_code == 0
    assert result.stdout == "Linux deck\n"
    connect = fake.connect_records[0]
    assert connect["hostname"] == "192.0.2.10"
    assert connect["username"] == "deck"
    assert connect["look_for_keys"] is False


def test_run_nonzero_raises_with_remote_code(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    make_paramiko_patch(monkeypatch, commands=[(r"false", b"", b"nope\n", 3)])
    handle = core.device_handle("deck")
    with pytest.raises(DevkitError) as exc_info:
        handle.run("false")
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    assert exc_info.value.remote_exit_code == 3
    assert "nope" in exc_info.value.details["stderr_tail"]


def test_run_check_false_returns_code(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    make_paramiko_patch(monkeypatch, commands=[(r"false", b"", b"", 3)])
    result = core.device_handle("deck").run("false", check=False)
    assert result.exit_code == 3


def test_run_json(core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _registry_device(core)
    payload = json.dumps({"steam_status": "SteamStatus.OS"}).encode()
    make_paramiko_patch(monkeypatch, commands=[(r"get-status", payload, b"", 0)])
    parsed = core.device_handle("deck").run_json("get-status --json")
    assert parsed["steam_status"] == "SteamStatus.OS"


def test_run_json_invalid(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    make_paramiko_patch(monkeypatch, commands=[(r"get-status", b"not json", b"", 0)])
    with pytest.raises(DevkitError) as exc_info:
        core.device_handle("deck").run_json("get-status --json")
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED


def test_sftp_get(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _registry_device(core)
    make_paramiko_patch(monkeypatch, filesystem={"/tmp/shot.png": b"\x89PNG fake"})
    dest = tmp_path / "shot.png"
    core.device_handle("deck").sftp_get("/tmp/shot.png", dest)
    assert dest.read_bytes() == b"\x89PNG fake"


def _fake_rsync_script(directory: Path, version_output: str) -> Path:
    directory.mkdir(exist_ok=True)
    path = directory / "rsync"
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version_output}'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_ensure_utils_builds_upstream_rsync_argv(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    monkeypatch.setenv(
        constants.RSYNC_ENV_VAR,
        str(
            _fake_rsync_script(core.state.root / "bin", "rsync  version 3.5.1  protocol version 33")
        ),
    )
    handle = core.device_handle("deck")
    calls: list[list[str]] = []

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(handle, "_run_local", fake_run)
    handle.ensure_utils()
    (argv,) = calls
    assert argv[1] == "-a"
    assert "--chmod=Du=rwx,Dgo=rx,Fu=rwx,Fog=rx" in argv
    assert "devkit-utils/" in argv[-1]
    assert argv[-1].startswith("deck@192.0.2.10:")


def test_ensure_utils_rejects_openrsync(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    monkeypatch.setenv(
        constants.RSYNC_ENV_VAR,
        str(_fake_rsync_script(core.state.root / "bin", "openrsync: protocol version 29")),
    )
    with pytest.raises(DevkitError) as exc_info:
        core.device_handle("deck").ensure_utils()
    assert exc_info.value.code == ErrorCode.OPENRSYNC_DETECTED


def test_ensure_utils_failure_maps_remote_failed(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    monkeypatch.setenv(
        constants.RSYNC_ENV_VAR,
        str(
            _fake_rsync_script(core.state.root / "bin", "rsync  version 3.5.1  protocol version 33")
        ),
    )
    handle = core.device_handle("deck")
    monkeypatch.setattr(
        handle,
        "_run_local",
        lambda argv: subprocess.CompletedProcess(argv, 23, "", "partial transfer"),
    )
    with pytest.raises(DevkitError) as exc_info:
        handle.ensure_utils()
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    assert exc_info.value.remote_exit_code == 23
