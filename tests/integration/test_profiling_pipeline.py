"""Integration tests for the gpu_trace and rgp_capture job pipelines.

Exercises the full flow over the fake-SSH harness: remote command sequencing,
the gpu-trace start+retry fallback, the RGP appear/grow/stabilize polling
loops, artifact registration/attachment, size verification, and the
cancel-mid-capture cleanup hook (spec section 7.4).
"""

import hashlib
import time
from pathlib import Path

import paramiko
import pytest
from fake_ssh import ScriptedResponse, make_paramiko_patch

from steamos_devkit import profiling
from steamos_devkit.artifacts import get_artifact
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.jobs import JobContext
from steamos_devkit.models import Device, JobState

TRACE_REMOTE = "/tmp/ABC123-trace.zip"
TRACE_PAYLOAD = b"\x50\x4b fake-trace-zip"
RGP_REMOTE = "/tmp/amd_trace.rgp"
RGP_PAYLOAD = b"R" * 200

MKTEMP = r"mktemp -p /tmp XXXXXXXXX-trace\.zip"
RM_TRACE = r"rm /tmp/ABC123-trace\.zip"
CAPTURE = r"gpu-trace --capture --no-gpuvis -o /tmp/ABC123-trace\.zip"
START = r"gpu-trace --start"


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


