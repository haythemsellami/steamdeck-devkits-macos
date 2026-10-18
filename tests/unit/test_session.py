"""Unit tests for session/Steam control (spec section 8; audit section 4)."""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import paramiko
import pytest
from fake_ssh import make_paramiko_patch
from typer.testing import CliRunner

from steamos_devkit import cli, constants, session
from steamos_devkit.core import DevkitCore
from steamos_devkit.device import DeviceHandle
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Device, Result

runner = CliRunner()

SELECT_GAMESCOPE = (
    "if command -v holo-session-select >/dev/null 2>&1; "
    "then holo-session-select gamescope; else steamos-session-select gamescope; fi"
)
SELECT_PLASMA = (
    "if command -v holo-session-select >/dev/null 2>&1; "
    "then holo-session-select plasma; else steamos-session-select plasma; fi"
)
MARKER = '${XDG_CONF_DIR:-"$HOME/.config"}/steamos-session-type'
SET_CLIENT = "python3 ~/devkit-utils/steamos-set-steam-client"
GET_STATUS = "python3 ~/devkit-utils/steamos-get-status --json"
FRAME_RESTART = "systemctl --user unmask steam ; systemctl --user restart steam"


@pytest.fixture
def core(state_dir: Path) -> DevkitCore:
    return DevkitCore(state_dir=state_dir)


@pytest.fixture
def paired_key(core: DevkitCore) -> Path:
    core.state.ensure_layout()
    key_path = core.state.key_path
    paramiko.RSAKey.generate(2048).write_private_key_file(str(key_path))
    return key_path


