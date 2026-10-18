"""Unit tests for shared adapter helpers in adapters/base.py and selection."""

import subprocess
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from steamos_devkit.adapters import base, get_adapter
from steamos_devkit.adapters.linux import LinuxAdapter
from steamos_devkit.adapters.macos import MacOSAdapter
from steamos_devkit.adapters.windows import WindowsAdapter
from steamos_devkit.models import RsyncInfo


def test_get_adapter_per_platform() -> None:
    assert isinstance(get_adapter("darwin"), MacOSAdapter)
    assert isinstance(get_adapter("win32"), WindowsAdapter)
    assert isinstance(get_adapter("linux"), LinuxAdapter)
    assert isinstance(get_adapter("freebsd"), LinuxAdapter)  # posix fallback


def test_probe_rsync_timeout(mocker: MockerFixture, tmp_path: Path) -> None:
    mocker.patch.object(
        base.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(cmd="rsync", timeout=10),
    )
    info = base.probe_rsync(tmp_path / "rsync", "path")
    assert info.ok is False
    assert info.message is not None and "failed to run" in info.message


def test_probe_rsync_unrecognized_output(mocker: MockerFixture, tmp_path: Path) -> None:
    mocker.patch.object(
        base.subprocess,
        "run",
        return_value=subprocess.CompletedProcess([], 0, stdout="something else\n"),
    )
    info = base.probe_rsync(tmp_path / "rsync", "path")
    assert info.ok is False
    assert info.flavor == "unknown"
    assert "unrecognized" in (info.message or "")


def test_probe_rsync_candidates_prefers_first_ok(mocker: MockerFixture) -> None:
    good = RsyncInfo(
        path=Path("/good"), flavor="rsync", version="3.5.1", protocol_version=35, ok=True
    )
    bad = RsyncInfo(path=Path("/bad"), flavor="openrsync", message="no")
    calls = iter([bad, good])
    mocker.patch.object(base, "probe_rsync", side_effect=lambda *a: next(calls))
    info = base.probe_rsync_candidates([(Path("/bad"), "path"), (Path("/good"), "path")])
    assert info is good


def test_probe_rsync_candidates_returns_first_failure(mocker: MockerFixture) -> None:
    bad1 = RsyncInfo(path=Path("/bad1"), flavor="openrsync", message="first")
    bad2 = RsyncInfo(path=Path("/bad2"), flavor="unknown", message="second")
    mocker.patch.object(base, "probe_rsync", side_effect=[bad1, bad2])
    info = base.probe_rsync_candidates([(Path("/bad1"), "path"), (Path("/bad2"), "path")])
    assert info is bad1


def test_probe_rsync_candidates_empty() -> None:
    info = base.probe_rsync_candidates([])
    assert info.ok is False
    assert info.source == "none"
    assert "rsync not found" in (info.message or "")


def test_path_scan_skips_empty_entries_and_nonexec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    nonexec = bindir / "rsync"
    nonexec.write_text("#!/bin/sh\n")
    nonexec.chmod(0o644)  # not executable
    monkeypatch.setenv("PATH", f"::{bindir}:")
    assert base.path_scan("rsync") == []
    nonexec.chmod(0o755)
    assert base.path_scan("rsync") == [nonexec]


def test_ssh_version(tmp_path: Path, mocker: MockerFixture) -> None:
    fake = tmp_path / "ssh"
    fake.write_text("#!/bin/sh\necho 'OpenSSH_10.3p1, LibreSSL 4.1' 1>&2\n")
    fake.chmod(0o755)
    assert base.ssh_version(fake) == "OpenSSH_10.3p1"
    mocker.patch.object(
        base.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=10),
    )
    assert base.ssh_version(fake) is None


def test_detect_context_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEVKIT_CONTEXT", raising=False)
    monkeypatch.setattr(base.sys, "frozen", True, raising=False)
    assert base.detect_context() == "bundled_app"


def test_host_adapter_protocol_runtime_checkable() -> None:
    assert isinstance(MacOSAdapter(), base.HostAdapter)
    assert isinstance(LinuxAdapter(), base.HostAdapter)
    assert isinstance(WindowsAdapter(), base.HostAdapter)
