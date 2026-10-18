"""Profiling commands (spec section 10): captures plus MangoHud/RenderDoc/Proton toggles."""

from __future__ import annotations

import json as jsonlib
from typing import Literal, cast

import typer

from steamos_devkit import constants
from steamos_devkit import profiling as ops
from steamos_devkit.cli.common import die, emit, get_options, make_core
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.jobs import JobHandle
from steamos_devkit.models import Result

renderdoc_app = typer.Typer(
    name="renderdoc",
    help="RenderDoc capture layer and replay server.",
    no_args_is_help=True,
)


def _parse_on_off(ctx: typer.Context, value: str) -> bool:
    lowered = value.lower()
    if lowered == "on":
        return True
    if lowered == "off":
        return False
    die(
        DevkitError(ErrorCode.VALIDATION, f"expected 'on' or 'off', got {value!r}"),
        get_options(ctx).json,
    )


def _emit_result(ctx: typer.Context, result: Result) -> None:
    emit(ctx, result.model_dump_json(), result.message or "")


def _start_job(ctx: typer.Context, job: JobHandle, *, wait: bool) -> None:
    """--no-wait prints the job id; otherwise block to a terminal state."""
    if not wait:
        emit(ctx, jsonlib.dumps({"job_id": job.job_id, "status": "queued"}), job.job_id)
        return
    try:
        info = job.wait()
    except DevkitError as err:
        die(err, get_options(ctx).json)
    human = f"job {info.id} {info.state}"
    if info.message:
        human += f": {info.message}"
    for artifact_id in info.artifacts:
        human += f"\nartifact: {artifact_id}"
    emit(ctx, info.model_dump_json(), human)


def _gpu_trace(
    ctx: typer.Context,
    sel: str = typer.Argument(..., help="Device selector (registry name, IP, or hostname)."),
    timeout: int = typer.Option(
        constants.TIMEOUT_GPU_TRACE_S, "--timeout", help="Job timeout in seconds."
    ),
    no_wait: bool = typer.Option(False, "--no-wait", help="Print the job id and exit."),
) -> None:
    """Capture a GPU system trace on the device into a zip artifact."""
    core = make_core(ctx)
    try:
        job = ops.gpu_trace(core, sel, timeout_s=timeout)
    except DevkitError as err:
        die(err, get_options(ctx).json)
    _start_job(ctx, job, wait=not no_wait)


def _rgp(
    ctx: typer.Context,
    sel: str = typer.Argument(..., help="Device selector (registry name, IP, or hostname)."),
    timeout: int = typer.Option(
        constants.TIMEOUT_RGP_S, "--timeout", help="Job timeout in seconds."
    ),
    no_wait: bool = typer.Option(False, "--no-wait", help="Print the job id and exit."),
) -> None:
    """Trigger an RGP capture and download it once the write stabilizes."""
    core = make_core(ctx)
    try:
        job = ops.rgp_capture(core, sel, timeout_s=timeout)
    except DevkitError as err:
        die(err, get_options(ctx).json)
    _start_job(ctx, job, wait=not no_wait)


def _perf_overlay(
    ctx: typer.Context,
    sel: str = typer.Argument(..., help="Device selector."),
    state: str = typer.Argument(..., metavar="on|off", help="on shows the overlay."),
) -> None:
    """Toggle the MangoHud performance overlay."""
    enabled = _parse_on_off(ctx, state)
    try:
        result = ops.set_perf_overlay(make_core(ctx), sel, enabled)
    except DevkitError as err:
        die(err, get_options(ctx).json)
    _emit_result(ctx, result)


def _frametimes(
    ctx: typer.Context,
    sel: str = typer.Argument(..., help="Device selector."),
    state: str = typer.Argument(..., metavar="on|off", help="on starts MangoHud logging."),
) -> None:
    """Toggle MangoHud frametime logging (fetch CSVs via logs --kind frametimes)."""
    enabled = _parse_on_off(ctx, state)
    try:
        result = ops.set_frametime_logging(make_core(ctx), sel, enabled)
    except DevkitError as err:
        die(err, get_options(ctx).json)
    _emit_result(ctx, result)


def _renderdoc_layer(
    ctx: typer.Context,
    sel: str = typer.Argument(..., help="Device selector."),
    state: str = typer.Argument(..., metavar="on|off", help="on enables the capture layer."),
) -> None:
    """Toggle the RenderDoc Vulkan capture layer flag."""
    enabled = _parse_on_off(ctx, state)
    try:
        result = ops.set_renderdoc_layer(make_core(ctx), sel, enabled)
    except DevkitError as err:
        die(err, get_options(ctx).json)
    _emit_result(ctx, result)


def _renderdoc_server(
    ctx: typer.Context,
    sel: str = typer.Argument(..., help="Device selector."),
    action: str = typer.Argument(..., metavar="start|stop|status", help="Server action."),
) -> None:
    """Start, stop, or query the RenderDoc replay server."""
    lowered = action.lower()
    if lowered not in ("start", "stop", "status"):
        die(
            DevkitError(ErrorCode.VALIDATION, f"expected start|stop|status, got {action!r}"),
            get_options(ctx).json,
        )
    try:
        result = ops.renderdoc_server(
            make_core(ctx), sel, cast(Literal["start", "stop", "status"], lowered)
        )
    except DevkitError as err:
        die(err, get_options(ctx).json)
    _emit_result(ctx, result)


def _proton_log(
    ctx: typer.Context,
    sel: str = typer.Argument(..., help="Device selector."),
    state: str = typer.Argument(..., metavar="on|off", help="on writes PROTON_LOG=1."),
    winedebug: str | None = typer.Option(
        None, "--winedebug", help="Also write this WINEDEBUG value string."
    ),
) -> None:
    """Toggle Proton logging on the device."""
    enabled = _parse_on_off(ctx, state)
    try:
        result = ops.set_proton_logging(
            make_core(ctx), sel, "on" if enabled else "off", winedebug=winedebug
        )
    except DevkitError as err:
        die(err, get_options(ctx).json)
    _emit_result(ctx, result)


def register(app: typer.Typer) -> None:
    app.command(name="gpu-trace")(_gpu_trace)
    app.command(name="rgp")(_rgp)
    app.command(name="perf-overlay")(_perf_overlay)
    app.command(name="frametimes")(_frametimes)
    app.command(name="proton-log")(_proton_log)
    renderdoc_app.command(name="layer")(_renderdoc_layer)
    renderdoc_app.command(name="server")(_renderdoc_server)
    app.add_typer(renderdoc_app, name="renderdoc")
