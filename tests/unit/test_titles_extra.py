"""Extra unit tests for titles.py — L3: ``cat`` sidecar error distinction.

A sidecar read must treat only the device's "No such file" answer as
absent; any other nonzero exit (permissions, ...) raises
REMOTE_COMMAND_FAILED instead of silently defaulting.
"""

from __future__ import annotations

from pathlib import Path

import paramiko
import pytest
from fake_ssh import FakeSSHClient, make_paramiko_patch

from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Device, TitleConfig
from steamos_devkit.titles import configure_title, inspect_title

GAMEID = "hello_triangle"


def _fake_rsync(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "rsync"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf 'rsync  version 3.5.1  protocol version 33\\n'\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@pytest.fixture
def core(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> DevkitCore:
    monkeypatch.setenv("DEVKIT_RSYNC", str(_fake_rsync(state_dir / "bin")))
    core = DevkitCore(state_dir=state_dir)
    core.state.ensure_layout()
    paramiko.RSAKey.generate(2048).write_private_key_file(str(core.state.key_path))
    core.state.devices.upsert(
        Device(name="deck", address="192.0.2.10", login="deck", added_via="explicit")
    )
    return core


def _patch_cat(monkeypatch: pytest.MonkeyPatch, stderr: bytes, exit_code: int) -> FakeSSHClient:
    return make_paramiko_patch(monkeypatch, commands=[(r"^cat ", b"", stderr, exit_code)])


def test_absent_sidecar_is_none_and_defaults(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_cat(monkeypatch, b"cat: /home/deck/x-argv.json: No such file or directory", 1)
    detail = inspect_title(core, "deck", GAMEID)
    assert detail.config == TitleConfig()


def test_permission_denied_sidecar_raises(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_cat(monkeypatch, b"cat: /home/deck/x-argv.json: Permission denied", 1)
    with pytest.raises(DevkitError) as exc_info:
        inspect_title(core, "deck", GAMEID)
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    assert exc_info.value.remote_exit_code == 1
    assert "Permission denied" in exc_info.value.details["stderr_tail"]


def test_other_cat_failure_raises(core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cat(monkeypatch, b"cat: read error: Input/output error", 1)
    with pytest.raises(DevkitError) as exc_info:
        inspect_title(core, "deck", GAMEID)
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED


def test_configure_merge_read_permission_denied_raises(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merge read in _write_config_sidecars honors the same distinction."""
    _patch_cat(monkeypatch, b"cat: /home/deck/x-settings.json: Permission denied", 1)
    with pytest.raises(DevkitError) as exc_info:
        configure_title(core, "deck", GAMEID, TitleConfig(settings={"steam_play": "1"}))
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
