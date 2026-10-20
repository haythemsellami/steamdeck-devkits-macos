"""Unit tests for `devkit setup` onboarding (spec section 10)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from steamos_devkit import cli
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.setup import run_setup

runner = CliRunner()


@pytest.fixture
def core(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> DevkitCore:
    # isolate the host rsync probe: always a good rsync unless a test says otherwise
    monkeypatch.setenv("DEVKIT_RSYNC", str(_good_rsync(state_dir / "bin")))
    return DevkitCore(state_dir=state_dir)


def _good_rsync(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "rsync"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf 'rsync  version 3.5.1  protocol version 33\\n'\n"
        "fi\nexit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@pytest.fixture(autouse=True)
def no_real_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("steamos_devkit.discovery.discover", lambda core, timeout_s=5.0: [])


def test_setup_happy_path(core: DevkitCore) -> None:
    report = run_setup(core)
    assert report.ok
    by_name = {step.name: step for step in report.steps}
    assert by_name["state-dir"].status == "ok"
    assert by_name["keypair"].status == "ok"
    assert core.state.key_path.exists()
    assert by_name["rsync"].status == "ok"
    assert by_name["ssh"].status == "ok"
    if sys.platform == "darwin":
        assert by_name["local-network"].status == "warning"  # no devices, no error


def test_setup_keypair_skipped_when_present(core: DevkitCore) -> None:
    first = run_setup(core)
    assert first.ok
    second = run_setup(core)
    by_name = {step.name: step for step in second.steps}
    assert by_name["keypair"].status == "skipped"


def test_setup_rsync_missing_no_install(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVKIT_RSYNC", str(state_dir / "bin" / "absent-rsync"))
    core = DevkitCore(state_dir=state_dir)
    report = run_setup(core, install_deps=False)
    assert not report.ok
    rsync_step = next(step for step in report.steps if step.name == "rsync")
    assert rsync_step.status == "error"
    assert "brew install rsync" in rsync_step.summary


def test_setup_rsync_missing_no_brew(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVKIT_RSYNC", str(state_dir / "bin" / "absent-rsync"))
    monkeypatch.setattr("steamos_devkit.setup._find_brew", lambda: None)
    core = DevkitCore(state_dir=state_dir)
    report = run_setup(core, install_deps=True)
    assert not report.ok
    rsync_step = next(step for step in report.steps if step.name == "rsync")
    assert "Homebrew" in rsync_step.summary


def test_setup_rsync_install_success(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = state_dir / "bin"
    rsync_path = bin_dir / "rsync"
    monkeypatch.setenv("DEVKIT_RSYNC", str(rsync_path))  # absent until "installed"
    monkeypatch.setattr("steamos_devkit.setup._find_brew", lambda: Path("/fake/brew"))
    calls: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if str(argv[0]).endswith("brew"):
            calls.append(list(argv))
            _good_rsync(bin_dir)  # the fake install lands a real fake rsync
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real_run(argv, **kwargs)  # adapter probes (rsync --version, ssh -V) stay real

    monkeypatch.setattr(subprocess, "run", fake_run)
    core = DevkitCore(state_dir=state_dir)
    report = run_setup(core)
    assert calls == [["/fake/brew", "install", "rsync"]]
    rsync_step = next(step for step in report.steps if step.name == "rsync")
    assert rsync_step.status == "ok"


def test_setup_probe_permission_error(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVKIT_RSYNC", str(_good_rsync(state_dir / "bin")))

    def _denied(core: DevkitCore, timeout_s: float = 5.0) -> list[object]:
        raise DevkitError(ErrorCode.PERMISSION_LOCAL_NETWORK, "denied")

    monkeypatch.setattr("steamos_devkit.discovery.discover", _denied)
    core = DevkitCore(state_dir=state_dir)
    report = run_setup(core)
    probe = next(step for step in report.steps if step.name == "local-network")
    if sys.platform == "darwin":
        assert probe.status == "error"
        assert not report.ok
    else:
        assert probe.status == "skipped"


def test_setup_probe_skipped_off_darwin(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVKIT_RSYNC", str(_good_rsync(state_dir / "bin")))
    monkeypatch.setattr(sys, "platform", "linux")
    core = DevkitCore(state_dir=state_dir)
    report = run_setup(core)
    probe = next(step for step in report.steps if step.name == "local-network")
    assert probe.status == "skipped"


def test_setup_cli_json(state_dir: Path) -> None:
    result = runner.invoke(cli.app, ["--state-dir", str(state_dir), "setup"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert [step["name"] for step in payload["steps"]] == [
        "state-dir",
        "keypair",
        "rsync",
        "ssh",
        "terminal",
        "local-network",
    ]


def test_setup_cli_exit_5_on_error(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVKIT_RSYNC", str(state_dir / "bin" / "absent-rsync"))
    result = runner.invoke(cli.app, ["--state-dir", str(state_dir), "setup", "--no-install"])
    assert result.exit_code == 5
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
