"""Append-only JSONL job journals (spec section 7.1).

Each job is a journal at ``jobs/<job_ulid>.jsonl``: one JSON event per line,
flushed and fsynced on every append. The public view (:class:`JobInfo`) is a
replay of the journal. Journals left in a non-terminal state by a dead process
are moved to ``interrupted`` by :meth:`JobStore.recover_interrupted`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import ulid

from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import JobInfo, JobKind, JobProgress, JobState

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^job_[0-9A-HJKMNP-TV-Z]{26}$")

_TERMINAL_EVENTS = {str(s): s for s in JobState if s.is_terminal}


def _pid_alive(pid: int) -> bool:
    """POSIX liveness probe; conservative False elsewhere (interrupts anyway)."""
    if sys.platform == "win32":
        return False  # no portable liveness check; falls back to interrupting
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class JobStore:
    """Persistence for job journals under the state root's ``jobs/`` dir."""

    def __init__(self, jobs_dir: Path) -> None:
        self._dir = jobs_dir

    @property
    def directory(self) -> Path:
        return self._dir

    def path_for(self, job_id: str) -> Path:
        """Journal path for an id; rejects malformed ids (traversal guard)."""
        if not _ID_RE.match(job_id):
            raise DevkitError(ErrorCode.JOB_NOT_FOUND, f"invalid job id: {job_id!r}")
        return self._dir / f"{job_id}.jsonl"

    # -- appends ----------------------------------------------------------------

    def create(
        self,
        *,
        kind: JobKind,
        device: str | None = None,
        gameid: str | None = None,
        spec: dict[str, Any] | None = None,
        timeout_s: int | None = None,
    ) -> JobInfo:
        job_id = f"job_{ulid.new()}"
        self._dir.mkdir(parents=True, exist_ok=True)
        created = _utcnow()
        self._append(
            job_id,
            {
                "t": "created",
                "ts": created.isoformat(),
                "job": {
                    "id": job_id,
                    "kind": str(kind),
                    "device": device,
                    "gameid": gameid,
                    "spec": spec or {},
                    "timeout_s": timeout_s,
                    "created_at": created.isoformat(),
                },
            },
        )
        return JobInfo(
            id=job_id,
            kind=kind,
            state=JobState.QUEUED,
            device=device,
            gameid=gameid,
            created_at=created,
            timeout_s=timeout_s,
        )

    def start(self, job_id: str, pid: int) -> None:
        self._append(job_id, {"t": "started", "ts": _utcnow().isoformat(), "pid": pid})

    def progress(self, job_id: str, progress: JobProgress) -> None:
        self._append(
            job_id,
            {
                "t": "progress",
                "ts": _utcnow().isoformat(),
                **progress.model_dump(exclude_none=True),
            },
        )

    def attach_artifact(self, job_id: str, artifact_id: str) -> None:
        self._append(
            job_id,
            {"t": "artifact", "ts": _utcnow().isoformat(), "artifact_id": artifact_id},
        )

    def finish(
        self,
        job_id: str,
        state: JobState,
        *,
        message: str | None = None,
        code: ErrorCode | None = None,
    ) -> None:
        if not state.is_terminal:
            raise DevkitError(
                ErrorCode.INTERNAL, f"finish() requires a terminal state, got {state}"
            )
        event: dict[str, Any] = {"t": str(state), "ts": _utcnow().isoformat()}
        if message is not None:
            event["message"] = message
        if code is not None:
            event["code"] = str(code)
        self._append(job_id, event)

    # -- reads -------------------------------------------------------------------

    def load(self, job_id: str) -> JobInfo:
        """Replay a journal into its current JobInfo.

        A malformed final line without a trailing newline is treated as a
        torn tail (process died mid-append, between write and fsync) and
        ignored; corruption anywhere else is STATE_CORRUPT.
        """
        path = self.path_for(job_id)
        if not path.exists():
            raise DevkitError(ErrorCode.JOB_NOT_FOUND, f"no such job: {job_id}")
        fields: dict[str, Any] = {"artifacts": []}
        state: JobState | None = None
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        for lineno, line in enumerate(lines, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                event: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError as e:
                if lineno == len(lines) and not content.endswith("\n"):
                    logger.warning(
                        "ignoring torn final journal line in %s (crash mid-append)", path
                    )
                    break
                raise DevkitError(
                    ErrorCode.STATE_CORRUPT, f"{path}:{lineno}: malformed journal line: {e}"
                ) from e
            self._apply(event, fields, path, lineno)
            t = event.get("t")
            if t == "created":
                state = JobState.QUEUED
            elif t in ("started", "progress") and state in (
                None,
                JobState.QUEUED,
                JobState.RUNNING,
            ):
                state = JobState.RUNNING
            elif t in _TERMINAL_EVENTS:
                state = _TERMINAL_EVENTS[t]
        if state is None or "kind" not in fields:
            raise DevkitError(ErrorCode.STATE_CORRUPT, f"{path}: journal has no created event")
        return JobInfo(state=state, **fields)

    def _apply(
        self, event: dict[str, Any], fields: dict[str, Any], path: Path, lineno: int
    ) -> None:
        t = event.get("t")
        if t == "created":
            job = event.get("job")
            if not isinstance(job, dict) or "kind" not in job or "id" not in job:
                raise DevkitError(
                    ErrorCode.STATE_CORRUPT, f"{path}:{lineno}: malformed created event"
                )
            try:
                fields["kind"] = JobKind(job["kind"])
            except ValueError as e:
                raise DevkitError(
                    ErrorCode.STATE_CORRUPT, f"{path}:{lineno}: unknown job kind {job['kind']!r}"
                ) from e
            fields["id"] = str(job["id"])
            fields["device"] = job.get("device")
            fields["gameid"] = job.get("gameid")
            fields["timeout_s"] = job.get("timeout_s")
            fields["created_at"] = _parse_ts(job.get("created_at")) or _utcnow()
        elif t == "started":
            fields["started_at"] = _parse_ts(event.get("ts"))
        elif t == "progress":
            snapshot = {
                k: v for k, v in event.items() if k in JobProgress.model_fields and v is not None
            }
            if snapshot:
                fields["progress"] = JobProgress.model_validate(snapshot)
        elif t == "artifact":
            artifact_id = event.get("artifact_id")
            if isinstance(artifact_id, str):
                fields["artifacts"].append(artifact_id)
        elif t in _TERMINAL_EVENTS:
            fields["finished_at"] = _parse_ts(event.get("ts"))
            message = event.get("message")
            code = event.get("code")
            result = event.get("result")  # tolerate {"result": {...}} terminal payloads
            if message is None and isinstance(result, dict):
                message = result.get("message")
                code = code if code is not None else result.get("code")
            fields["message"] = message
            fields["code"] = ErrorCode(code) if isinstance(code, str) else None

    def list_jobs(
        self, *, device: str | None = None, state: JobState | None = None
    ) -> list[JobInfo]:
        """All journals, oldest first, optionally filtered.

        Corrupt journals are skipped with a warning (direct ``load`` still
        raises STATE_CORRUPT): one damaged file must not brick the subsystem.
        """
        infos: list[JobInfo] = []
        for path in sorted(self._dir.glob("job_*.jsonl")):
            if not _ID_RE.match(path.stem):
                continue
            try:
                infos.append(self.load(path.stem))
            except DevkitError as e:
                if e.code == ErrorCode.STATE_CORRUPT:
                    logger.warning("skipping corrupt journal %s: %s", path, e.message)
                    continue
                raise
        # ULIDs from the same millisecond don't sort reliably by filename;
        # order by the journal-recorded creation time instead.
        infos.sort(key=lambda i: i.created_at)
        if device is not None:
            infos = [i for i in infos if i.device == device]
        if state is not None:
            infos = [i for i in infos if i.state == state]
        return infos

    def recover_interrupted(self) -> list[str]:
        """Mark journals left non-terminal by a DEAD process as interrupted.

        Journals are shared between short-lived CLI processes and a
        long-lived MCP server over one state root, so a non-terminal journal
        whose recorded pid is still alive belongs to another live process and
        must be left alone. Never-started journals (no pid) are interrupted:
        they carry no work, and a live owner converges them to a terminal
        state on completion anyway. pid reuse is a documented limitation.
        """
        recovered: list[str] = []
        for info in self.list_jobs():
            if info.state.is_terminal:
                continue
            pid = self._started_pid(self.path_for(info.id))
            if pid is not None and _pid_alive(pid):
                logger.info("leaving job %s alone: owned by live pid %d", info.id, pid)
                continue
            self.finish(
                info.id,
                JobState.INTERRUPTED,
                message="process exited before the job completed",
            )
            recovered.append(info.id)
        return recovered

    def _started_pid(self, path: Path) -> int | None:
        """The pid from the last ``started`` event, tolerating bad lines."""
        pid: int | None = None
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return None
        for line in content.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("t") == "started":
                candidate = event.get("pid")
                if isinstance(candidate, int):
                    pid = candidate
        return pid

    # -- internals -----------------------------------------------------------------

    def _append(self, job_id: str, event: dict[str, Any]) -> None:
        path = self.path_for(job_id)
        line = json.dumps(event, default=str, separators=(",", ":"))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
