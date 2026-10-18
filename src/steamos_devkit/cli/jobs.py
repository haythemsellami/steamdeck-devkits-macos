"""``devkit jobs`` and ``devkit artifacts`` commands (spec section 10).

These commands only read journals and artifact metadata from the state root —
they deliberately avoid ``core.job_runner`` so a read never triggers the
recovery sweep against another live process's jobs.
"""

from __future__ import annotations

import json
import time

import typer

from steamos_devkit.artifacts import get_artifact, list_artifacts
from steamos_devkit.cli.common import die, emit, get_options, make_core
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Artifact, JobInfo, JobState, Result

_JOB_WAIT_POLL_S = 0.25

# jobs wait exit codes (spec section 10): interrupted = the owning process
# died mid-job, reported like a failure.
_JOB_STATE_EXIT = {
    JobState.SUCCEEDED: 0,
    JobState.FAILED: 7,
    JobState.INTERRUPTED: 7,
    JobState.TIMEOUT: 8,
    JobState.CANCELLED: 9,
}

_ARTIFACT_KINDS = {
    "logs",
    "proton_log",
    "screenshot",
    "controller_config",
    "gpu_trace",
    "rgp",
    "frametimes",
    "dumps",
}


def _render_job_human(info: JobInfo) -> str:
    parts = [f"{info.id}  {info.kind}  {info.state}"]
    if info.device:
        parts.append(f"device={info.device}")
    if info.gameid:
        parts.append(f"gameid={info.gameid}")
    if info.message:
        parts.append(f"— {info.message}")
    return "  ".join(parts)


def _jobs_list(
    ctx: typer.Context,
    selector: str | None = typer.Argument(None),
    state: str | None = typer.Option(None, "--state", help="Filter by job state."),
) -> None:
    """List jobs, oldest first, optionally filtered by device and/or state."""
    core = make_core(ctx)
    try:
        state_filter = None
        if state is not None:
            try:
                state_filter = JobState(state)
            except ValueError:
                valid = ", ".join(str(s) for s in JobState)
                raise DevkitError(
                    ErrorCode.VALIDATION, f"unknown job state {state!r}; one of: {valid}"
                ) from None
        infos = core.job_store.list_jobs(device=selector, state=state_filter)
    except DevkitError as err:
        die(err, get_options(ctx).json)
    payload = json.dumps([i.model_dump(mode="json") for i in infos])
    human = "\n".join(_render_job_human(i) for i in infos) if infos else "no jobs"
    emit(ctx, payload, human)


def _jobs_status(ctx: typer.Context, job_id: str = typer.Argument(...)) -> None:
    """Show one job's current state and progress."""
    core = make_core(ctx)
    try:
        info = core.job_store.load(job_id)
    except DevkitError as err:
        die(err, get_options(ctx).json)
    emit(ctx, info.model_dump_json(), _render_job_human(info))


def _jobs_wait(ctx: typer.Context, job_id: str = typer.Argument(...)) -> None:
    """Block until a job reaches a terminal state; exit with its outcome code."""
    core = make_core(ctx)
    try:
        while True:
            info = core.job_store.load(job_id)
            if info.state.is_terminal:
                break
            time.sleep(_JOB_WAIT_POLL_S)
    except DevkitError as err:
        die(err, get_options(ctx).json)
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    emit(ctx, info.model_dump_json(), _render_job_human(info))
    raise typer.Exit(_JOB_STATE_EXIT.get(info.state, 1))


def _jobs_cancel(ctx: typer.Context, job_id: str = typer.Argument(...)) -> None:
    """Cancel a job owned by this process; foreign-process jobs are rejected."""
    core = make_core(ctx)
    try:
        info = core.job_store.load(job_id)
        if info.state.is_terminal:
            result = Result.success(message=f"job {job_id} already finished ({info.state})")
            emit(ctx, result.model_dump_json(), result.message or "")
            return
        raise DevkitError(
            ErrorCode.UNSUPPORTED,
            f"job {job_id} is owned by another process; cancel it from the owning process "
            "(Ctrl-C, or the MCP devkit_job_cancel tool of a live server)",
        )
    except DevkitError as err:
        die(err, get_options(ctx).json)


def _artifacts_list(
    ctx: typer.Context,
    selector: str | None = typer.Argument(None),
    kind: str | None = typer.Option(None, "--kind", help="Filter by artifact kind."),
) -> None:
    """List captured artifacts, oldest first, optionally filtered."""
    core = make_core(ctx)
    try:
        if kind is not None and kind not in _ARTIFACT_KINDS:
            valid = ", ".join(sorted(_ARTIFACT_KINDS))
            raise DevkitError(
                ErrorCode.VALIDATION, f"unknown artifact kind {kind!r}; one of: {valid}"
            )
        artifacts = list_artifacts(core.state, device=selector, kind=kind)
    except DevkitError as err:
        die(err, get_options(ctx).json)
    payload = json.dumps([a.model_dump(mode="json") for a in artifacts])
    if artifacts:
        human = "\n".join(f"{a.id}  {a.kind}  {a.device}  {a.bytes}B  {a.path}" for a in artifacts)
    else:
        human = "no artifacts"
    emit(ctx, payload, human)


def _artifacts_get(ctx: typer.Context, artifact_id: str = typer.Argument(...)) -> None:
    """Print the local path of one artifact's payload."""
    core = make_core(ctx)
    try:
        artifact: Artifact = get_artifact(core.state, artifact_id)
    except DevkitError as err:
        die(err, get_options(ctx).json)
    emit(ctx, artifact.model_dump_json(), str(artifact.path))


def register(app: typer.Typer) -> None:
    jobs_app = typer.Typer(name="jobs", help="Inspect and follow jobs.", no_args_is_help=True)
    jobs_app.command(name="list")(_jobs_list)
    jobs_app.command(name="status")(_jobs_status)
    jobs_app.command(name="wait")(_jobs_wait)
    jobs_app.command(name="cancel")(_jobs_cancel)
    app.add_typer(jobs_app, name="jobs")

    artifacts_app = typer.Typer(
        name="artifacts", help="List and locate captured artifacts.", no_args_is_help=True
    )
    artifacts_app.command(name="list")(_artifacts_list)
    artifacts_app.command(name="get")(_artifacts_get)
    app.add_typer(artifacts_app, name="artifacts")
