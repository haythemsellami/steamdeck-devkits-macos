"""Shared CLI machinery (spec section 10).

Command modules import from here — never from ``cli/__init__.py`` — to avoid
circular imports. Each command module exposes ``register(app: typer.Typer)``;
``__init__`` wires them in its registration section.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import typer

from steamos_devkit.artifacts import EXCERPT_MAX_BYTES, EXCERPT_MAX_LINES, get_artifact
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.jobs import JobHandle
from steamos_devkit.models import JobInfo, Result

logger = logging.getLogger(__name__)

# spec section 10 exit-code table
_EXIT_CODES: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION: 2,
    ErrorCode.DEVICE_NOT_FOUND: 3,
    ErrorCode.DEVICE_UNREACHABLE: 3,
    ErrorCode.AMBIGUOUS_DEVICE: 3,
    ErrorCode.PAIRING_DECLINED: 4,
    ErrorCode.PAIRING_FAILED: 4,
    ErrorCode.DEPENDENCY_MISSING: 5,
    ErrorCode.OPENRSYNC_DETECTED: 5,
    ErrorCode.PERMISSION_LOCAL_NETWORK: 6,
    ErrorCode.PERMISSION_KEY_FILE: 6,
    ErrorCode.REMOTE_COMMAND_FAILED: 7,
    ErrorCode.TIMEOUT: 8,
    ErrorCode.PAIRING_TIMEOUT: 8,
    ErrorCode.CANCELLED: 9,
    ErrorCode.UNSUPPORTED: 10,
    # JOB_NOT_FOUND / ARTIFACT_NOT_FOUND: referencing a job or artifact that
    # does not exist is a usage error.
    ErrorCode.JOB_NOT_FOUND: 2,
    ErrorCode.ARTIFACT_NOT_FOUND: 2,
    ErrorCode.STATE_CORRUPT: 1,
    ErrorCode.INTERNAL: 1,
}


def exit_code_for(code: ErrorCode | None) -> int:
    """Map an error code to the spec section 10 exit code (0 when None)."""
    if code is None:
        return 0
    return _EXIT_CODES.get(code, 1)


def exit_code_for_error(err: DevkitError) -> int:
    return exit_code_for(err.code)


@dataclass
class Options:
    """Global CLI options captured by the app callback."""

    json: bool
    state_dir: Path | None
    verbose: bool


def stdout_is_tty() -> bool:
    """Isolated for tests: CliRunner's captured stdout is never a TTY."""
    return sys.stdout.isatty()


def die(err: DevkitError, json_mode: bool) -> NoReturn:
    """Render a failure Result and exit with the mapped code (spec section 10)."""
    if json_mode:
        from steamos_devkit.models import Result

        sys.stdout.write(Result.failure(err.code, err.message).model_dump_json() + "\n")
    else:
        print(str(err), file=sys.stderr)
    raise typer.Exit(exit_code_for_error(err))


def get_options(ctx: typer.Context) -> Options:
    opts = ctx.obj
    assert isinstance(opts, Options), "app callback did not store Options"
    return opts


def make_core(ctx: typer.Context) -> DevkitCore:
    """Build the typed core for a command, honoring --state-dir."""
    return DevkitCore(state_dir=get_options(ctx).state_dir)


def resolve_selector(ctx: typer.Context, selector: str | None) -> str:
    """Explicit selector wins; otherwise the last-device marker (spec section 5).

    Every device-taking command resolves through here so the CLI defaults
    consistently to the device last paired with or addressed.
    """
    if selector is not None:
        return selector
    last = make_core(ctx).state.last_device
    if last is None:
        raise DevkitError(
            ErrorCode.VALIDATION,
            "no device given and no last device; pass a device or run `devkit pair`",
        )
    return last


def shift_optional_selector(
    sel: str | None, value: str | None, *, missing: str
) -> tuple[str | None, str]:
    """Split ``<sel> <value>`` positionals when the selector is optional.

    Two positionals given: the first is the device, the second the value. One
    positional given: it is the value, and the device is left to
    :func:`resolve_selector`'s last-device fallback. Neither: VALIDATION with
    the caller's ``missing`` message (each command names its own argument).
    """
    if value is not None:
        return sel, value
    if sel is None:
        raise DevkitError(ErrorCode.VALIDATION, missing)
    return None, sel


def run_job(ctx: typer.Context, job: JobHandle, *, wait: bool) -> JobInfo | None:
    """``--no-wait`` prints the job id; otherwise block to a terminal state.

    Returns the terminal JobInfo when waiting (None in no-wait mode) so
    callers can print human-mode follow-ups (artifact paths, log excerpts).
    """
    if not wait:
        emit(
            ctx,
            json.dumps({"job_id": job.job_id, "status": "queued"}),
            f"job {job.job_id} submitted; `devkit jobs wait {job.job_id}` to follow it",
        )
        return None
    try:
        info = job.wait()
    except DevkitError as err:
        die(err, get_options(ctx).json)
    human = f"job {info.id} {info.state}"
    if info.message:
        human += f": {info.message}"
    emit(ctx, info.model_dump_json(), human)
    return info


def artifact_paths_human(core: DevkitCore, info: JobInfo) -> list[str]:
    """Human-mode lines locating each artifact a finished job produced."""
    lines: list[str] = []
    for artifact_id in info.artifacts:
        artifact = get_artifact(core.state, artifact_id)
        lines.append(f"artifact: {artifact.id} — {artifact.path}")
    return lines


def tail_text_bounded(path: Path, tail_lines: int) -> str:
    """Bounded tail of one text file, at read_artifact_excerpt's bounds.

    ``artifacts.read_artifact_excerpt`` is the canonical implementation, but
    it takes a registered file artifact; a "logs" artifact is a directory, so
    the primary log inside it is tailed by path here under the same bounds
    (``EXCERPT_MAX_LINES`` / ``EXCERPT_MAX_BYTES``, owned by artifacts.py).
    """
    tail_lines = min(tail_lines, EXCERPT_MAX_LINES)
    size = path.stat().st_size
    with open(path, "rb") as fh:
        if size > EXCERPT_MAX_BYTES:
            fh.seek(-EXCERPT_MAX_BYTES, 2)
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if len(lines) > tail_lines:
        lines = lines[-tail_lines:]
    return "\n".join(lines)


def emit_result(ctx: typer.Context, result: Result) -> None:
    """Emit a plain Result (JSON, or its message in human mode)."""
    emit(ctx, result.model_dump_json(), result.message or "")


def emit(ctx: typer.Context, payload_json: str, human: str) -> None:
    """JSON goes to stdout alone; human text goes to stdout only on a TTY."""
    if get_options(ctx).json:
        sys.stdout.write(payload_json + "\n")
    else:
        print(human)