def _fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep polling-loop tests fast while preserving cancel semantics."""

    def fast(self: JobContext, seconds: float) -> None:
        self._cancel.wait(min(seconds, 0.01))
        self.check_cancelled()

    monkeypatch.setattr(JobContext, "sleep", fast)


def _gpu_trace_success_rules() -> list:
    return [
        (MKTEMP, f"{TRACE_REMOTE}\n".encode(), b"", 0),
        (RM_TRACE, b"", b"", 0),
        (CAPTURE, b"", b"", 0),
        (r"rm -f", b"", b"", 0),
    ]


# --- gpu_trace ---------------------------------------------------------------


def test_gpu_trace_success_lands_artifact(
    core: DevkitCore, paired_key: Path, device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = make_paramiko_patch(
        monkeypatch,
        commands=_gpu_trace_success_rules(),
        filesystem={TRACE_REMOTE: TRACE_PAYLOAD},
    )
    job = profiling.gpu_trace(core, "deck")
    info = job.wait()
    assert info.state == JobState.SUCCEEDED
    assert info.kind.value == "gpu_trace"
    assert len(info.artifacts) == 1

    artifact = get_artifact(core.state, info.artifacts[0])
    assert artifact.kind == "gpu_trace"
    assert artifact.media_type == "application/zip"
    assert artifact.device == "deck"
    assert artifact.job_id == job.job_id
    assert artifact.remote_source == TRACE_REMOTE
    assert artifact.bytes == len(TRACE_PAYLOAD)
    assert artifact.sha256 == hashlib.sha256(TRACE_PAYLOAD).hexdigest()
    assert artifact.path.read_bytes() == TRACE_PAYLOAD

    # exact upstream sequence: mktemp, rm, capture, then rm -f housekeeping
    assert fake.commands_run == [
        "mktemp -p /tmp XXXXXXXXX-trace.zip",
        f"rm {TRACE_REMOTE}",
        f"gpu-trace --capture --no-gpuvis -o {TRACE_REMOTE}",
        f"rm -f {TRACE_REMOTE}",
    ]


def test_gpu_trace_retries_after_start_when_capture_fails(
    core: DevkitCore, paired_key: Path, device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fast_sleep(monkeypatch)
    fake = make_paramiko_patch(
        monkeypatch,
        commands=[
            (MKTEMP, f"{TRACE_REMOTE}\n".encode(), b"", 0),
            (RM_TRACE, b"", b"", 0),
            ScriptedResponse(
                CAPTURE, b"", b"Failed to capture trace: not tracing\n", 1, max_uses=1
            ),
            (START, b"", b"", 0),
            (CAPTURE, b"", b"", 0),
            (r"rm -f", b"", b"", 0),
        ],
        filesystem={TRACE_REMOTE: TRACE_PAYLOAD},
    )
    info = profiling.gpu_trace(core, "deck").wait()
    assert info.state == JobState.SUCCEEDED
    captures = [c for c in fake.commands_run if c.startswith("gpu-trace --capture")]
    assert len(captures) == 2
    assert "gpu-trace --start" in fake.commands_run


def test_gpu_trace_command_not_found_still_retries_once(
    core: DevkitCore, paired_key: Path, device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    # upstream reports "command not found" (exit 127) on devices without the
    # gpu-trace package; the capture is retried once after gpu-trace --start
    _fast_sleep(monkeypatch)
    fake = make_paramiko_patch(
        monkeypatch,
        commands=[
            (MKTEMP, f"{TRACE_REMOTE}\n".encode(), b"", 0),
            (RM_TRACE, b"", b"", 0),
            ScriptedResponse(
                CAPTURE, b"", b"bash: gpu-trace: command not found\n", 127, max_uses=1
            ),
            (START, b"", b"", 0),
            (CAPTURE, b"", b"", 0),
            (r"rm -f", b"", b"", 0),
        ],
        filesystem={TRACE_REMOTE: TRACE_PAYLOAD},
    )
    info = profiling.gpu_trace(core, "deck").wait()
    assert info.state == JobState.SUCCEEDED
    assert "gpu-trace --start" in fake.commands_run


def test_gpu_trace_persistent_failure_fails_job(
    core: DevkitCore, paired_key: Path, device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fast_sleep(monkeypatch)
    make_paramiko_patch(
        monkeypatch,
        commands=[
            (MKTEMP, f"{TRACE_REMOTE}\n".encode(), b"", 0),
            (RM_TRACE, b"", b"", 0),
            (CAPTURE, b"", b"bash: gpu-trace: command not found\n", 127),
            (START, b"", b"bash: gpu-trace: command not found\n", 127),
        ],
    )
    job = profiling.gpu_trace(core, "deck")
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    info = job.info()
    assert info.state == JobState.FAILED
    assert info.code == ErrorCode.REMOTE_COMMAND_FAILED


def test_gpu_trace_cancel_mid_capture_runs_cleanup(
    core: DevkitCore, paired_key: Path, device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    # real ctx.sleep here: the cancel must interrupt the post-`--start` wait
    fake = make_paramiko_patch(
        monkeypatch,
        commands=[
            (MKTEMP, f"{TRACE_REMOTE}\n".encode(), b"", 0),
            (RM_TRACE, b"", b"", 0),
            (CAPTURE, b"", b"Failed to capture trace\n", 1),
            (START, b"", b"", 0),
        ],
    )
    job = profiling.gpu_trace(core, "deck")
    deadline = time.monotonic() + 5
    while "gpu-trace --start" not in fake.commands_run:
        assert time.monotonic() < deadline, "job never reached gpu-trace --start"
        time.sleep(0.01)
    job.cancel()
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert exc_info.value.code == ErrorCode.CANCELLED
    info = job.info()
    assert info.state == JobState.CANCELLED
    assert info.code == ErrorCode.CANCELLED
    # spec section 7.4: cleanup removes the remote temp zip
    assert f"rm -f {TRACE_REMOTE}" in fake.commands_run
    assert info.artifacts == []


def test_gpu_trace_mktemp_empty_output_fails(
    core: DevkitCore, paired_key: Path, device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_paramiko_patch(monkeypatch, commands=[(MKTEMP, b"\n", b"", 0)])
    job = profiling.gpu_trace(core, "deck")
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    assert "mktemp" in (exc_info.value.message or "")


# --- rgp_capture -------------------------------------------------------------

RGP_RM = r"rm -f /tmp/\*\.rgp"
RGP_TOUCH = r"touch /tmp/rgp\.trigger"
RGP_LS = r"ls -1t /tmp/\*\.rgp"
RGP_STAT = r"stat -c %s /tmp/amd_trace\.rgp"


def _rgp_rules() -> list:
    return [
        (RGP_RM, b"", b"", 0),
        (RGP_TOUCH, b"", b"", 0),
        # capture appears on the third poll
        ScriptedResponse(RGP_LS, b"", b"ls: No such file or directory\n", 2, max_uses=2),
        ScriptedResponse(RGP_LS, f"{RGP_REMOTE}\n".encode(), b"", 0),
        # grows, then holds stable
        ScriptedResponse(RGP_STAT, b"100\n", b"", 0, max_uses=1),
        ScriptedResponse(RGP_STAT, b"200\n", b"", 0, max_uses=1),
        ScriptedResponse(RGP_STAT, b"200\n", b"", 0),
    ]


def test_rgp_capture_appear_grow_stabilize_then_artifact(
    core: DevkitCore, paired_key: Path, device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fast_sleep(monkeypatch)
    fake = make_paramiko_patch(
        monkeypatch,
        commands=_rgp_rules(),
        filesystem={RGP_REMOTE: RGP_PAYLOAD},
    )
    job = profiling.rgp_capture(core, "deck")
    info = job.wait()
    assert info.state == JobState.SUCCEEDED
    assert info.kind.value == "rgp"
    assert len(info.artifacts) == 1

    artifact = get_artifact(core.state, info.artifacts[0])
    assert artifact.kind == "rgp"
    assert artifact.remote_source == RGP_REMOTE
    assert artifact.bytes == len(RGP_PAYLOAD)
    assert artifact.path.read_bytes() == RGP_PAYLOAD

    commands = fake.commands_run
    assert commands[0] == "rm -f /tmp/*.rgp"
    assert commands[1] == "touch /tmp/rgp.trigger"
    assert commands[2:5] == ["ls -1t /tmp/*.rgp"] * 3
    assert commands[5:] == [f"stat -c %s {RGP_REMOTE}"] * 5


def test_rgp_capture_truncated_download_fails(
    core: DevkitCore, paired_key: Path, device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fast_sleep(monkeypatch)
    make_paramiko_patch(
        monkeypatch,
        commands=_rgp_rules(),  # remote size stabilizes at 200
        filesystem={RGP_REMOTE: RGP_PAYLOAD[:150]},  # but the transfer delivers less
    )
    job = profiling.rgp_capture(core, "deck")
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    assert "truncated" in (exc_info.value.message or "")
    assert job.info().artifacts == []


def test_rgp_capture_appearance_timeout(
    core: DevkitCore, paired_key: Path, device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fast_sleep(monkeypatch)
    monkeypatch.setattr(profiling, "_RGP_APPEAR_TIMEOUT_S", 0.2)
    make_paramiko_patch(
        monkeypatch,
        commands=[
            (RGP_RM, b"", b"", 0),
            (RGP_TOUCH, b"", b"", 0),
            (RGP_LS, b"", b"ls: No such file or directory\n", 2),
        ],
    )
    job = profiling.rgp_capture(core, "deck")
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    assert "Enable Graphics Profiling" in (exc_info.value.message or "")


def test_rgp_capture_stability_timeout(
    core: DevkitCore, paired_key: Path, device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fast_sleep(monkeypatch)
    monkeypatch.setattr(profiling, "_RGP_STABLE_TIMEOUT_S", 0.2)
    # the capture keeps growing: never three equal consecutive samples
    growing = [
        ScriptedResponse(RGP_STAT, f"{i * 100}\n".encode(), b"", 0, max_uses=1)
        for i in range(1, 40)
    ]
    make_paramiko_patch(
        monkeypatch,
        commands=[
            (RGP_RM, b"", b"", 0),
            (RGP_TOUCH, b"", b"", 0),
            (RGP_LS, f"{RGP_REMOTE}\n".encode(), b"", 0),
            *growing,
        ],
    )
    job = profiling.rgp_capture(core, "deck")
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    assert "finish writing" in (exc_info.value.message or "")


def test_rgp_capture_garbage_stat_fails(
    core: DevkitCore, paired_key: Path, device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fast_sleep(monkeypatch)
    make_paramiko_patch(
        monkeypatch,
        commands=[
            (RGP_RM, b"", b"", 0),
            (RGP_TOUCH, b"", b"", 0),
            (RGP_LS, f"{RGP_REMOTE}\n".encode(), b"", 0),
            (RGP_STAT, b"not-a-number\n", b"", 0),
        ],
    )
    job = profiling.rgp_capture(core, "deck")
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    assert "unexpected stat output" in (exc_info.value.message or "")


def test_rgp_cleanup_runs_on_cancel(
    core: DevkitCore, paired_key: Path, device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ls never finds a capture; cancel while the job polls
    fake = make_paramiko_patch(
        monkeypatch,
        commands=[
            (RGP_RM, b"", b"", 0),
            (RGP_TOUCH, b"", b"", 0),
            (RGP_LS, b"", b"ls: No such file or directory\n", 2),
        ],
    )
    job = profiling.rgp_capture(core, "deck")
    deadline = time.monotonic() + 5
    while "ls -1t /tmp/*.rgp" not in fake.commands_run:
        assert time.monotonic() < deadline, "job never reached the ls poll"
        time.sleep(0.01)
    job.cancel()
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert exc_info.value.code == ErrorCode.CANCELLED
    # spec section 7.4: trigger and partial captures are removed
    assert "rm -f /tmp/rgp.trigger /tmp/*.rgp" in fake.commands_run
