"""Extra unit tests for status.py — L6: registry family refresh.

A successful get_status of a *registered* device refreshes ``family``
("frame" when is_deckard else "deck") alongside ``last_seen``; transient
devices stay unregistered.
"""

from __future__ import annotations

from pathlib import Path

import paramiko
import pytest
from fake_ssh import make_paramiko_patch

from steamos_devkit import status
from steamos_devkit.core import DevkitCore
from steamos_devkit.device import DeviceHandle
from steamos_devkit.models import Device

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(autouse=True)
def skip_utils_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DeviceHandle, "ensure_utils", lambda self, force=False: None)


@pytest.fixture
def core(state_dir: Path) -> DevkitCore:
    core = DevkitCore(state_dir=state_dir)
    core.state.ensure_layout()
    paramiko.RSAKey.generate(2048).write_private_key_file(str(core.state.key_path))
    return core


def _register(core: DevkitCore, name: str, address: str) -> Device:
    device = Device(name=name, address=address, login="deck", added_via="explicit")
    core.state.devices.upsert(device)
    return device


def test_status_refreshes_family_deck(core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
    _register(core, "deck", "192.0.2.10")
    assert core.state.devices.get("deck") is not None
    assert core.state.devices.get("deck").family == "unknown"  # type: ignore[union-attr]
    payload = (FIXTURES / "steamos-get-status.json").read_bytes()
    make_paramiko_patch(monkeypatch, commands=[(r"steamos-get-status --json", payload, b"", 0)])
    status.get_status(core, "deck")
    entry = core.state.devices.get("deck")
    assert entry is not None
    assert entry.family == "deck"
    assert entry.last_seen is not None


def test_status_refreshes_family_frame(core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
    _register(core, "frame", "192.0.2.20")
    payload = (FIXTURES / "steamos-get-status-deckard.json").read_bytes()
    make_paramiko_patch(monkeypatch, commands=[(r"steamos-get-status --json", payload, b"", 0)])
    result = status.get_status(core, "frame")
    assert result.is_deckard is True
    entry = core.state.devices.get("frame")
    assert entry is not None
    assert entry.family == "frame"
    assert entry.last_seen is not None


def test_status_transient_device_registers_nothing(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = (FIXTURES / "steamos-get-status.json").read_bytes()
    make_paramiko_patch(monkeypatch, commands=[(r"steamos-get-status --json", payload, b"", 0)])
    status.get_status(core, "192.0.2.77")  # explicit IP, no registry entry
    assert core.state.devices.list() == []
