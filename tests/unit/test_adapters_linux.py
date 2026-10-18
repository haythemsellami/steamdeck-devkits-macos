"""Unit tests for the Linux host adapter (spec section 12, best-effort port)."""

import stat
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from steamos_devkit import constants
from steamos_devkit.adapters import linux
from steamos_devkit.errors import DevkitError, ErrorCode

RSYNC_GOOD = "rsync  version 3.2.7  protocol version 31\n"
OPENRSYNC = "openrsync: protocol version 29\n"


def _fake_binary(directory: Path, name: str, output: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_terminal_search_list_matches_upstream_order() -> None:
    assert [name for name, _ in linux.TERMINAL_SEARCH_LIST] == [
        "konsole",
        "gnome-terminal",
        "xterm",
    ]


def test_rsync_env_override_is_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _fake_binary(tmp_path / "bin", "rsync", RSYNC_GOOD)
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(good))
    info = linux.LinuxAdapter().rsync()
    assert info.ok is True
    assert info.source == "env"
    assert info.version == "3.2.7"


def test_rsync_env_override_openrsync_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = _fake_binary(tmp_path / "bin", "rsync", OPENRSYNC)
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(bad))
    info = linux.LinuxAdapter().rsync()
    assert info.ok is False
    assert info.flavor == "openrsync"


def test_rsync_path_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bindir = tmp_path / "bin"
    _fake_binary(bindir, "rsync", RSYNC_GOOD)
    monkeypatch.delenv(constants.RSYNC_ENV_VAR, raising=False)
    monkeypatch.setenv("PATH", str(bindir))
    info = linux.LinuxAdapter().rsync()
    assert info.ok is True
    assert info.source == "path"


def test_ssh_default_and_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ssh = _fake_binary(tmp_path / "bin", "ssh", "OpenSSH_9.9p1")
    monkeypatch.delenv(constants.SSH_ENV_VAR, raising=False)
    monkeypatch.setenv("PATH", str(fake_ssh.parent))
    assert linux.LinuxAdapter().ssh() == fake_ssh
    monkeypatch.setenv(constants.SSH_ENV_VAR, "/custom/ssh")
    assert linux.LinuxAdapter().ssh() == Path("/custom/ssh")


def test_ssh_fallback_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(constants.SSH_ENV_VAR, raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")
    assert linux.LinuxAdapter().ssh() == Path("/usr/bin/ssh")


def test_fix_key_permissions(tmp_path: Path) -> None:
    key = tmp_path / "devkit_rsa"
    key.write_bytes(b"key")
    key.chmod(0o644)
    linux.LinuxAdapter().fix_key_permissions(key)
    assert stat.S_IMODE(key.stat().st_mode) == 0o400
    with pytest.raises(DevkitError) as exc_info:
        linux.LinuxAdapter().fix_key_permissions(tmp_path / "absent")
    assert exc_info.value.code == ErrorCode.PERMISSION_KEY_FILE


def test_terminal_launch_uses_first_available(mocker: MockerFixture) -> None:
    which = {name: None for name, _ in linux.TERMINAL_SEARCH_LIST}
    which["xterm"] = Path("/usr/bin/xterm")
    mocker.patch.object(linux, "which_path", side_effect=lambda name: which.get(name))
    popen = mocker.patch("subprocess.Popen")
    linux.LinuxAdapter().terminal_launch(["ssh", "-t", "deck@192.0.2.10"])
    (argv,), _ = popen.call_args
    assert argv == ["/usr/bin/xterm", "-e", "ssh", "-t", "deck@192.0.2.10"]


def test_terminal_launch_none_found(mocker: MockerFixture) -> None:
    mocker.patch.object(linux, "which_path", return_value=None)
    with pytest.raises(DevkitError) as exc_info:
        linux.LinuxAdapter().terminal_launch(["ssh"])
    assert exc_info.value.code == ErrorCode.DEPENDENCY_MISSING
    assert "konsole" in exc_info.value.message


def test_terminal_launch_oserror(mocker: MockerFixture) -> None:
    mocker.patch.object(linux, "which_path", return_value=Path("/usr/bin/konsole"))
    mocker.patch("subprocess.Popen", side_effect=OSError("boom"))
    with pytest.raises(DevkitError) as exc_info:
        linux.LinuxAdapter().terminal_launch(["ssh"])
    assert exc_info.value.code == ErrorCode.DEPENDENCY_MISSING


def test_xdg_open(mocker: MockerFixture) -> None:
    mocker.patch.object(linux, "which_path", return_value=Path("/usr/bin/xdg-open"))
    popen = mocker.patch("subprocess.Popen")
    adapter = linux.LinuxAdapter()
    adapter.open_folder(Path("/tmp/x"))
    adapter.open_url("https://example.test")
    assert popen.call_args_list[0][0][0] == ["/usr/bin/xdg-open", "/tmp/x"]
    assert popen.call_args_list[1][0][0] == ["/usr/bin/xdg-open", "https://example.test"]


def test_xdg_open_missing(mocker: MockerFixture) -> None:
    mocker.patch.object(linux, "which_path", return_value=None)
    with pytest.raises(DevkitError) as exc_info:
        linux.LinuxAdapter().open_url("https://example.test")
    assert exc_info.value.code == ErrorCode.DEPENDENCY_MISSING


def test_discover_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bindir = tmp_path / "bin"
    _fake_binary(bindir, "rsync", RSYNC_GOOD)
    _fake_binary(bindir, "ssh", "OpenSSH_9.9p1, LibreSSL")
    monkeypatch.delenv(constants.RSYNC_ENV_VAR, raising=False)
    monkeypatch.delenv(constants.SSH_ENV_VAR, raising=False)
    monkeypatch.setenv("PATH", str(bindir))
    tools = linux.LinuxAdapter().discover_tools()
    assert tools.rsync.ok is True
    assert tools.ssh_path == bindir / "ssh"
    assert tools.ssh_version == "OpenSSH_9.9p1"
    assert tools.terminal is None  # no konsole/gnome-terminal/xterm on the fake PATH
    assert tools.filezilla is None


def test_state_dir_matches_host_platform_default() -> None:
    # appdirs follows the host platform: on Linux this is ~/.config/steamos-devkit
    # (upstream-compatible); here on macOS it is the shared Application Support dir.
    assert linux.LinuxAdapter().state_dir() == linux.platform_default_state_dir()


def test_context() -> None:
    assert linux.LinuxAdapter().context() == "cli"
