"""Extra unit tests for device.py — external-review fixes.

Covers: the ensure_utils process-lifetime cache (M3), the default
remote-command timeout and ensure_utils timeout mapping (M7), the public
``DeviceHandle.core`` accessor (L7), and ``host:port`` selector parsing
(L5).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import paramiko
import pytest
from fake_ssh import make_paramiko_patch

from steamos_devkit import constants
from steamos_devkit.core import DevkitCore
from steamos_devkit.device import (
    _ENSURE_UTILS_TIMEOUT_S,
    REMOTE_COMMAND_TIMEOUT_S,
    DeviceHandle,
    resolve_device,
)
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Device


@pytest.fixture
def core(state_dir: Path) -> DevkitCore:
    return DevkitCore(state_dir=state_dir)


@pytest.fixture
def paired_key(core: DevkitCore) -> Path:
    core.state.ensure_layout()
    key_path = core.state.key_path
    paramiko.RSAKey.generate(2048).write_private_key_file(str(key_path))
    return key_path


def _registry_device(core: DevkitCore, name: str = "deck", address: str = "192.0.2.10") -> Device:
    device = Device(name=name, address=address, login="deck", added_via="explicit")
    core.state.devices.upsert(device)
    return device


def _fake_rsync_script(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "rsync"
    path.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'rsync  version 3.5.1  protocol version 33'\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _patch_run_local(monkeypatch: pytest.MonkeyPatch, calls: list[list[str]]) -> None:
    def fake(self: DeviceHandle, argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(DeviceHandle, "_run_local", fake)


# --- M3: ensure_utils syncs once per process per (state root, device) ----------


def test_ensure_utils_cached_across_handles_same_device(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync_script(core.state.root / "bin")))
    calls: list[list[str]] = []
    _patch_run_local(monkeypatch, calls)
    first = core.device_handle("deck")
    second = core.device_handle("deck")  # a second handle for the same device
    first.ensure_utils()
    second.ensure_utils()
    assert len(calls) == 1  # the rsync ran once, the second call was a cache hit


def test_ensure_utils_force_resyncs(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync_script(core.state.root / "bin")))
    calls: list[list[str]] = []
    _patch_run_local(monkeypatch, calls)
    handle = core.device_handle("deck")
    handle.ensure_utils()
    handle.ensure_utils(force=True)
    assert len(calls) == 2


def test_ensure_utils_different_device_resyncs(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    _registry_device(core, name="frame", address="192.0.2.20")
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync_script(core.state.root / "bin")))
    calls: list[list[str]] = []
    _patch_run_local(monkeypatch, calls)
    core.device_handle("deck").ensure_utils()
    core.device_handle("frame").ensure_utils()
    assert len(calls) == 2


def test_ensure_utils_different_state_root_resyncs(
    state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync_script(tmp_path / "bin")))
    calls: list[list[str]] = []
    _patch_run_local(monkeypatch, calls)
    cores = []
    for i in range(2):
        root = tmp_path / f"state-{i}"
        root.mkdir()
        c = DevkitCore(state_dir=root)
        c.state.ensure_layout()
        paramiko.RSAKey.generate(2048).write_private_key_file(str(c.state.key_path))
        _registry_device(c)
        cores.append(c)
    cores[0].device_handle("deck").ensure_utils()
    cores[1].device_handle("deck").ensure_utils()  # same address, different state root
    assert len(calls) == 2


def test_ensure_utils_failed_sync_is_not_cached(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync_script(core.state.root / "bin")))
    calls: list[list[str]] = []
    outcomes = [1, 0]  # first sync fails, the retry must actually run

    def flaky(self: DeviceHandle, argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, outcomes.pop(0), "", "boom")

    monkeypatch.setattr(DeviceHandle, "_run_local", flaky)
    handle = core.device_handle("deck")
    with pytest.raises(DevkitError) as exc_info:
        handle.ensure_utils()
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    handle.ensure_utils()  # retries instead of hitting a poisoned cache
    assert len(calls) == 2


# --- M7: default remote-command timeout -----------------------------------------


def test_run_applies_default_timeout(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    fake = make_paramiko_patch(monkeypatch, commands=[(r"uname", b"Linux\n", b"", 0)])
    core.device_handle("deck").run("uname -a")
    assert fake.command_records[0]["timeout"] == REMOTE_COMMAND_TIMEOUT_S == 120


def test_run_explicit_timeout_preserved(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    fake = make_paramiko_patch(monkeypatch, commands=[(r"uname", b"Linux\n", b"", 0)])
    core.device_handle("deck").run("uname -a", timeout=7)
    assert fake.command_records[0]["timeout"] == 7


def test_run_json_applies_default_timeout(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    fake = make_paramiko_patch(monkeypatch, commands=[(r"get-status", b"{}", b"", 0)])
    core.device_handle("deck").run_json("get-status --json")
    assert fake.command_records[0]["timeout"] == REMOTE_COMMAND_TIMEOUT_S


def test_run_local_timeout_maps_to_devkit_timeout(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync_script(core.state.root / "bin")))
    seen: dict[str, object] = {}

    def hang(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in argv:  # the adapter's rsync probe still answers
            return subprocess.CompletedProcess(
                argv, 0, "rsync  version 3.5.1  protocol version 33\n", ""
            )
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(cmd=argv, timeout=_ENSURE_UTILS_TIMEOUT_S)

    monkeypatch.setattr("steamos_devkit.device.subprocess.run", hang)
    with pytest.raises(DevkitError) as exc_info:
        core.device_handle("deck").ensure_utils()
    assert exc_info.value.code == ErrorCode.TIMEOUT
    assert seen["timeout"] == _ENSURE_UTILS_TIMEOUT_S == 300


# --- L7: public core accessor -----------------------------------------------------


def test_handle_core_property(core: DevkitCore) -> None:
    _registry_device(core)
    handle = core.device_handle("deck")
    assert handle.core is core


# --- L5: host:port selector parsing -------------------------------------------------


def test_resolve_ipv4_with_port(core: DevkitCore) -> None:
    resolved = resolve_device(core, "192.0.2.10:32001")
    assert resolved.address == "192.0.2.10"
    assert resolved.port == 32001
    assert resolved.name == "192.0.2.10:32001"
    assert resolved.added_via == "explicit"
    assert resolved.login == "deck"


def test_resolve_ipv4_without_port_defaults(core: DevkitCore) -> None:
    resolved = resolve_device(core, "192.0.2.10")
    assert resolved.port == constants.DEVICE_HTTP_PORT_DEFAULT == 32000


@pytest.mark.parametrize("port", ["0", "99999", "-1", "65536"])
def test_resolve_ipv4_port_out_of_range_rejected(core: DevkitCore, port: str) -> None:
    with pytest.raises(DevkitError) as exc_info:
        resolve_device(core, f"192.0.2.10:{port}")
    assert exc_info.value.code == ErrorCode.VALIDATION


@pytest.mark.parametrize("port", ["abc", ""])
def test_resolve_ipv4_port_not_a_number_rejected(core: DevkitCore, port: str) -> None:
    with pytest.raises(DevkitError) as exc_info:
        resolve_device(core, f"192.0.2.10:{port}")
    assert exc_info.value.code == ErrorCode.VALIDATION


def test_resolve_ipv4_port_boundary_accepted(core: DevkitCore) -> None:
    assert resolve_device(core, "192.0.2.10:1").port == 1
    assert resolve_device(core, "192.0.2.10:65535").port == 65535
