"""Diagnostics commands (spec section 10): logs, screenshot, controller-config."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import typer

from steamos_devkit import constants
from steamos_devkit import diagnostics as ops
from steamos_devkit.artifacts import get_artifact
from steamos_devkit.cli.common import (
    artifact_paths_human,
    die,
    get_options,
    make_core,
    resolve_selector,
    run_job,
    shift_optional_selector,
    tail_text_bounded,
)
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import JobInfo, LogKind, ScreenshotMode

_SEL_HELP = "Device selector (registry name, IP, or hostname; default: last device)."
_GAMEID_ARG = (
    "Title gameid. With one positional given it is the gameid "
    "and the device defaults to the last device."
)

_LOG_KINDS = {"steam", "proton", "dumps", "frametimes"}

# Primary log inside a "logs" directory artifact: console-linux.txt on Deck,
# steam_output.log on Steam Frame.
_PRIMARY_LOG_NAMES = ("console-linux.txt", "steam_output.log")


def _find_primary_log(payload_dir: Path) -> Path | None:
    for name in _PRIMARY_LOG_NAMES:
        direct = payload_dir / name
        if direct.is_file():
            return direct
    for name in _PRIMARY_LOG_NAMES:
        matches = sorted(p for p in payload_dir.rglob(name) if p.is_file())
        if matches:
            return matches[0]
    return None


def _print_primary_log_excerpt(
    ctx: typer.Context, core: DevkitCore, info: JobInfo, tail: int
) -> None:
    """Human-mode bounded excerpt of the steam logs artifact's primary log."""
    if get_options(ctx).json:
        return
    for artifact_id in info.artifacts:
        artifact = get_artifact(core.state, artifact_id)
        if artifact.kind != "logs":
            continue
        primary = _find_primary_log(artifact.path)
        if primary is None:
            print("(no console-linux.txt / steam_output.log in the logs artifact)")
            return
        print(f"--- {primary.name} (last {tail} lines) ---")
        print(tail_text_bounded(primary, tail))
        return


def _logs(
    ctx: typer.Context,
    sel: str | None = typer.Argument(None, help=_SEL_HELP),
    kind: list[str] = typer.Option(
        [], "--kind", help="Log kind: steam, proton, dumps, frametimes. Repeatable."
    ),
    tail: int = typer.Option(200, "--tail", help="Lines of the primary log to print."),
    timeout: int = typer.Option(
        constants.TIMEOUT_FETCH_LOGS_S, "--timeout", help="Job timeout in seconds."
    ),
    no_wait: bool = typer.Option(False, "--no-wait", help="Print the job id and exit."),
) -> None:
    """Fetch device logs (Steam, Proton, dumps, frametimes) as artifacts."""
    core = make_core(ctx)
    try:
        for entry in kind:
            if entry not in _LOG_KINDS:
                valid = ", ".join(sorted(_LOG_KINDS))
                raise DevkitError(
                    ErrorCode.VALIDATION, f"unknown log kind {entry!r}; one of: {valid}"
                )
        job = ops.fetch_logs(
            core,
            resolve_selector(ctx, sel),
            kinds=cast(list[LogKind], kind) if kind else None,
            timeout_s=timeout,
        )
    except DevkitError as err:
        die(err, get_options(ctx).json)
    info = run_job(ctx, job, wait=not no_wait)
    if info is not None:
        _print_primary_log_excerpt(ctx, core, info, tail)


def _screenshot(
    ctx: typer.Context,
    sel: str | None = typer.Argument(None, help=_SEL_HELP),
    mode: int = typer.Option(
        3,
        "--mode",
        help="Gamescope mode: 1=baseplane, 2=all real layers, 3=full composition, 4=screen buffer.",
    ),
    timeout: int = typer.Option(
        constants.TIMEOUT_SCREENSHOT_S, "--timeout", help="Job timeout in seconds."
    ),
    no_wait: bool = typer.Option(False, "--no-wait", help="Print the job id and exit."),
) -> None:
    """Capture a screenshot on the device into a PNG artifact."""
    core = make_core(ctx)
    try:
        if mode not in constants.SCREENSHOT_MODES:
            raise DevkitError(
                ErrorCode.VALIDATION,
                f"unknown screenshot mode {mode!r}; one of: 1, 2, 3, 4",
            )
        job = ops.screenshot(
            core, resolve_selector(ctx, sel), mode=cast("ScreenshotMode", mode), timeout_s=timeout
        )
    except DevkitError as err:
        die(err, get_options(ctx).json)
    info = run_job(ctx, job, wait=not no_wait)
    if info is not None and not get_options(ctx).json:
        for line in artifact_paths_human(core, info):
            print(line)


def _controller_config(
    ctx: typer.Context,
    sel: str | None = typer.Argument(None, help=_SEL_HELP),
    gameid: str | None = typer.Argument(None, help=_GAMEID_ARG),
    timeout: int = typer.Option(
        constants.TIMEOUT_CONTROLLER_CONFIG_S, "--timeout", help="Job timeout in seconds."
    ),
    no_wait: bool = typer.Option(False, "--no-wait", help="Print the job id and exit."),
) -> None:
    """Dump a title's controller configuration (VDF) into an artifact."""
    core = make_core(ctx)
    try:
        device, resolved_gameid = shift_optional_selector(sel, gameid, missing="missing gameid")
        job = ops.controller_config(
            core, resolve_selector(ctx, device), resolved_gameid, timeout_s=timeout
        )
    except DevkitError as err:
        die(err, get_options(ctx).json)
    info = run_job(ctx, job, wait=not no_wait)
    if info is not None and not get_options(ctx).json:
        for line in artifact_paths_human(core, info):
            print(line)


def register(app: typer.Typer) -> None:
    app.command(name="logs")(_logs)
    app.command(name="screenshot")(_screenshot)
    app.command(name="controller-config")(_controller_config)
