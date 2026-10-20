"""`devkit setup` — one-shot host onboarding (spec section 10)."""

from __future__ import annotations

import typer

from steamos_devkit.cli.common import die, emit, get_options, make_core
from steamos_devkit.errors import DevkitError
from steamos_devkit.models import SetupReport
from steamos_devkit.setup import run_setup


def _render(report: SetupReport) -> str:
    lines = ["devkit setup:"]
    for step in report.steps:
        lines.append(f"[{step.status:>7}] {step.name}: {step.summary}")
    lines.append("result: " + ("ready" if report.ok else "problems found — see error steps above"))
    return "\n".join(lines)


def _setup(
    ctx: typer.Context,
    install: bool = typer.Option(
        True, "--install/--no-install", help="Install missing dependencies via Homebrew."
    ),
    probe: bool = typer.Option(
        True,
        "--probe/--no-probe",
        help="Run a short mDNS probe to surface the macOS Local Network prompt.",
    ),
) -> None:
    """Set up everything the devkit client needs on this Mac, end to end."""
    core = make_core(ctx)
    try:
        report = run_setup(core, install_deps=install, network_probe=probe)
    except DevkitError as err:
        die(err, get_options(ctx).json)
    emit(ctx, report.model_dump_json(), _render(report))
    raise typer.Exit(0 if report.ok else 5)


def register(app: typer.Typer) -> None:
    app.command(name="setup")(_setup)
