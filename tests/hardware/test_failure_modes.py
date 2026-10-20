"""Failure modes on hardware: honest errors, interruption, unreachable devices.

Covers hardware-matrix rows "Failed-launch handling", "Interrupted upload +
resume", and "Disconnect mid-deploy" (the disconnect path is simulated with
an unreachable TEST-NET selector — a mid-upload cable pull maps to the same
DEVICE_UNREACHABLE/remote-failure codes, surfaced by the same code paths).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from steamos_devkit import pairing
from steamos_devkit import status as status_mod
from steamos_devkit import titles as titles_mod
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import DeploySpec, JobState, TitleConfig

HwRecord = Callable[..., None]

_UNREACHABLE_IP = "192.0.2.1"  # TEST-NET-1 (RFC 5737): guaranteed unroutable
_FAIL_FAST_BUDGET_S = 20.0  # connect timeout is 5 s; generous LAN margin
_PROGRESS_DEADLINE_S = 60.0
_BLOB_BYTES = 50 * 1024 * 1024  # 4 blobs ~= 200 MiB


def test_failed_launch_is_honest(
    core: DevkitCore,
    device_selector: str,
    hardware_gameid: str,
    tmp_path: Path,
    hwrecord: HwRecord,
    force_delete: Callable[[DevkitCore, str, str], None],
) -> None:
    """Deploy a title whose entrypoint does not exist, then launch it.

    Steam may accept the launch request (the process dies later on the device)
    or refuse it; both are honest outcomes. What must never happen is a hang
    or a fabricated readiness signal.
    """
    source = tmp_path / "broken"
    source.mkdir()
    (source / "README.txt").write_text("no entrypoint here\n", encoding="utf-8")
    config = TitleConfig(entrypoint="no/such-binary.sh")
    try:
        spec = DeploySpec(
            device=device_selector,
            gameid=hardware_gameid,
            source_dir=source,
            config=config,
        )
        try:
            info = titles_mod.deploy(core, device_selector, spec).wait()
        except DevkitError as err:
            # the device validates the entrypoint at registration time and
            # refuses — that IS the honest failure, arriving early
            assert err.code == ErrorCode.REMOTE_COMMAND_FAILED
            assert "doesn't exist" in err.message or "registration" in err.message.lower()
            hwrecord("failed_launch", "pass", outcome="registration-refused")
            return
        assert info.state == JobState.SUCCEEDED

        started = time.monotonic()
        outcome: str
        try:
            result = titles_mod.launch_title(core, device_selector, hardware_gameid)
        except DevkitError as err:
            assert err.code == ErrorCode.REMOTE_COMMAND_FAILED
            outcome = f"error:{err.code}"
        else:
            if result.launched:
                # launch accepted; readiness must stay honestly unknown
                assert result.readiness == "unknown"
                outcome = "accepted-unverifiable"
            else:
                outcome = "refused"
        elapsed = time.monotonic() - started
        assert elapsed < _FAIL_FAST_BUDGET_S, f"launch took {elapsed:.1f}s (hang?)"
    except Exception:
        force_delete(core, device_selector, hardware_gameid)
        raise
    hwrecord("failed_launch", "pass", outcome=outcome, elapsed_s=round(elapsed, 1))


def test_interrupted_upload_then_redeploy(
    core: DevkitCore,
    device_selector: str,
    hardware_gameid: str,
    tmp_path: Path,
    hwrecord: HwRecord,
    force_delete: Callable[[DevkitCore, str, str], None],
) -> None:
    """Cancel a ~200 MiB deploy mid-transfer; the journal records cancelled;
    a redeploy to completion succeeds (remote partials kept via --partial)."""
    source = tmp_path / "big-payload"
    source.mkdir()
    for index in range(4):
        (source / f"blob{index}.bin").write_bytes(os.urandom(_BLOB_BYTES))
    entrypoint = source / "run.sh"
    entrypoint.write_text("#!/bin/sh\necho zz_devkit_test alive\n", encoding="utf-8")
    entrypoint.chmod(0o755)
    try:
        spec = DeploySpec(
            device=device_selector,
            gameid=hardware_gameid,
            source_dir=source,
            register=False,
        )
        handle = titles_mod.deploy(core, device_selector, spec)
        deadline = time.monotonic() + _PROGRESS_DEADLINE_S
        while time.monotonic() < deadline:
            progress = handle.info().progress
            if progress is not None and progress.phase == "transferring":
                break
            time.sleep(0.1)
        else:
            handle.cancel()
            pytest.fail("no transferring progress event within the deadline")
        handle.cancel()
        with pytest.raises(DevkitError) as exc_info:
            handle.wait()
        assert exc_info.value.code == ErrorCode.CANCELLED
        assert handle.info().state == JobState.CANCELLED

        resumed = titles_mod.deploy(
            core,
            device_selector,
            DeploySpec(
                device=device_selector,
                gameid=hardware_gameid,
                source_dir=source,
                config=TitleConfig(entrypoint="run.sh"),
            ),
        )
        info = resumed.wait()
        assert info.state == JobState.SUCCEEDED
    except Exception:
        force_delete(core, device_selector, hardware_gameid)
        raise
    hwrecord("interrupted_upload", "pass", redeployed=True)


def test_unreachable_device_fails_fast(core: DevkitCore, hwrecord: HwRecord) -> None:
    """A transient selector at an unroutable TEST-NET IP -> DEVICE_UNREACHABLE."""
    pairing.ensure_key(core)  # key-only guard would otherwise fire first
    started = time.monotonic()
    with pytest.raises(DevkitError) as exc_info:
        status_mod.get_status(core, _UNREACHABLE_IP)
    elapsed = time.monotonic() - started
    assert exc_info.value.code == ErrorCode.DEVICE_UNREACHABLE
    assert elapsed < _FAIL_FAST_BUDGET_S, f"unreachable probe took {elapsed:.1f}s (hang?)"
    hwrecord("disconnect_mid_status", "pass", elapsed_s=round(elapsed, 1))
