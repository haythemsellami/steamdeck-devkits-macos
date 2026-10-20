"""Unit tests for jobs.runner (spec section 7.2)."""

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.jobs import JobContext, JobRunner, JobStore
from steamos_devkit.models import JobKind, JobState


@pytest.fixture
def runner(tmp_path: Path) -> JobRunner:
    return JobRunner(JobStore(tmp_path / "jobs"))


def test_success_lifecycle(runner: JobRunner) -> None:
    handle = runner.submit(lambda ctx: "done", kind=JobKind.DEPLOY, device="deck")
    info = handle.wait()
    assert info.state == JobState.SUCCEEDED
    assert info.message == "done"
    assert info.started_at is not None and info.finished_at is not None


def test_failure_maps_devkit_error(runner: JobRunner) -> None:
    def fn(ctx: JobContext) -> None:
        raise DevkitError(ErrorCode.REMOTE_COMMAND_FAILED, "remote said no")

    handle = runner.submit(fn, kind=JobKind.DEPLOY)
    with pytest.raises(DevkitError) as exc_info:
        handle.wait()
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    assert handle.info().state == JobState.FAILED


def test_unexpected_error_becomes_internal(runner: JobRunner) -> None:
    def fn(ctx: JobContext) -> None:
        raise RuntimeError("surprise")

    handle = runner.submit(fn, kind=JobKind.DEPLOY)
    with pytest.raises(DevkitError):
        handle.wait()
    info = handle.info()
    assert info.state == JobState.FAILED
    assert info.code == ErrorCode.INTERNAL
    assert info.message is not None and "RuntimeError" in info.message


def test_cancel_kills_child_process_group(runner: JobRunner, tmp_path: Path) -> None:
    marker = tmp_path / "child-was-killed"

    def fn(ctx: JobContext) -> None:
        ctx.run_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
        )

    handle = runner.submit(fn, kind=JobKind.DEPLOY)
    time.sleep(0.4)  # let the child start
    handle.cancel()
    with pytest.raises(DevkitError) as exc_info:
        handle.wait()
    assert exc_info.value.code == ErrorCode.CANCELLED
    assert handle.info().state == JobState.CANCELLED
    assert not marker.exists()


def test_cancel_runs_cleanup_hook(runner: JobRunner) -> None:
    cleaned: list[str] = []

    def fn(ctx: JobContext) -> None:
        ctx.sleep(60)

    def cleanup(ctx: JobContext) -> None:
        cleaned.append(ctx.job_id)

    handle = runner.submit(fn, kind=JobKind.DEPLOY, cleanup=cleanup)
    time.sleep(0.2)
    handle.cancel()
    with pytest.raises(DevkitError):
        handle.wait()
    assert cleaned == [handle.job_id]


def test_timeout_fires(runner: JobRunner) -> None:
    def fn(ctx: JobContext) -> None:
        ctx.sleep(60)

    handle = runner.submit(fn, kind=JobKind.DEPLOY, timeout_s=1)
    with pytest.raises(DevkitError) as exc_info:
        handle.wait()
    assert exc_info.value.code == ErrorCode.TIMEOUT
    assert handle.info().state == JobState.TIMEOUT


def test_run_process_captures_cr_records(runner: JobRunner) -> None:
    lines: list[str] = []
    handle = runner.submit(
        lambda ctx: _emit_and_finish(ctx, lines),
        kind=JobKind.DEPLOY,
    )
    handle.wait()
    assert lines == ["one", "two", "three"]


def _emit_and_finish(ctx: JobContext, lines: list[str]) -> None:
    code = ctx.run_process(
        [sys.executable, "-c", "import sys; sys.stdout.write('one\\rtwo\\nthree\\n')"],
        on_output=lines.append,
    )
    assert code == 0
    return None