@pytest.fixture
def utils_uploads(core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Make ensure_utils() a recorded local no-op (rsync upload of devkit-utils)."""
    bin_dir = core.state.root / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake_rsync = bin_dir / "rsync"
    fake_rsync.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'rsync  version 3.5.1  protocol version 33'\n",
        encoding="utf-8",
    )
    fake_rsync.chmod(0o755)
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(fake_rsync))
    calls: list[list[str]] = []

    def fake_run(self: DeviceHandle, argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(DeviceHandle, "_run_local", fake_run)
    return calls


def _registry_device(core: DevkitCore, name: str = "deck") -> Device:
    device = Device(name=name, address="192.0.2.10", login="deck", added_via="explicit")
    core.state.devices.upsert(device)
    return device


# --- steam-mode ------------------------------------------------------------------


def test_steam_mode_os_default_gameid(
    core: DevkitCore,
    paired_key: Path,
    utils_uploads: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry_device(core)
    fake = make_paramiko_patch(monkeypatch, commands=[(r"steamos-set-steam-client", b"", b"", 0)])
    result = session.set_steam_mode(core, "deck", "OS")
    assert result.ok
    # ensure_utils ran (rsync upload), then the helper with the placeholder gameid
    assert len(utils_uploads) == 1
    assert fake.commands_run == [f"{SET_CLIENT} --client SteamStatus.OS --gameid steam"]


def test_steam_mode_os_dev_full_command(
    core: DevkitCore,
    paired_key: Path,
    utils_uploads: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry_device(core)
    fake = make_paramiko_patch(monkeypatch, commands=[(r"steamos-set-steam-client", b"", b"", 0)])
    result = session.set_steam_mode(
        core, "deck", "OS_DEV", gameid="mygame", args="-steamos3 -fullscreen", gdbserver=True
    )
    assert result.ok
    assert fake.commands_run == [
        f"{SET_CLIENT} --client SteamStatus.OS_DEV --gameid mygame "
        "--args='-steamos3 -fullscreen' --gdbserver"
    ]


def test_steam_mode_sideloaded_rejected(
    core: DevkitCore,
    paired_key: Path,
    utils_uploads: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry_device(core)
    fake = make_paramiko_patch(monkeypatch)
    with pytest.raises(DevkitError) as exc_info:
        session.set_steam_mode(core, "deck", "SIDELOADED")  # type: ignore[arg-type]
    assert exc_info.value.code == ErrorCode.UNSUPPORTED
    # rejected before touching the device or uploading helpers
    assert fake.commands_run == []
    assert utils_uploads == []


def test_steam_mode_rejects_bad_mode_and_gameid(
    core: DevkitCore, paired_key: Path, utils_uploads: list[list[str]]
) -> None:
    _registry_device(core)
    with pytest.raises(DevkitError) as exc_info:
        session.set_steam_mode(core, "deck", "DEV")  # type: ignore[arg-type]
    assert exc_info.value.code == ErrorCode.VALIDATION
    with pytest.raises(DevkitError) as exc_info:
        session.set_steam_mode(core, "deck", "OS", gameid="bad;id")
    assert exc_info.value.code == ErrorCode.VALIDATION


def test_steam_mode_helper_failure(
    core: DevkitCore,
    paired_key: Path,
    utils_uploads: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry_device(core)
    make_paramiko_patch(monkeypatch, commands=[(r"steamos-set-steam-client", b"", b"nope\n", 1)])
    with pytest.raises(DevkitError) as exc_info:
        session.set_steam_mode(core, "deck", "OS")
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED


# --- session select ----------------------------------------------------------------


def test_session_gamescope_removes_marker(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    fake = make_paramiko_patch(monkeypatch, commands=[(r".", b"", b"", 0)])
    result = session.set_session(core, "deck", "gamescope")
    assert result.ok
    assert fake.commands_run == [f"rm -f {MARKER}", SELECT_GAMESCOPE]


def test_session_plasma_x11(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    fake = make_paramiko_patch(monkeypatch, commands=[(r".", b"", b"", 0)])
    result = session.set_session(core, "deck", "plasma-x11")
    assert result.ok
    assert fake.commands_run == [f"rm -f {MARKER}", SELECT_PLASMA]


def test_session_plasma_wayland_writes_marker(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    fake = make_paramiko_patch(monkeypatch, commands=[(r".", b"", b"", 0)])
    result = session.set_session(core, "deck", "plasma-wayland")
    assert result.ok
    assert fake.commands_run == [f"echo wayland > {MARKER}", SELECT_PLASMA]


def test_session_rejects_unknown(core: DevkitCore) -> None:
    _registry_device(core)
    with pytest.raises(DevkitError) as exc_info:
        session.set_session(core, "deck", "twm")  # type: ignore[arg-type]
    assert exc_info.value.code == ErrorCode.VALIDATION


# --- reload steam ------------------------------------------------------------------


def test_reload_steam_deck_reselects_gamescope(
    core: DevkitCore,
    paired_key: Path,
    utils_uploads: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    fixture_file: Callable[[str], str],
) -> None:
    _registry_device(core)
    fake = make_paramiko_patch(
        monkeypatch,
        commands=[
            (r"steamos-get-status", fixture_file("steamos-get-status.json").encode(), b"", 0),
            (r"session-select", b"", b"", 0),
        ],
    )
    result = session.reload_steam(core, "deck")
    assert result.ok
    assert fake.commands_run == [GET_STATUS, SELECT_GAMESCOPE]
    assert not any("systemctl" in cmd for cmd in fake.commands_run)


def test_reload_steam_frame_restarts_service(
    core: DevkitCore,
    paired_key: Path,
    utils_uploads: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    fixture_file: Callable[[str], str],
) -> None:
    _registry_device(core)
    deckard = fixture_file("steamos-get-status-deckard.json").encode()
    fake = make_paramiko_patch(
        monkeypatch,
        commands=[
            (r"steamos-get-status", deckard, b"", 0),
            (r"systemctl", b"", b"", 0),
        ],
    )
    result = session.reload_steam(core, "deck")
    assert result.ok
    assert "restarted" in (result.message or "")
    assert fake.commands_run == [GET_STATUS, FRAME_RESTART]


def test_reload_steam_bad_status_payload(
    core: DevkitCore,
    paired_key: Path,
    utils_uploads: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry_device(core)
    make_paramiko_patch(monkeypatch, commands=[(r"steamos-get-status", b"[1, 2]", b"", 0)])
    with pytest.raises(DevkitError) as exc_info:
        session.reload_steam(core, "deck")
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED


# --- CLI plumbing ------------------------------------------------------------------


def test_cli_steam_mode_maps_mode_and_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = []

    def fake_set_steam_mode(core, sel, mode, gameid=None, args=None, gdbserver=False):
        calls.append((sel, mode, gameid, args, gdbserver))
        return Result.success(message="ok")

    monkeypatch.setattr(session, "set_steam_mode", fake_set_steam_mode)
    result = runner.invoke(
        cli.app,
        [
            "--state-dir",
            str(tmp_path),
            "steam-mode",
            "deck",
            "os-dev",
            "--gameid",
            "mygame",
            "--args",
            "-steamos3",
            "--gdbserver",
        ],
    )
    assert result.exit_code == 0
    assert calls == [("deck", "OS_DEV", "mygame", "-steamos3", True)]
    assert json.loads(result.stdout)["ok"] is True


def test_cli_steam_mode_rejects_bad_mode(tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app, ["--state-dir", str(tmp_path), "steam-mode", "deck", "sideload"]
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "VALIDATION"


def test_cli_session_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(
        session,
        "set_session",
        lambda core, sel, s: calls.append(s) or Result.success(message="switched"),
    )
    result = runner.invoke(
        cli.app, ["--state-dir", str(tmp_path), "session", "deck", "plasma-wayland"]
    )
    assert result.exit_code == 0
    assert calls == ["plasma-wayland"]

    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path), "session", "deck", "twm"])
    assert result.exit_code == 2


def test_cli_reload_steam(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        session,
        "reload_steam",
        lambda core, sel: Result.success(message="steam service restarted"),
    )
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path), "reload-steam", "deck"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["ok"] is True


def test_cli_reload_steam_failure_maps_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(core, sel):
        raise DevkitError(ErrorCode.DEVICE_UNREACHABLE, "cannot reach")

    monkeypatch.setattr(session, "reload_steam", boom)
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path), "reload-steam", "deck"])
    assert result.exit_code == 3
    assert json.loads(result.stdout)["code"] == "DEVICE_UNREACHABLE"


def test_cli_steam_mode_error_maps_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(core, sel, mode, gameid=None, args=None, gdbserver=False):
        raise DevkitError(ErrorCode.DEVICE_UNREACHABLE, "cannot reach")

    monkeypatch.setattr(session, "set_steam_mode", boom)
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path), "steam-mode", "deck", "os"])
    assert result.exit_code == 3
    assert json.loads(result.stdout)["code"] == "DEVICE_UNREACHABLE"


def test_cli_session_error_maps_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(core, sel, s):
        raise DevkitError(ErrorCode.DEVICE_UNREACHABLE, "cannot reach")

    monkeypatch.setattr(session, "set_session", boom)
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path), "session", "deck", "gamescope"])
    assert result.exit_code == 3
    assert json.loads(result.stdout)["code"] == "DEVICE_UNREACHABLE"
