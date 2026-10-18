"""Unit tests for the Windows host adapter (spec section 12, best-effort port).

Runs on macOS: platform-guarded paths raise UNSUPPORTED, everything else is
exercised through fakes.
"""

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from steamos_devkit import constants
from steamos_devkit.adapters import windows
from steamos_devkit.errors import DevkitError, ErrorCode

RSYNC_GOOD = "rsync  version 3.4.1  protocol version 32\n"


def _fake_binary(directory: Path, name: str, output: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_icacls_commands_match_upstream_sequence() -> None:
    cmds = windows.icacls_commands("DESKTOP\\user", Path("C:\\keys\\devkit_rsa"))
    assert cmds == [
        ["icacls.exe", "C:\\keys\\devkit_rsa", "/Reset"],
        ["icacls.exe", "C:\\keys\\devkit_rsa", "/Inheritance:r"],
        ["icacls.exe", "C:\\keys\\devkit_rsa", "/Grant:r", "DESKTOP\\user:(R)"],
        ["icacls.exe", "C:\\keys\\devkit_rsa"],
    ]


def test_platform_guards_raise_unsupported_off_windows() -> None:
    adapter = windows.WindowsAdapter()
    for call in (
        lambda: adapter.open_folder(Path("C:\\x")),
        lambda: adapter.open_url("https://example.test"),
        lambda: adapter.terminal_launch(["ssh"]),
        lambda: adapter._cygwin_root(),
    ):
        with pytest.raises(DevkitError) as exc_info:
            call()
        assert exc_info.value.code == ErrorCode.UNSUPPORTED


def test_rsync_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = _fake_binary(tmp_path / "bin", "rsync", RSYNC_GOOD)
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(good))
    info = windows.WindowsAdapter().rsync()
    assert info.ok is True
    assert info.source == "env"


def test_rsync_falls_back_to_path_off_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # _cygwin_tool raises UNSUPPORTED off win32; the PATH fallback still works.
    bindir = tmp_path / "bin"
    _fake_binary(bindir, "rsync", RSYNC_GOOD)
    monkeypatch.delenv(constants.RSYNC_ENV_VAR, raising=False)
    monkeypatch.setenv("PATH", str(bindir))
    info = windows.WindowsAdapter().rsync()
    assert info.ok is True
    assert info.source == "path"


def test_ssh_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(constants.SSH_ENV_VAR, "C:\\cygwin\\bin\\ssh.exe")
    assert windows.WindowsAdapter().ssh() == Path("C:\\cygwin\\bin\\ssh.exe")


def test_ssh_falls_back_off_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ssh = _fake_binary(tmp_path / "bin", "ssh", "OpenSSH_for_Windows_9.5")
    monkeypatch.delenv(constants.SSH_ENV_VAR, raising=False)
    monkeypatch.setenv("PATH", str(fake_ssh.parent))
    assert windows.WindowsAdapter().ssh() == fake_ssh


def test_fix_key_permissions_icacls_dance(mocker: MockerFixture) -> None:
    mocker.patch.object(windows, "_domain_and_name", return_value="DESKTOP\\user")
    run = mocker.patch("subprocess.run")
    windows.WindowsAdapter().fix_key_permissions(Path("C:\\keys\\devkit_rsa"))
    assert [c.args[0] for c in run.call_args_list] == windows.icacls_commands(
        "DESKTOP\\user", Path("C:\\keys\\devkit_rsa")
    )


def test_discover_tools_off_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bindir = tmp_path / "bin"
    _fake_binary(bindir, "rsync", RSYNC_GOOD)
    _fake_binary(bindir, "ssh", "OpenSSH_for_Windows_9.5")
    monkeypatch.delenv(constants.RSYNC_ENV_VAR, raising=False)
    monkeypatch.delenv(constants.SSH_ENV_VAR, raising=False)
    monkeypatch.setenv("PATH", str(bindir))
    tools = windows.WindowsAdapter().discover_tools()
    assert tools.rsync.ok is True
    assert tools.ssh_version == "OpenSSH_for_Windows_9.5"
    assert tools.terminal is None  # no powershell.exe on the fake PATH


def test_state_dir_and_context() -> None:
    adapter = windows.WindowsAdapter()
    assert adapter.state_dir().name == constants.APP_NAME
    assert adapter.context() == "cli"
