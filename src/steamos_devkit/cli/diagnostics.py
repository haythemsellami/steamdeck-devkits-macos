"""Diagnostics commands (spec section 10): logs, screenshot, controller-config."""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from typing import cast

import typer

from steamos_devkit import constants
from steamos_devkit import diagnostics as ops
from steamos_devkit.artifacts import EXCERPT_MAX_BYTES, EXCERPT_MAX_LINES, get_artifact
from steamos_devkit.cli.common import die, emit, get_options, make_core, resolve_selector
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.jobs import JobHandle
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


def _gameid_or_shift(sel: str | None, gameid: str | None) -> tuple[str | None, str]:
    """``<sel> <gameid>`` with an optional selector: one positional = gameid."""
    if gameid is not None:
        return sel, gameid
    if sel is None:
        raise DevkitError(ErrorCode.VALIDATION, "missing gameid")
    return None, sel


def _start_job(ctx: typer.Context, job: JobHandle, *, wait: bool) -> JobInfo | None:
    """--no-wait prints the job id; otherwise block to a terminal state."""
    if not wait:
        emit(ctx, jsonlib.dumps({"job_id": job.job_id, "status": "queued"}), job.job_id)
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


def _artifact_paths_human(core: DevkitCore, info: JobInfo) -> list[str]:
    lines: list[str] = []
    for artifact_id in info.artifacts:
        artifact = get_artifact(core.state, artifact_id)
        lines.append(f"artifact: {artifact.id} — {artifact.path}")
    return lines


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


def _tail_text(path: Path, tail_lines: int) -> str:
    """Bounded tail of one log file (artifacts.read_artifact_excerpt bounds)."""
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
        print(_tail_text(primary, tail))
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
    info = _start_job(ctx, job, wait=not no_wait)
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
    info = _start_job(ctx, job, wait=not no_wait)
    if info is not None and not get_options(ctx).json:
        for line in _artifact_paths_human(core, info):
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
        device, resolved_gameid = _gameid_or_shift(sel, gameid)
        job = ops.controller_config(
            core, resolve_selector(ctx, device), resolved_gameid, timeout_s=timeout
        )
    except DevkitError as err:
        die(err, get_options(ctx).json)
    info = _start_job(ctx, job, wait=not no_wait)
    if info is not None and not get_options(ctx).json:
        for line in _artifact_paths_human(core, info):
            print(line)


def register(app: typer.Typer) -> None:
    app.command(name="logs")(_logs)
    app.command(name="screenshot")(_screenshot)
    app.command(name="controller-config")(_controller_config)
