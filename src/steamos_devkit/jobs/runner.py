"""Job execution: worker pool, cancellation, timeouts, process groups (spec 7.2).

A job function receives a :class:`JobContext` and returns an optional human
message. Cancellation is cooperative (``ctx.check_cancelled()`` /
``ctx.run_process()`` honor it) and journal-backed: every transition lands in
the journal before this module returns, so state survives process death.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.jobs.progress import split_records
from steamos_devkit.jobs.store import JobStore
from steamos_devkit.models import JobInfo, JobKind, JobProgress, JobState

logger = logging.getLogger(__name__)

_TERM_GRACE_S = 5.0
_POLL_INTERVAL_S = 0.05


class JobContext:
    """A job's handle on its own journal, cancellation, and child processes."""

    def __init__(
        self,
        job_id: str,
        store: JobStore,
        cancel_event: threading.Event,
        timed_out: Callable[[], bool],
    ) -> None:
        self.job_id = job_id
        self._store = store
        self._cancel = cancel_event
        self._timed_out = timed_out

    # -- cancellation ------------------------------------------------------------

    def check_cancelled(self) -> None:
        if self._cancel.is_set():
            if self._timed_out():
                raise DevkitError(ErrorCode.TIMEOUT, f"job {self.job_id} exceeded its timeout")
            raise DevkitError(ErrorCode.CANCELLED, f"job {self.job_id} was cancelled")

    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def sleep(self, seconds: float) -> None:
        """Cancel-aware sleep: returns early (then raises) if cancelled."""
        self._cancel.wait(seconds)
        self.check_cancelled()

    # -- progress -------------------------------------------------------------------

    def progress(
        self,
        phase: str,
        *,
        bytes_done: int | None = None,
        bytes_total: int | None = None,
        items_done: int | None = None,
        rate_bps: float | None = None,
    ) -> None:
        self._store.progress(
            self.job_id,
            JobProgress(
                phase=phase,
                bytes_done=bytes_done,
                bytes_total=bytes_total,
                items_done=items_done,
                rate_bps=rate_bps,
            ),
        )

    # -- child processes -------------------------------------------------------------

    def run_process(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        on_output: Callable[[str], None] | None = None,
    ) -> int:
        """Run a child in its own process group; kill the group on cancel.

        Returns the exit code; the caller maps nonzero codes. Output is pumped
        in both \\r- and \\n-separated records (rsync progress2) to on_output.
        """
        args = [os.fspath(a) for a in argv]
        proc = subprocess.Popen(  # noqa: S603 - argv is built by callers, never a shell
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
        try:
            return self._pump(proc, on_output)
        except BaseException:
            self._kill_group(proc, signal.SIGTERM)
            if not self._wait_deadline(proc, _TERM_GRACE_S):
                self._kill_group(proc, signal.SIGKILL)
                proc.wait()
            raise
        finally:
            if proc.stdout is not None:
                proc.stdout.close()

    def _pump(self, proc: subprocess.Popen[bytes], on_output: Callable[[str], None] | None) -> int:
        assert proc.stdout is not None
        os.set_blocking(proc.stdout.fileno(), False)
        buffer = b""

        def drain(chunk: bytes) -> None:
            nonlocal buffer
            buffer += chunk
            # Split on raw separator bytes and decode only complete records:
            # a multi-byte UTF-8 character straddling two reads stays intact
            # in the byte tail instead of decaying into a permanent U+FFFD.
            cut = max(buffer.rfind(b"\r"), buffer.rfind(b"\n"))
            if cut < 0:
                return
            complete, buffer = buffer[:cut], buffer[cut + 1 :]
            if on_output is not None:
                for record in split_records(complete.decode("utf-8", errors="replace")):
                    on_output(record)

        while True:
            self.check_cancelled()
            chunk = proc.stdout.read(4096)  # None: no data right now; b"": EOF
            if chunk:
                drain(chunk)
            if proc.poll() is not None:
                tail = proc.stdout.read() or b""
                if tail:
                    drain(tail)
                if buffer.strip() and on_output is not None:
                    on_output(buffer.decode("utf-8", errors="replace"))
                return proc.returncode if proc.returncode is not None else -1
            if not chunk:
                self._cancel.wait(_POLL_INTERVAL_S)

    @staticmethod
    def _kill_group(proc: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    @staticmethod
    def _wait_deadline(proc: subprocess.Popen[bytes], timeout_s: float) -> bool:
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return False
        return True


class JobHandle:
    """Caller-side handle for a submitted job."""

    def __init__(
        self,
        job_id: str,
        store: JobStore,
        cancel_event: threading.Event,
        future: Future[None],
    ) -> None:
        self.job_id = job_id
        self._store = store
        self._cancel = cancel_event
        self._future = future

    def info(self) -> JobInfo:
        return self._store.load(self.job_id)

    def cancel(self) -> JobInfo:
        self._cancel.set()
        return self.info()

    def wait(self) -> JobInfo:
        """Block to terminal state; raises DevkitError for failed/cancelled/timeout."""
        self._future.result()
        info = self.info()
        if info.state == JobState.SUCCEEDED:
            return info
        if info.state in (JobState.CANCELLED, JobState.TIMEOUT, JobState.FAILED):
            raise DevkitError(
                info.code or ErrorCode.INTERNAL,
                info.message or f"job ended in state {info.state}",
            )
        raise DevkitError(
            ErrorCode.INTERNAL, f"job {info.id} wait() returned in non-terminal {info.state}"
        )


JobFunction = Callable[[JobContext], str | None]
CleanupFunction = Callable[[JobContext], None]


class JobRunner:
    """Submits jobs to a worker pool with journal-backed lifecycle (spec 7.2)."""

    def __init__(self, store: JobStore, max_workers: int = 4) -> None:
        self._store = store
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="devkit-job")
        # process start: journals left non-terminal by a dead process are interrupted
        self.recovered: list[str] = store.recover_interrupted()

    @property
    def store(self) -> JobStore:
        return self._store

    def submit(
        self,
        fn: JobFunction,
        *,
        kind: JobKind,
        device: str | None = None,
        gameid: str | None = None,
        spec: dict[str, object] | None = None,
        timeout_s: int | None = None,
        cleanup: CleanupFunction | None = None,
    ) -> JobHandle:
        info = self._store.create(
            kind=kind, device=device, gameid=gameid, spec=spec, timeout_s=timeout_s
        )
        cancel_event = threading.Event()
        future = self._pool.submit(self._drive, info.id, fn, cancel_event, timeout_s, cleanup)
        return JobHandle(info.id, self._store, cancel_event, future)

    def _drive(
        self,
        job_id: str,
        fn: JobFunction,
        cancel_event: threading.Event,
        timeout_s: int | None,
        cleanup: CleanupFunction | None,
    ) -> None:
        timed_out = threading.Event()

        def _on_timeout() -> None:
            timed_out.set()
            cancel_event.set()

        timer: threading.Timer | None = None
        if timeout_s is not None and timeout_s > 0:
            timer = threading.Timer(timeout_s, _on_timeout)
            timer.daemon = True
            timer.start()

        ctx = JobContext(job_id, self._store, cancel_event, timed_out.is_set)
        self._store.start(job_id, os.getpid())
        try:
            message = fn(ctx)
            ctx.check_cancelled()  # a cancel landing after fn returned still counts
            self._store.finish(job_id, JobState.SUCCEEDED, message=message)
        except DevkitError as err:
            if err.code in (ErrorCode.CANCELLED, ErrorCode.TIMEOUT):
                state = JobState.CANCELLED if err.code == ErrorCode.CANCELLED else JobState.TIMEOUT
                if cleanup is not None:
                    try:
                        cleanup(ctx)
                    except Exception:
                        logger.exception("cleanup hook failed for %s", job_id)
            else:
                state = JobState.FAILED
            self._store.finish(job_id, state, message=err.message, code=err.code)
        except Exception as err:  # unexpected: journal INTERNAL failure, never lose it
            logger.exception("job %s failed unexpectedly", job_id)
            self._store.finish(
                job_id,
                JobState.FAILED,
                message=f"{type(err).__name__}: {err}",
                code=ErrorCode.INTERNAL,
            )
        finally:
            if timer is not None:
                timer.cancel()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
