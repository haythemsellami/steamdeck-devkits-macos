"""``devkit deploy`` (spec section 10): preview or run the deploy pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, cast

import typer
from pydantic import ValidationError

from steamos_devkit import constants
from steamos_devkit.cli.common import die, emit, get_options, make_core
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import DeploySpec, FilterSpec
from steamos_devkit.titles import deploy as deploy_job
from steamos_devkit.transfer import preview_deploy

_FILTER_MODES = ("exclude", "include", "raw")


def _deploy(
    ctx: typer.Context,
    sel: str = typer.Argument(..., help="Device selector (registry name, IP, or hostname)."),
    gameid: str = typer.Argument(...),
    source_dir: Path = typer.Argument(..., help="Local build tree to upload."),
    preview: bool = typer.Option(
        False, "--preview", help="Dry-run only: show what would change, touch nothing."
    ),
    clean: bool = typer.Option(
        False,
        "--clean",
        help="Delete remote files absent locally (--delete --delete-excluded --delete-delay).",
    ),
    checksum: bool = typer.Option(
        False, "--checksum", help="Content-compare files (rsync --checksum)."
    ),
    update: bool = typer.Option(
        False,
        "--update",
        help="Skip remote files that are newer (rsync --update; exclusive with --checksum).",
    ),
    no_compress: bool = typer.Option(False, "--no-compress", help="Disable rsync -z compression."),
    filter_mode: str = typer.Option(
        "exclude", "--filter-mode", help="Filter interpretation: exclude | include | raw."
    ),
    filter_patterns: list[str] = typer.Option(
        [], "--filter", help="Filter pattern (per --filter-mode). Repeatable."
    ),
    no_register: bool = typer.Option(
        False, "--no-register", help="Skip steam-client-create-shortcut after the upload."
    ),
    launch: bool = typer.Option(False, "--launch", help="Run the title after a successful deploy."),
    no_wait: bool = typer.Option(
        False, "--no-wait", help="Print the job id and exit; follow with `devkit jobs wait <id>`."
    ),
    timeout: int = typer.Option(
        constants.TIMEOUT_DEPLOY_S, "--timeout", help="Job timeout in seconds."
    ),
) -> None:
    """Upload a build tree to a device, then optionally register and launch it."""
    core = make_core(ctx)
    try:
        if filter_mode not in _FILTER_MODES:
            raise DevkitError(
                ErrorCode.VALIDATION,
                f"--filter-mode must be one of {', '.join(_FILTER_MODES)}, got {filter_mode!r}",
            )
        try:
            spec = DeploySpec(
                device=sel,
                gameid=gameid,
                source_dir=source_dir,
                filters=FilterSpec(
                    mode=cast("Literal['exclude', 'include', 'raw']", filter_mode),
                    patterns=list(filter_patterns),
                ),
                clean=clean,
                checksum=checksum,
                update=update,
                compress=not no_compress,
                register=not no_register,
                launch=launch,
            )
        except ValidationError as err:
            raise DevkitError(ErrorCode.VALIDATION, str(err)) from err
        if preview:
            result = preview_deploy(core, sel, spec)
            human = (
                f"preview {spec.gameid}: +{result.files_added} added, "
                f"~{result.files_changed} changed, -{result.files_deleted} deleted, "
                f"{result.bytes_to_transfer} bytes to transfer"
            )
            if result.warnings:
                human += "\n" + "\n".join(f"warning: {w}" for w in result.warnings)
            emit(ctx, result.model_dump_json(), human)
            return
        handle = deploy_job(core, sel, spec, timeout_s=timeout)
        if no_wait:
            emit(
                ctx,
                json.dumps({"job_id": handle.job_id, "state": "queued"}),
                f"job {handle.job_id} submitted; `devkit jobs wait {handle.job_id}` to follow it",
            )
            return
        info = handle.wait()
    except DevkitError as err:
        die(err, get_options(ctx).json)
    emit(ctx, info.model_dump_json(), info.message or "deploy complete")


def register(app: typer.Typer) -> None:
    app.command(name="deploy")(_deploy)
