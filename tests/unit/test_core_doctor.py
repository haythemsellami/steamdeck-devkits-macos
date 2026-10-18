"""Unit tests for DevkitCore.doctor() (spec section 15, M0 exit check)."""

from pathlib import Path

import pytest

from steamos_devkit import constants
from steamos_devkit.adapters.macos import MacOSAdapter
from steamos_devkit.core import DevkitCore
from steamos_devkit.models import DoctorReport

RSYNC_GOOD = "rsync  version 3.5.1  protocol version 35\\n"
OPENRSYNC = "openrsync: protocol version 29\\n"


def _fake_rsync(directory: Path, output: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "rsync"
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _core(tmp_path: Path) -> DevkitCore:
    return DevkitCore(state_dir=tmp_path / "state", adapter=MacOSAdapter())


def test_doctor_green_with_good_rsync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = _fake_rsync(tmp_path / "bin", RSYNC_GOOD)
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(good))
    report = _core(tmp_path).doctor()
    assert isinstance(report, DoctorReport)
    assert report.ok is True
    assert report.state_dir == tmp_path / "state"
    assert report.platform == "darwin"
    assert report.context == "cli"
    assert report.tools.rsync.ok is True
    by_name = {c.name: c for c in report.checks}
    assert by_name["rsync"].status == "ok"
    assert by_name["ssh"].status == "ok"
    assert by_name["python"].status == "ok"
    assert by_name["state-dir"].status == "ok"
    # Local Network is honestly unknown with guidance (spec section 9)
    assert by_name["local-network"].status == "unknown"
    assert "Local Network" in by_name["local-network"].summary
    # the state layout was created as a side effect
    assert (tmp_path / "state" / "keys").is_dir()


def test_doctor_reports_openrsync_as_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = _fake_rsync(tmp_path / "bin", OPENRSYNC)
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(bad))
    report = _core(tmp_path).doctor()
    assert report.ok is False
    rsync_check = next(c for c in report.checks if c.name == "rsync")
    assert rsync_check.status == "error"
    assert "brew install rsync" in rsync_check.summary
    assert report.tools.rsync.flavor == "openrsync"


def test_doctor_reports_missing_rsync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(tmp_path / "no-such-rsync"))
    report = _core(tmp_path).doctor()
    assert report.ok is False
    rsync_check = next(c for c in report.checks if c.name == "rsync")
    assert rsync_check.status == "error"


def test_doctor_serializes_to_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = _fake_rsync(tmp_path / "bin", RSYNC_GOOD)
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(good))
    report = _core(tmp_path).doctor()
    parsed = DoctorReport.model_validate_json(report.model_dump_json())
    assert parsed == report


def test_core_exposes_adapter_and_state(tmp_path: Path) -> None:
    adapter = MacOSAdapter()
    core = DevkitCore(state_dir=tmp_path / "state", adapter=adapter)
    assert core.adapter is adapter
    assert core.state.root == tmp_path / "state"


def test_core_env_state_dir_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(constants.STATE_DIR_ENV_VAR, str(tmp_path / "env-state"))
    core = DevkitCore(adapter=MacOSAdapter())
    assert core.state.root == tmp_path / "env-state"


def test_doctor_state_dir_not_writable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = _fake_rsync(tmp_path / "bin", RSYNC_GOOD)
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(good))
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o555)
    try:
        report = DevkitCore(state_dir=readonly, adapter=MacOSAdapter()).doctor()
    finally:
        readonly.chmod(0o755)
    assert report.ok is False
    state_check = next(c for c in report.checks if c.name == "state-dir")
    assert state_check.status == "error"


class _NoTerminalAdapter(MacOSAdapter):
    def discover_tools(self):  # type: ignore[override]
        tools = super().discover_tools()
        tools.terminal = None
        return tools


def test_doctor_terminal_missing_is_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _fake_rsync(tmp_path / "bin", RSYNC_GOOD)
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(good))
    report = DevkitCore(state_dir=tmp_path / "state", adapter=_NoTerminalAdapter()).doctor()
    terminal_check = next(c for c in report.checks if c.name == "terminal")
    assert terminal_check.status == "warning"
    # a warning does not fail the report
    assert report.ok is True