def test_recovery_sweep_on_construction(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs")
    stale = store.create(kind=JobKind.DEPLOY)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    store.start(stale.id, proc.pid)  # reaped above: definitely dead
    runner = JobRunner(store)
    assert runner.recovered == [stale.id]
    runner.shutdown()


def test_progress_lands_in_journal(runner: JobRunner) -> None:
    def fn(ctx: JobContext) -> None:
        ctx.progress("transferring", bytes_done=5, bytes_total=10, items_done=1)

    handle = runner.submit(fn, kind=JobKind.DEPLOY)
    handle.wait()
    info = handle.info()
    assert info.progress is not None
    assert info.progress.phase == "transferring"
    assert info.progress.bytes_done == 5


def test_run_process_preserves_utf8_char_split_across_reads(runner: JobRunner) -> None:
    # 'é' is 0xC3 0xA9 in UTF-8; the child flushes the first byte, sleeps so
    # the pump drains it as its own read, then flushes the rest.
    child = (
        "import sys, time; "
        "sys.stdout.buffer.write(b'h\\xc3'); sys.stdout.flush(); "
        "time.sleep(0.3); "
        "sys.stdout.buffer.write(b'\\xa9llo\\n'); sys.stdout.flush()"
    )
    lines: list[str] = []

    def fn(ctx: JobContext) -> None:
        code = ctx.run_process([sys.executable, "-c", child], on_output=lines.append)
        assert code == 0

    handle = runner.submit(fn, kind=JobKind.DEPLOY)
    handle.wait()
    assert lines == ["héllo"]


def test_run_process_flushes_unterminated_tail(runner: JobRunner) -> None:
    lines: list[str] = []

    def fn(ctx: JobContext) -> None:
        code = ctx.run_process(
            [sys.executable, "-c", "import sys; sys.stdout.write('partial')"],
            on_output=lines.append,
        )
        assert code == 0

    handle = runner.submit(fn, kind=JobKind.DEPLOY)
    handle.wait()
    assert lines == ["partial"]


# -- M7: deadline introspection, sleep cap, per-process timeout ------------------


def test_run_process_timeout_kills_child_and_journals_timeout(runner: JobRunner) -> None:
    started = time.monotonic()

    def fn(ctx: JobContext) -> None:
        ctx.run_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            timeout_s=1,
        )

    handle = runner.submit(fn, kind=JobKind.DEPLOY)
    with pytest.raises(DevkitError) as exc_info:
        handle.wait()
    assert exc_info.value.code == ErrorCode.TIMEOUT
    info = handle.info()
    assert info.state == JobState.TIMEOUT
    assert info.code == ErrorCode.TIMEOUT
    # killed promptly, not after the SIGTERM grace period let alone the 60 s sleep
    assert time.monotonic() - started < 30


def test_run_process_timeout_does_not_fire_when_child_is_fast(runner: JobRunner) -> None:
    lines: list[str] = []

    def fn(ctx: JobContext) -> None:
        code = ctx.run_process(
            [sys.executable, "-c", "print('quick')"],
            on_output=lines.append,
            timeout_s=10,
        )
        assert code == 0

    handle = runner.submit(fn, kind=JobKind.DEPLOY)
    handle.wait()
    assert lines == ["quick"]


def test_time_remaining_counts_down(runner: JobRunner) -> None:
    samples: list[float | None] = []

    def fn(ctx: JobContext) -> None:
        samples.append(ctx.time_remaining())
        ctx.sleep(0.2)
        samples.append(ctx.time_remaining())

    handle = runner.submit(fn, kind=JobKind.DEPLOY, timeout_s=30)
    handle.wait()
    first, second = samples
    assert first is not None and second is not None
    assert 0 < second < first <= 30


def test_time_remaining_none_without_timeout(runner: JobRunner) -> None:
    observations: list[tuple[float | None, bool]] = []

    def fn(ctx: JobContext) -> None:
        observations.append((ctx.time_remaining(), ctx.deadline_exceeded))

    handle = runner.submit(fn, kind=JobKind.DEPLOY)
    handle.wait()
    assert observations == [(None, False)]


def _bare_context(tmp_path: Path, deadline: float | None) -> JobContext:
    return JobContext(
        "job_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        JobStore(tmp_path / "jobs"),
        threading.Event(),
        lambda: False,
        deadline=deadline,
    )


def test_deadline_exceeded(tmp_path: Path) -> None:
    assert _bare_context(tmp_path, None).deadline_exceeded is False
    assert _bare_context(tmp_path, time.monotonic() + 60).deadline_exceeded is False
    assert _bare_context(tmp_path, time.monotonic() - 1).deadline_exceeded is True


def test_sleep_capped_at_deadline_raises_timeout(tmp_path: Path) -> None:
    # No runner timer involved: the deadline alone caps the wait and raises.
    ctx = _bare_context(tmp_path, time.monotonic() + 0.2)
    started = time.monotonic()
    with pytest.raises(DevkitError) as exc_info:
        ctx.sleep(60)
    assert exc_info.value.code == ErrorCode.TIMEOUT
    assert time.monotonic() - started < 5
