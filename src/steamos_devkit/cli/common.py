"""Shared CLI machinery (spec section 10).

Command modules import from here — never from ``cli/__init__.py`` — to avoid
circular imports. Each command module exposes ``register(app: typer.Typer)``;
``__init__`` wires them in its registration section.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import typer

from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode

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


def emit(ctx: typer.Context, payload_json: str, human: str) -> None:
    """JSON goes to stdout alone; human text goes to stdout only on a TTY."""
    if get_options(ctx).json:
        sys.stdout.write(payload_json + "\n")
    else:
        print(human)
