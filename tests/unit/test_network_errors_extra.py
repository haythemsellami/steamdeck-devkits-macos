"""Errno-65 (EHOSTUNREACH) on darwin = possible Local Network (TCC) denial.

Verified against a real Deck on macOS 26: unsigned processes get errno 65 on
LAN connects while Apple system tools pass. The mapper must present both
causes (device offline vs permission) and use PERMISSION_LOCAL_NETWORK.
"""

from __future__ import annotations

import errno
import sys
import urllib.error
from pathlib import Path

import pytest

from steamos_devkit.core import DevkitCore
from steamos_devkit.discovery import _discovery_error
from steamos_devkit.errors import DevkitError, ErrorCode, map_network_oserror
from steamos_devkit.pairing import fetch_properties

EHOSTUNREACH = errno.EHOSTUNREACH


def test_mapper_errno65_on_darwin() -> None:
    err = map_network_oserror(OSError(EHOSTUNREACH, "No route to host"), target="192.0.2.10:32000")
    assert err.code == ErrorCode.PERMISSION_LOCAL_NETWORK
    assert "Local Network" in err.message
    assert "offline" in err.message  # both causes presented honestly


def test_mapper_unwraps_urlerror_reason() -> None:
    wrapped = urllib.error.URLError(OSError(EHOSTUNREACH, "No route to host"))
    err = map_network_oserror(wrapped, target="192.0.2.10:32000")  # type: ignore[arg-type]
    assert err.code == ErrorCode.PERMISSION_LOCAL_NETWORK


def test_mapper_other_errors_stay_unreachable() -> None:
    err = map_network_oserror(OSError(errno.ECONNREFUSED, "refused"), target="192.0.2.10:22")
    assert err.code == ErrorCode.DEVICE_UNREACHABLE


def test_mapper_errno65_off_darwin_stays_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    err = map_network_oserror(OSError(EHOSTUNREACH, "No route to host"), target="192.0.2.10")
    assert err.code == ErrorCode.DEVICE_UNREACHABLE


def test_fetch_properties_errno65_maps_permission(
    monkeypatch: pytest.MonkeyPatch, state_dir: Path
) -> None:
    monkeypatch.setattr(
        "steamos_devkit.pairing._urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(
            urllib.error.URLError(OSError(EHOSTUNREACH, "No route to host"))
        ),
    )
    with pytest.raises(DevkitError) as exc_info:
        fetch_properties("192.0.2.10", 32000, 5)
    assert exc_info.value.code == ErrorCode.PERMISSION_LOCAL_NETWORK


def test_device_connect_errno65_maps_permission(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import paramiko

    core = DevkitCore(state_dir=state_dir)
    core.state.ensure_layout()
    paramiko.RSAKey.generate(2048).write_private_key_file(str(core.state.key_path))
    handle = core.device_handle("192.0.2.10")

    class _Fail:
        def set_missing_host_key_policy(self, policy: object) -> None:
            pass

        def connect(self, *a: object, **kw: object) -> None:
            raise OSError(EHOSTUNREACH, "No route to host")

    monkeypatch.setattr("paramiko.SSHClient", lambda *a, **kw: _Fail())
    with pytest.raises(DevkitError) as exc_info:
        handle.run("true")
    assert exc_info.value.code == ErrorCode.PERMISSION_LOCAL_NETWORK


def test_discovery_errno65_on_darwin_is_permission(state_dir: Path) -> None:
    core = DevkitCore(state_dir=state_dir)
    err = _discovery_error(core, OSError(EHOSTUNREACH, "No route to host"))
    if sys.platform == "darwin":
        assert err.code == ErrorCode.PERMISSION_LOCAL_NETWORK
    else:
        assert err.code == ErrorCode.DEVICE_UNREACHABLE


def test_discovery_errno65_off_darwin_is_unreachable(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    core = DevkitCore(state_dir=state_dir)
    err = _discovery_error(core, OSError(EHOSTUNREACH, "No route to host"))
    assert err.code == ErrorCode.DEVICE_UNREACHABLE
