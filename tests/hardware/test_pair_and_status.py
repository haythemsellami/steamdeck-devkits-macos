"""Pair (idempotent), status shape, and the capabilities operations matrix.

Covers hardware-matrix rows "Pair" and "Status" plus the capabilities probe
that gates honest operation reporting (spec section 9).
"""

from __future__ import annotations

from collections.abc import Callable

from steamos_devkit import capabilities as capabilities_mod
from steamos_devkit import pairing
from steamos_devkit import status as status_mod
from steamos_devkit.core import DevkitCore

HwRecord = Callable[..., None]

_STEAM_STATUSES = ("NOT_RUNNING", "ERROR", "OS", "OS_DEV", "SIDELOADED", "UNKNOWN")


def test_pair_idempotent(core: DevkitCore, device_selector: str, hwrecord: HwRecord) -> None:
    first = pairing.pair(core, device_selector)
    assert first.ok, f"pairing failed: {first.message}"
    assert first.device is not None
    # Re-pairing with the same key must be accepted without another modal.
    second = pairing.pair(core, device_selector)
    assert second.ok, f"re-pair was not idempotent: {second.message}"
    hwrecord("pair", "pass", device=first.device.name, address=first.device.address)


def test_status_shape(core: DevkitCore, device_selector: str, hwrecord: HwRecord) -> None:
    report = status_mod.get_status(core, device_selector)
    assert report.hostname
    assert report.os_name
    assert report.steam_status in _STEAM_STATUSES
    hwrecord(
        "status",
        "pass",
        steam_status=report.steam_status,
        os_version=report.os_version,
        family="frame" if report.is_deckard else "deck",
    )


def test_capabilities_operations_matrix(
    core: DevkitCore, device_selector: str, hwrecord: HwRecord
) -> None:
    caps = capabilities_mod.capabilities(core, device_selector)
    assert caps.device is not None
    expected = {
        "status",
        "deploy",
        "preview_deploy",
        "register_title",
        "launch_title",
        "delete_title",
        "fetch_logs",
        "screenshot",
        # never faked (spec section 9)
        "stop_title",
        "launch_readiness",
        "title_stdout_streaming",
    }
    missing = expected - set(caps.operations)
    assert not missing, f"operations matrix incomplete: {missing}"
    for name in ("stop_title", "launch_readiness", "title_stdout_streaming"):
        assert caps.operations[name].status == "unsupported_upstream"
    # Nothing may claim supported_tested without a green hardware-matrix row.
    assert all(op.status != "supported_tested" for op in caps.operations.values())
    hwrecord("capabilities", "pass", operations=len(caps.operations), family=caps.device.family)
