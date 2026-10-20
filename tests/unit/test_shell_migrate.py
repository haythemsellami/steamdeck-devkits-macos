"""Unit tests for `devkit shell` and `devkit migrate` (spec section 10)."""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import paramiko
import pytest
from typer.testing import CliRunner

from steamos_devkit import cli
from steamos_devkit.models import Device

runner = CliRunner()


def _setup_state(state_dir: Path) -> None:
    from steamos_devkit.core import DevkitCore

    core = DevkitCore(state_dir=state_dir)
    core.state.ensure_layout()
    paramiko.RSAKey.generate(2048).write_private_key_file(str(core.state.key_path))
    core.state.devices.upsert(
        Device(name="deck", address="192.0.2.10", login="deck", added_via="explicit")
    )


class _FakePopen:
    def __init__(self, argv: list[str], **kwargs: object) -> None:
        self.argv = argv
        self.pid = 0
        launched.append(argv)


launched: list[list[str]] = []


@pytest.fixture(autouse=True)
def _no_real_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    launched.clear()
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)  # type: ignore[assignment]


def test_shell_launches_terminal_with_upstream_ssh_argv(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_state(state_dir)
    result = runner.invoke(cli.app, ["--state-dir", str(state_dir), "shell", "deck"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    (osascript_call,) = launched
    assert osascript_call[0].endswith("osascript")
    script = osascript_call[2]
    assert " -t " in script
    assert " -i " in script
    assert "deck@192.0.2.10" in script
    assert "devkit_rsa" in script
    assert "StrictHostKeyChecking=no" in script


def test_shell_without_key_exits_4(state_dir: Path) -> None:
    from steamos_devkit.core import DevkitCore

    core = DevkitCore(state_dir=state_dir)
    core.state.ensure_layout()
    core.state.devices.upsert(
        Device(name="deck", address="192.0.2.10", login="deck", added_via="explicit")
    )
    result = runner.invoke(cli.app, ["--state-dir", str(state_dir), "shell", "deck"])
    assert result.exit_code == 4  # PAIRING_FAILED (spec section 10)
    assert launched == []


def test_shell_unknown_device_exits_3(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_state(state_dir)
    monkeypatch.setattr("steamos_devkit.discovery.resolve_name", lambda *a, **kw: None)
    result = runner.invoke(cli.app, ["--state-dir", str(state_dir), "shell", "nosuchdevice"])
    assert result.exit_code == 3


def _legacy_dir(tmp_path: Path) -> Path:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "devkit_rsa").write_text("PRIVATE KEY MATERIAL", encoding="utf-8")
    (legacy / "devkit_rsa.pub").write_text("ssh-rsa AAAA devkit-client:x@y", encoding="utf-8")
    return legacy


def test_migrate_imports_legacy_keys_atomically_at_0400(
    state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = _legacy_dir(tmp_path)
    monkeypatch.setattr("steamos_devkit.config.platform_default_state_dir", lambda: legacy)
    result = runner.invoke(cli.app, ["--state-dir", str(state_dir), "migrate"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["keys_imported"] is True
    key = state_dir / "keys" / "devkit_rsa"
    assert key.read_text(encoding="utf-8") == "PRIVATE KEY MATERIAL"
    # M6: the key was created at 0400 — no weaker-permission window
    assert stat.S_IMODE(key.stat().st_mode) == 0o400
    # legacy source untouched
    assert (legacy / "devkit_rsa").exists()


def test_migrate_never_overwrites_existing_key(
    state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_state(state_dir)  # creates our own key first
    legacy = _legacy_dir(tmp_path)
    monkeypatch.setattr("steamos_devkit.config.platform_default_state_dir", lambda: legacy)
    before = (state_dir / "keys" / "devkit_rsa").read_bytes()
    result = runner.invoke(cli.app, ["--state-dir", str(state_dir), "migrate"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["keys_imported"] is False
    assert any("already exist" in s for s in payload["skipped"])
    assert (state_dir / "keys" / "devkit_rsa").read_bytes() == before
