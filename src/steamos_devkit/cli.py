"""``devkit`` CLI (spec section 10).

M0 commands: ``doctor`` and ``version``. Later milestones add the rest of the
spec section 10 surface. Exit codes follow the spec section 10 table via
:func:`exit_code_for`.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import NoReturn

import typer

from steamos_devkit import constants
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import DoctorReport
from steamos_devkit.version import __version__

logger = logging.getLogger(__name__)

app = typer.Typer(
    name=constants.CLI_COMMAND,
    help=f"{constants.APP_DISPLAY_NAME} for macOS — CLI over the shared DevkitCore.",
    add_completion=False,
    no_args_is_help=True,
)

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
}


def exit_code_for(code: ErrorCode | None) -> int:
    """Map an error code to the spec section 10 exit code (0 when None)."""
    if code is None:
        return 0
    return _EXIT_CODES.get(code, 1)


def exit_code_for_error(err: DevkitError) -> int:
    return exit_code_for(err.code)


@dataclass
class _Options:
    json: bool
    state_dir: Path | None
    verbose: bool


@app.callback()
def _callback(
    ctx: typer.Context,
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON (automatic when stdout is not a TTY)."
    ),
    state_dir: Path | None = typer.Option(
        None,
        "--state-dir",
        help=f"State root override (also ${constants.STATE_DIR_ENV_VAR}).",
    ),
    verbose: bool = typer.Option(False, "--verbose", help="Debug logging on stderr."),
) -> None:
    json_mode = json_output or not sys.stdout.isatty()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        stream=sys.stderr,
        force=True,
    )
    ctx.obj = _Options(json=json_mode, state_dir=state_dir, verbose=verbose)


def _die(err: DevkitError, json_mode: bool) -> NoReturn:
    """Render a failure Result and exit with the mapped code (spec section 10)."""
    if json_mode:
        from steamos_devkit.models import Result

        sys.stdout.write(Result.failure(err.code, err.message).model_dump_json() + "\n")
    else:
        print(str(err), file=sys.stderr)
    raise typer.Exit(exit_code_for_error(err))


# doctor-check name -> exit code for a failing check
_DOCTOR_CHECK_EXIT = {
    "rsync": 5,
    "ssh": 5,
    "python": 5,
    "state-dir": 6,
}


def _doctor_exit_code(report: DoctorReport) -> int:
    if report.ok:
        return 0
    for check in report.checks:
        if check.status == "error":
            return _DOCTOR_CHECK_EXIT.get(check.name, 1)
    return 1


def _render_doctor(report: DoctorReport) -> str:
    lines = [
        f"{constants.APP_DISPLAY_NAME} doctor — {report.platform} {report.arch} "
        f"(context: {report.context})",
        f"state dir: {report.state_dir}",
    ]
    for check in report.checks:
        lines.append(f"[{check.status:>7}] {check.name}: {check.summary}")
    lines.append("result: " + ("ok" if report.ok else "problems found"))
    return "\n".join(lines)


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Probe host dependencies (rsync, ssh, python, state dir, terminal)."""
    opts: _Options = ctx.obj
    try:
        report = DevkitCore(state_dir=opts.state_dir).doctor()
    except DevkitError as err:
        _die(err, opts.json)
    if opts.json:
        sys.stdout.write(report.model_dump_json() + "\n")
    else:
        print(_render_doctor(report))
    raise typer.Exit(_doctor_exit_code(report))


def _upstream_commit() -> str | None:
    try:
        return (
            resources.files("steamos_devkit")
            .joinpath("_vendor")
            .joinpath("UPSTREAM_COMMIT")
            .read_text(encoding="utf-8")
            .strip()
        )
    except (FileNotFoundError, OSError):
        return None


@app.command()
def version(ctx: typer.Context) -> None:
    """Print client and vendored-upstream versions."""
    opts: _Options = ctx.obj
    upstream = _upstream_commit()
    if opts.json:
        import json as jsonlib

        sys.stdout.write(
            jsonlib.dumps({"version": __version__, "upstream_commit": upstream}) + "\n"
        )
    else:
        suffix = f" (upstream steamos-devkit {upstream})" if upstream else ""
        print(f"{constants.CLI_COMMAND} {__version__}{suffix}")


def main() -> None:
    try:
        app()
    except DevkitError as err:
        json_mode = "--json" in sys.argv[1:] or not sys.stdout.isatty()
        if json_mode:
            from steamos_devkit.models import Result

            sys.stdout.write(Result.failure(err.code, err.message).model_dump_json() + "\n")
        else:
            print(str(err), file=sys.stderr)
        sys.exit(exit_code_for_error(err))


if __name__ == "__main__":
    main()
