"""Extra unit tests for discovery.py — L4: zeroconf failure classification.

Only permission-shaped socket errors (EPERM/EACCES — the macOS Local
Network/TCC signature on multicast sendto) map to PERMISSION_LOCAL_NETWORK;
other OS-level failures are DEVICE_UNREACHABLE without claiming a TCC block,
and non-OS failures are INTERNAL.
"""

from __future__ import annotations

import errno
from pathlib import Path
from typing import Any, ClassVar

import pytest

from steamos_devkit import discovery
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode


class _FailingZeroconf:
    """zeroconf.Zeroconf stand-in whose constructor raises a canned error."""

    construct_error: ClassVar[BaseException | None] = None

    def __init__(self) -> None:
        assert _FailingZeroconf.construct_error is not None
        raise _FailingZeroconf.construct_error

    @classmethod
    def reset(cls) -> None:
        cls.construct_error = None


@pytest.fixture(autouse=True)
def fake_zeroconf(monkeypatch: pytest.MonkeyPatch) -> None:
    _FailingZeroconf.reset()
    monkeypatch.setattr(discovery.zeroconf, "Zeroconf", _FailingZeroconf)


@pytest.fixture
def core(state_dir: Path) -> DevkitCore:
    return DevkitCore(state_dir=state_dir)


@pytest.mark.parametrize("err", [errno.EPERM, errno.EACCES])
def test_permission_shaped_errors_map_to_permission_local_network(
    core: DevkitCore, err: int
) -> None:
    _FailingZeroconf.construct_error = PermissionError(err, "denied")
    with pytest.raises(DevkitError) as exc_info:
        discovery.discover(core, timeout_s=0.01)
    assert exc_info.value.code == ErrorCode.PERMISSION_LOCAL_NETWORK


@pytest.mark.parametrize(
    "err",
    [errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EADDRNOTAVAIL],
)
def test_network_failures_map_to_device_unreachable(core: DevkitCore, err: int) -> None:
    _FailingZeroconf.construct_error = OSError(err, "no route")
    with pytest.raises(DevkitError) as exc_info:
        discovery.discover(core, timeout_s=0.01)
    assert exc_info.value.code == ErrorCode.DEVICE_UNREACHABLE
    # no TCC claim without a permission errno
    assert "Local Network" not in exc_info.value.message


def test_os_error_without_errno_is_unreachable_not_permission(core: DevkitCore) -> None:
    _FailingZeroconf.construct_error = OSError("no multicast route")
    with pytest.raises(DevkitError) as exc_info:
        discovery.discover(core, timeout_s=0.01)
    assert exc_info.value.code == ErrorCode.DEVICE_UNREACHABLE
    assert "Local Network" not in exc_info.value.message


def test_non_os_failure_maps_to_internal(core: DevkitCore) -> None:
    _FailingZeroconf.construct_error = RuntimeError("zeroconf exploded")
    with pytest.raises(DevkitError) as exc_info:
        discovery.discover(core, timeout_s=0.01)
    assert exc_info.value.code == ErrorCode.INTERNAL


def test_resolve_device_propagates_narrowed_codes(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare-name mDNS fallback surfaces the classified error unchanged."""

    def _offline(c: DevkitCore, name: str, **kw: Any) -> None:
        raise DevkitError(ErrorCode.DEVICE_UNREACHABLE, "offline")

    monkeypatch.setattr(discovery, "resolve_name", _offline)
    with pytest.raises(DevkitError) as exc_info:
        from steamos_devkit.device import resolve_device

        resolve_device(core, "livingroom")
    assert exc_info.value.code == ErrorCode.DEVICE_UNREACHABLE
