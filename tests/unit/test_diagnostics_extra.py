"""Extra unit tests for diagnostics.py — L12: the Deck screenshot poll loop.

The ``/tmp/gamescope*.png`` wait must poll (more than one probe) until the
deadline expires, not exit after a single iteration.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import paramiko
import pytest
from fake_ssh import make_paramiko_patch

from steamos_devkit import diagnostics
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.jobs import JobContext
from steamos_devkit.models import Device

GET_STATUS = r"steamos-get-status --json"
XPROP = r"xprop -root -f GAMESCOPECTRL_DEBUG_REQUEST_SCREENSHOT"
FIND_SCREENSHOT_RE = r'find /tmp -maxdepth 1 -type f -name "gamescope\*\.png"'
FIND_SCREENSHOT_CMD = 'find /tmp -maxdepth 1 -type f -name "gamescope*.png"'


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
def device(core: DevkitCore) -> Device:
    entry = Device(name="deck", address="192.0.2.10", login="deck", added_via="explicit")
    core.state.devices.upsert(entry)
    return entry


def _fake_rsync(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "rsync"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf 'rsync  version 3.5.1  protocol version 33\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    def fast(self: JobContext, seconds: float) -> None:
        self._cancel.wait(min(seconds, 0.01))
        self.check_cancelled()

    monkeypatch.setattr(JobContext, "sleep", fast)


def test_screenshot_deck_polls_more_than_once_before_timeout(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_file: Callable[[str], str],
) -> None:
    _fast_sleep(monkeypatch)
    monkeypatch.setattr(diagnostics, "_SCREENSHOT_APPEAR_TIMEOUT_S", 0.05)
    monkeypatch.setenv("DEVKIT_RSYNC", str(_fake_rsync(tmp_path / "bin")))
    fake = make_paramiko_patch(
        monkeypatch,
        commands=[
            (GET_STATUS, fixture_file("steamos-get-status.json").encode(), b"", 0),
            (r"rm -f /tmp/gamescope", b"", b"", 0),
            (XPROP, b"", b"", 0),
            # find is unscripted: exit 127 -> treated as "not there yet"
        ],
    )
    job = diagnostics.screenshot(core, "deck")
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    find_calls = fake.commands_run.count(FIND_SCREENSHOT_CMD)
    assert find_calls > 1  # L12: the loop really polls; it does not give up after one probe
