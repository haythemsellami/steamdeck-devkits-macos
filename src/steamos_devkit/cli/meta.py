"""Top-level ``doctor`` and ``version`` commands (spec section 10)."""

from __future__ import annotations

import sys

import typer

from steamos_devkit import constants
from steamos_devkit._vendor import upstream_commit
from steamos_devkit.cli.common import die, emit, get_options, make_core
from steamos_devkit.errors import DevkitError
from steamos_devkit.models import DoctorReport
from steamos_devkit.version import __version__

# doctor-check name -> exit code for a failing check
_DOCTOR_CHECK_EXIT = {
    "rsync": 5,
    "ssh": 5,
    "python": 5,
    "state-dir": 6,
}


def doctor_exit_code(report: DoctorReport) -> int:
    if report.ok:
        return 0
    for check in report.checks:
        if check.status == "error":
            return _DOCTOR_CHECK_EXIT.get(check.name, 1)
    return 1


def render_doctor(report: DoctorReport) -> str:
    lines = [
        f"{constants.APP_DISPLAY_NAME} doctor — {report.platform} {report.arch} "
        f"(context: {report.context})",
        f"state dir: {report.state_dir}",
    ]
    for check in report.checks:
        lines.append(f"[{check.status:>7}] {check.name}: {check.summary}")
    lines.append("result: " + ("ok" if report.ok else "problems found"))
    return "\n".join(lines)


def _doctor(ctx: typer.Context) -> None:
    """Probe host dependencies (rsync, ssh, python, state dir, terminal)."""
    try:
        report = make_core(ctx).doctor()
    except DevkitError as err:
        die(err, get_options(ctx).json)
    emit(ctx, report.model_dump_json(), render_doctor(report))
    raise typer.Exit(doctor_exit_code(report))


def _version(ctx: typer.Context) -> None:
    """Print client and vendored-upstream versions."""
    upstream = upstream_commit()
    if get_options(ctx).json:
        import json as jsonlib

        sys.stdout.write(
            jsonlib.dumps({"version": __version__, "upstream_commit": upstream}) + "\n"
        )
    else:
        suffix = f" (upstream steamos-devkit {upstream})" if upstream else ""
        print(f"{constants.CLI_COMMAND} {__version__}{suffix}")


def register(app: typer.Typer) -> None:
    app.command(name="doctor")(_doctor)
    app.command(name="version")(_version)
