"""Unit tests for device status (spec sections 8 and 9).

SSH is always the scripted fake; the devkit-utils rsync upload is patched out
at ``DeviceHandle.ensure_utils``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fake_ssh import make_paramiko_patch

from steamos_devkit import status
from steamos_devkit.core import DevkitCore
from steamos_devkit.device import DeviceHandle
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Device, DeviceStatus

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(autouse=True)
def skip_utils_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DeviceHandle, "ensure_utils", lambda self: None)


@pytest.fixture
def core(state_dir: Path) -> DevkitCore:
    core = DevkitCore(state_dir=state_dir)
    core.state.ensure_layout()
    # a real key file so DeviceHandle.connect passes its existence check
    import paramiko

    paramiko.RSAKey.generate(2048).write_private_key_file(str(core.state.key_path))
    return core


def _register(core: DevkitCore, name: str = "deck", address: str = "192.0.2.10") -> Device:
    device = Device(name=name, address=address, login="deck", added_via="explicit")
    core.state.devices.upsert(device)
    return device


def test_status_parses_deck_fixture(core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
    _register(core)
    payload = (FIXTURES / "steamos-get-status.json").read_bytes()
    fake = make_paramiko_patch(
        monkeypatch, commands=[(r"steamos-get-status --json", payload, b"", 0)]
    )
    result = status.get_status(core, "deck")
    assert isinstance(result, DeviceStatus)
    assert result.hostname == "steamdeck"
    assert result.is_deckard is False
    assert result.steam_status == "OS"  # "SteamStatus.OS" prefix stripped
    assert result.steam_osclient_branch == "stable"
    assert result.steam_osclient_version == 1761150938  # int on Deck
    assert result.os_version == "20251022.100"
    assert result.session_status == "gamescope"
    assert result.steam_default_args == ["-steamos3", "-steampal", "-steamdeck", "-gamepadui"]
    # unparsed extras are preserved verbatim
    assert result.raw["session_select"] == "holo-session-select"
    assert result.raw["steam_configuration"] == "SteamConfig.OS"
    assert isinstance(result.raw["os_info"], dict)
    # the exact helper invocation (audit section 4)
    assert fake.commands_run == ["python3 ~/devkit-utils/steamos-get-status --json"]
    # registry last_seen was refreshed
    assert core.state.devices.get("deck") is not None
    assert core.state.devices.get("deck").last_seen is not None  # type: ignore[union-attr]


def test_status_parses_deckard_fixture(core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
    _register(core, name="frame", address="192.0.2.20")
    payload = (FIXTURES / "steamos-get-status-deckard.json").read_bytes()
    make_paramiko_patch(monkeypatch, commands=[(r"steamos-get-status --json", payload, b"", 0)])
    result = status.get_status(core, "frame")
    assert result.hostname == "steamframe"
    assert result.is_deckard is True
    assert result.steam_osclient_version == "1760054400"  # raw string on Frame
    assert result.steam_default_args == []


def test_status_transient_device_not_added_to_registry(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = (FIXTURES / "steamos-get-status.json").read_bytes()
    make_paramiko_patch(monkeypatch, commands=[(r"steamos-get-status --json", payload, b"", 0)])
    result = status.get_status(core, "192.0.2.77")  # explicit IP, no registry entry
    assert result.hostname == "steamdeck"
    assert core.state.devices.list() == []


def test_status_invalid_json_maps_remote_command_failed(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(core)
    make_paramiko_patch(monkeypatch, commands=[(r"steamos-get-status", b"<html>", b"", 0)])
    with pytest.raises(DevkitError) as exc_info:
        status.get_status(core, "deck")
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED


def test_status_non_object_payload_rejected(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(core)
    make_paramiko_patch(monkeypatch, commands=[(r"steamos-get-status", b"[1, 2]", b"", 0)])
    with pytest.raises(DevkitError) as exc_info:
        status.get_status(core, "deck")
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    assert "non-object" in exc_info.value.message


def test_status_missing_required_keys_rejected(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(core)
    make_paramiko_patch(
        monkeypatch, commands=[(r"steamos-get-status", json.dumps({"hostname": "x"}).encode(), b"", 0)]
    )
    with pytest.raises(DevkitError) as exc_info:
        status.get_status(core, "deck")
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    assert "validation error" in exc_info.value.message


def test_status_propagates_utils_upload_failure(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(core)
    make_paramiko_patch(monkeypatch)

    def fail(self: DeviceHandle) -> None:
        raise DevkitError(ErrorCode.OPENRSYNC_DETECTED, "openrsync found")

    monkeypatch.setattr(DeviceHandle, "ensure_utils", fail)
    with pytest.raises(DevkitError) as exc_info:
        status.get_status(core, "deck")
    assert exc_info.value.code == ErrorCode.OPENRSYNC_DETECTED


def test_parse_status_direct_roundtrip() -> None:
    payload = json.loads((FIXTURES / "steamos-get-status.json").read_text())
    parsed = status.parse_status(payload)
    assert DeviceStatus.model_validate_json(parsed.model_dump_json()) == parsed
