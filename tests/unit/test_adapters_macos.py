"""Unit tests for the macOS host adapter (spec section 12)."""

import stat
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from steamos_devkit import constants
from steamos_devkit.adapters import macos
from steamos_devkit.adapters.base import (
    MIN_RSYNC_PROTOCOL,
    detect_context,
    parse_rsync_version_output,
    probe_rsync,
)
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import RsyncInfo

RSYNC_351 = (
    "rsync  version 3.5.1  protocol version 35  Copyright (C) 1996-2025 ...\n"
    "Capabilities: 64-bit files, ...\n"
)
RSYNC_327 = "rsync  version 3.2.7  protocol version 31  Copyright ...\n"
RSYNC_2610 = "rsync  version 2.6.9  protocol version 29  Copyright ...\n"
OPENRSYNC = "openrsync: protocol version 29\n"
OPENRSYNC_DETAIL = "openrsync 20220513\nopenrsync: protocol version 29\n"


@pytest.mark.parametrize(
    ("text", "flavor", "version", "protocol"),
    [
        (RSYNC_351, "rsync", "3.5.1", 35),
        (RSYNC_327, "rsync", "3.2.7", 31),
        (RSYNC_2610, "rsync", "2.6.9", 29),
        (OPENRSYNC, "openrsync", None, 29),
        (OPENRSYNC_DETAIL, "openrsync", None, 29),
        ("garbage output\n", "unknown", None, None),
        ("", "unknown", None, None),
    ],
)
def test_parse_rsync_version_output(
    text: str, flavor: str, version: str | None, protocol: int | None
) -> None:
    assert parse_rsync_version_output(text) == (flavor, version, protocol)


def _make_fake_rsync(directory: Path, output: str, name: str = "rsync") -> Path:
    path = directory / name
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_probe_rsync_good(tmp_path: Path) -> None:
    good = _make_fake_rsync(tmp_path, RSYNC_351)
    info = probe_rsync(good, "env")
    assert info.ok is True
    assert info.flavor == "rsync"
    assert info.version == "3.5.1"
    assert info.protocol_version == 35


def test_probe_rsync_rejects_openrsync(tmp_path: Path) -> None:
    bad = _make_fake_rsync(tmp_path, OPENRSYNC)
    info = probe_rsync(bad, "path")
    assert info.ok is False
    assert info.flavor == "openrsync"
    assert info.message is not None and "brew install rsync" in info.message


def test_probe_rsync_rejects_old_protocol(tmp_path: Path) -> None:
    old = _make_fake_rsync(tmp_path, RSYNC_2610)
    info = probe_rsync(old, "path")
    assert info.ok is False
    assert info.protocol_version == 29
    assert info.message is not None and str(MIN_RSYNC_PROTOCOL) in info.message


def test_probe_rsync_missing_binary(tmp_path: Path) -> None:
    info = probe_rsync(tmp_path / "does-not-exist", "path")
    assert info.ok is False
    assert info.message is not None and "failed to run" in info.message


def test_adapter_rsync_env_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = _make_fake_rsync(tmp_path, RSYNC_351)
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(good))
    info = macos.MacOSAdapter().rsync()
    assert info.ok is True
    assert info.source == "env"
    assert info.path == good


def test_adapter_rsync_path_scan_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _make_fake_rsync(bindir, RSYNC_327)
    monkeypatch.delenv(constants.RSYNC_ENV_VAR, raising=False)
    monkeypatch.setattr(macos, "_HOMEBREW_RSYNC", tmp_path / "no-homebrew-rsync")
    monkeypatch.setenv("PATH", str(bindir))
    info = macos.MacOSAdapter().rsync()
    assert info.ok is True
    assert info.source == "path"
    assert info.version == "3.2.7"


def test_adapter_rsync_reports_openrsync_from_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _make_fake_rsync(bindir, OPENRSYNC)
    monkeypatch.delenv(constants.RSYNC_ENV_VAR, raising=False)
    monkeypatch.setattr(macos, "_HOMEBREW_RSYNC", tmp_path / "no-homebrew-rsync")
    monkeypatch.setenv("PATH", str(bindir))
    info = macos.MacOSAdapter().rsync()
    assert info.ok is False
    assert info.flavor == "openrsync"


def test_adapter_rsync_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(constants.RSYNC_ENV_VAR, raising=False)
    monkeypatch.setattr(macos, "_HOMEBREW_RSYNC", tmp_path / "no-homebrew-rsync")
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing on PATH
    info = macos.MacOSAdapter().rsync()
    assert info.ok is False
    assert info.path is None
    assert info.message is not None and "rsync not found" in info.message


