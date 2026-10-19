"""Branch-coverage push for the Windows adapter (spec section 12).

Runs on macOS with faked platform seams: ``sys.platform=win32``, an injected
``winreg`` module, scripted subprocess calls, and a stubbed ``os.startfile``.
No Windows host, registry, or network is touched.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pytest_mock import MockerFixture

from steamos_devkit import constants
from steamos_devkit.adapters import windows
from steamos_devkit.errors import DevkitError, ErrorCode

RSYNC_GOOD = "rsync  version 3.4.1  protocol version 32\n"


class FakeWinreg:
    """Minimal stand-in for the winreg module: two hives, scripted answers."""

    HKEY_LOCAL_MACHINE = "HKLM"
    HKEY_CURRENT_USER = "HKCU"

    def __init__(
        self, openable: tuple[str, ...] = (), values: dict[str, str] | None = None
    ) -> None:
        self._openable = set(openable)
        self._values = values or {}

    def OpenKey(self, hive: str, subkey: str) -> str:
        assert subkey == windows._CYGWIN_REGISTRY_KEY
        if hive not in self._openable:
            raise OSError("key not found")
        return hive  # the hive name doubles as the key handle

    def QueryValueEx(self, key: str, name: str) -> tuple[str, int]:
        assert name == "rootdir"
        if key not in self._values:
            raise OSError("value not found")
        return (self._values[key], 1)


@pytest.fixture
def fake_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")


def _fake_rsync(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "rsync.exe"
    path.write_text("#!/bin/sh\nprintf '%s\\n' '" + RSYNC_GOOD + "'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_winreg_import_uses_sys_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    assert windows._winreg() is fake


def test_require_windows_passes_on_win32(fake_win32: None) -> None:
    windows._require_windows()  # must not raise


def test_cygwin_root_reads_hklm(fake_win32: None, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeWinreg(openable=("HKLM",), values={"HKLM": "C:\\cygwin64"})
    monkeypatch.setattr(windows, "_winreg", lambda: fake)
    assert windows.WindowsAdapter()._cygwin_root() == Path("C:\\cygwin64")


def test_cygwin_root_falls_back_to_hkcu(fake_win32: None, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeWinreg(openable=("HKCU",), values={"HKCU": "D:\\cygwin"})
    monkeypatch.setattr(windows, "_winreg", lambda: fake)
    assert windows.WindowsAdapter()._cygwin_root() == Path("D:\\cygwin")


def test_cygwin_root_skips_hive_with_missing_value(
    fake_win32: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # HKLM opens but has no rootdir value; HKCU has one
    fake = FakeWinreg(openable=("HKLM", "HKCU"), values={"HKCU": "E:\\cw"})
    monkeypatch.setattr(windows, "_winreg", lambda: fake)
    assert windows.WindowsAdapter()._cygwin_root() == Path("E:\\cw")


def test_cygwin_root_absent(fake_win32: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows, "_winreg", lambda: FakeWinreg())
    assert windows.WindowsAdapter()._cygwin_root() is None


def test_cygwin_tool_prefers_frozen_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundled = _fake_rsync(tmp_path / "app" / "cygroot" / "bin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app" / "devkit.exe"))
    assert windows.WindowsAdapter()._cygwin_tool("rsync.exe") == bundled


def test_cygwin_tool_from_registry_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> None:
    tool = _fake_rsync(tmp_path / "cygwin" / "bin")
    mocker.patch.object(windows.WindowsAdapter, "_cygwin_root", lambda self: tmp_path / "cygwin")
    assert windows.WindowsAdapter()._cygwin_tool("rsync.exe") == tool


def test_cygwin_tool_missing_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    mocker.patch.object(windows.WindowsAdapter, "_cygwin_root", lambda self: tmp_path / "empty")
    assert windows.WindowsAdapter()._cygwin_tool("rsync.exe") is None


def test_rsync_uses_cygwin_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> None:
    monkeypatch.delenv(constants.RSYNC_ENV_VAR, raising=False)
    tool = _fake_rsync(tmp_path / "cygwin" / "bin")
    mocker.patch.object(windows.WindowsAdapter, "_cygwin_tool", lambda self, name: tool)
    info = windows.WindowsAdapter().rsync()
    assert info.ok is True
    assert info.source == "cygwin"


def test_ssh_uses_cygwin_candidate(mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(constants.SSH_ENV_VAR, raising=False)
    cygwin_ssh = Path("C:\\cygwin\\bin\\ssh.exe")
    mocker.patch.object(windows.WindowsAdapter, "_cygwin_tool", lambda self, name: cygwin_ssh)
    assert windows.WindowsAdapter().ssh() == cygwin_ssh


def test_domain_and_name(mocker: MockerFixture) -> None:
    mocker.patch.object(subprocess, "check_output", return_value="DESKTOP-1\\deck\n")
    assert windows._domain_and_name() == "DESKTOP-1\\deck"


def test_open_folder_and_url_use_startfile(
    fake_win32: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)
    adapter = windows.WindowsAdapter()
    adapter.open_folder(Path("C:\\artifacts"))
    adapter.open_url("https://example.test")
    assert opened == ["C:\\artifacts", "https://example.test"]


def test_terminal_launch_writes_powershell_script(
    fake_win32: None, mocker: MockerFixture, tmp_path: Path
) -> None:
    popen_calls: list[list[str]] = []

    class _Proc:
        args: Any = None

    def _popen(argv: list[str], **kwargs: Any) -> _Proc:
        popen_calls.append(argv)
        return _Proc()

    mocker.patch.object(windows, "which_path", lambda name: Path("C:\\Windows\\powershell.exe"))
    mocker.patch.object(subprocess, "Popen", _popen)
    windows.WindowsAdapter().terminal_launch(["ssh.exe", "-t", "deck@192.0.2.10"])
    assert len(popen_calls) == 1
    argv = popen_calls[0]
    assert argv[0] == "C:\\Windows\\powershell.exe"
    assert argv[1:3] == ["-ExecutionPolicy", "Bypass"]
    script_path = Path(argv[3])
    script = script_path.read_text(encoding="utf-8")
    try:
        assert 'Start-Process -NoNewWindow -Wait -FilePath "ssh.exe"' in script
        assert '"-t","deck@192.0.2.10"' in script
    finally:
        script_path.unlink()


def test_terminal_launch_without_powershell(
    fake_win32: None, mocker: MockerFixture, tmp_path: Path
) -> None:
    mocker.patch.object(windows, "which_path", lambda name: None)
    with pytest.raises(DevkitError) as exc_info:
        windows.WindowsAdapter().terminal_launch(["ssh.exe"])
    assert exc_info.value.code == ErrorCode.DEPENDENCY_MISSING