def test_ssh_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(constants.SSH_ENV_VAR, raising=False)
    assert macos.MacOSAdapter().ssh() == Path("/usr/bin/ssh")
    monkeypatch.setenv(constants.SSH_ENV_VAR, "/custom/ssh")
    assert macos.MacOSAdapter().ssh() == Path("/custom/ssh")


def test_fix_key_permissions_chmod_0400(tmp_path: Path) -> None:
    key = tmp_path / "devkit_rsa"
    key.write_bytes(b"key")
    key.chmod(0o644)
    macos.MacOSAdapter().fix_key_permissions(key)
    assert stat.S_IMODE(key.stat().st_mode) == 0o400


def test_fix_key_permissions_missing_file(tmp_path: Path) -> None:
    with pytest.raises(DevkitError) as exc_info:
        macos.MacOSAdapter().fix_key_permissions(tmp_path / "absent")
    assert exc_info.value.code == ErrorCode.PERMISSION_KEY_FILE


def test_context_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(constants.CONTEXT_ENV_VAR, raising=False)
    assert detect_context() == "cli"  # not frozen in tests
    monkeypatch.setenv(constants.CONTEXT_ENV_VAR, "mcp_stdio")
    assert detect_context() == "mcp_stdio"
    monkeypatch.setenv(constants.CONTEXT_ENV_VAR, "bogus")
    assert detect_context() == "cli"


def test_terminal_launch_terminal_app(mocker: MockerFixture) -> None:
    mocker.patch.object(macos, "_iterm_installed", return_value=False)
    popen = mocker.patch("subprocess.Popen")
    macos.MacOSAdapter().terminal_launch(["/usr/bin/ssh", "-t", "deck@192.0.2.10"])
    (argv,), _ = popen.call_args
    assert argv[0] == "/usr/bin/osascript"
    assert "Terminal" in argv[2]
    assert "iTerm" not in argv[2]
    assert "ssh" in argv[2] and "deck@192.0.2.10" in argv[2]


def test_terminal_launch_iterm_when_installed(mocker: MockerFixture) -> None:
    mocker.patch.object(macos, "_iterm_installed", return_value=True)
    popen = mocker.patch("subprocess.Popen")
    macos.MacOSAdapter().terminal_launch(["/usr/bin/ssh", 'weird"name'])
    (argv,), _ = popen.call_args
    assert "iTerm" in argv[2]
    # double quotes are escaped for AppleScript
    assert 'weird\\"name' in argv[2]


def test_terminal_launch_osascript_missing(mocker: MockerFixture) -> None:
    mocker.patch.object(macos, "_iterm_installed", return_value=False)
    mocker.patch("subprocess.Popen", side_effect=FileNotFoundError("no osascript"))
    with pytest.raises(DevkitError) as exc_info:
        macos.MacOSAdapter().terminal_launch(["ssh"])
    assert exc_info.value.code == ErrorCode.DEPENDENCY_MISSING


def test_discover_tools_shape(mocker: MockerFixture, tmp_path: Path) -> None:
    mocker.patch.object(
        macos.MacOSAdapter,
        "rsync",
        return_value=RsyncInfo(flavor="rsync", version="3.5.1", protocol_version=35, ok=True),
    )
    mocker.patch.object(macos, "_filezilla_installed", return_value=True)
    tools = macos.MacOSAdapter().discover_tools()
    assert tools.rsync.ok is True
    assert tools.terminal in ("Terminal.app", "iTerm2")
    assert tools.filezilla == macos._FILEZILLA_APP


def test_open_folder_and_url(mocker: MockerFixture) -> None:
    popen = mocker.patch("subprocess.Popen")
    adapter = macos.MacOSAdapter()
    adapter.open_folder(Path("/tmp/x"))
    adapter.open_url("https://example.test")
    assert popen.call_args_list[0][0][0] == ["/usr/bin/open", "/tmp/x"]
    assert popen.call_args_list[1][0][0] == ["/usr/bin/open", "https://example.test"]


def test_state_dir_is_appdirs_location() -> None:
    expected = Path.home() / "Library" / "Application Support" / constants.APP_NAME
    assert macos.MacOSAdapter().state_dir() == expected


def test_bundled_rsync_only_when_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    assert macos._bundled_rsync() is None
    monkeypatch.setattr(macos.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        macos.sys, "executable", str(Path("/nonexistent/App.app/Contents/MacOS/app"))
    )
    assert macos._bundled_rsync() is None  # not present on disk
