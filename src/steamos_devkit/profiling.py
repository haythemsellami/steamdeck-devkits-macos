"""Profiling operations (spec section 8; audit section 4).

gpu-trace and RGP captures run as jobs (they can take minutes) and land as
artifacts; the MangoHud, RenderDoc, and Proton toggles are single remote
commands returning a :class:`Result`. Every remote invocation matches the
upstream protocol verbatim — gpu-trace/renderdoccmd/mangohudctl ship with
SteamOS, so no devkit-utils upload happens here (unlike the session helpers).
"""

from __future__ import annotations

import logging
import posixpath
import shlex
import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from steamos_devkit import constants
from steamos_devkit.artifacts import register_artifact
from steamos_devkit.device import DeviceHandle, RemoteResult
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.jobs import JobContext, JobHandle
from steamos_devkit.models import Artifact, JobKind, Result

if TYPE_CHECKING:
    from steamos_devkit.core import DevkitCore

logger = logging.getLogger(__name__)

# upstream _obtain_trace treats this stderr marker as a capture failure even
# when the exit code is 0 (:1659).
_GPU_TRACE_CAPTURE_FAILED_MARKER = "Failed to capture trace"
_GPU_TRACE_START_DELAY_S = 5

_RGP_TRIGGER = "/tmp/rgp.trigger"
_RGP_APPEAR_TIMEOUT_S = 10.0
_RGP_STABLE_TIMEOUT_S = 60.0
_RGP_STABLE_SAMPLES = 3

_STEAM_ENV_DIR = "$XDG_RUNTIME_DIR/steam/env"
_RENDERDOC_LAYER_FLAG = "ENABLE_VULKAN_RENDERDOC_CAPTURE"
_RENDERDOC_UNIT = "devkit-renderdoc-replay"
_PROTON_LOG_FLAG = "PROTON_LOG"
_WINEDEBUG_FLAG = "WINEDEBUG"

# set[str] operands: runtime validation for untyped (MCP) callers without
# literal narrowing on the Literal-typed parameters
_RENDERDOC_ACTIONS: frozenset[str] = frozenset({"start", "stop", "status"})
_PROTON_MODES: frozenset[str] = frozenset({"off", "on"})


def _trace_capture_failed(result: RemoteResult) -> bool:
    return result.exit_code != 0 or _GPU_TRACE_CAPTURE_FAILED_MARKER in result.stderr


def _fetch_artifact(
    core: DevkitCore,
    ctx: JobContext,
    handle: DeviceHandle,
    remote_path: str,
    *,
    kind: str,
    media_type: str | None = None,
) -> Artifact:
    """SFTP one remote file into a temp dir and move it into the store."""
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"devkit-{kind}-"))
    local = tmp_dir / posixpath.basename(remote_path)
    try:
        handle.sftp_get(remote_path, local)
        return register_artifact(
            core.state,
            job_id=ctx.job_id,
            device=handle.device.name,
            kind=kind,
            source=local,
            remote_source=remote_path,
            media_type=media_type,
        )
    finally:
        # register_artifact() moved the payload out; this removes the empty
        # temp dir, or the partial download when the get/register failed.
        shutil.rmtree(tmp_dir, ignore_errors=True)


def gpu_trace(
    core: DevkitCore, sel: str, timeout_s: int = constants.TIMEOUT_GPU_TRACE_S
) -> JobHandle:
    """Capture a GPU system trace into a zip artifact (audit :1644-1679)."""
    handle = core.device_handle(sel)
    remote_file: list[str] = []  # known only after mktemp; shared with the cleanup hook

    def _cleanup(ctx: JobContext) -> None:
        # spec section 7.4: rm -f the remote temp zip (best-effort)
        if not remote_file:
            return
        try:
            handle.run(f"rm -f {remote_file[0]}", check=False)
        except Exception:
            logger.warning("gpu_trace cleanup failed for job %s", ctx.job_id, exc_info=True)

    def _run(ctx: JobContext) -> str:
        ctx.progress("preparing")
        # upstream :1647: obtain a unique temp file, delete it, and let the
        # gpu-trace service write it out again.
        created = handle.run("mktemp -p /tmp XXXXXXXXX-trace.zip")
        remote = created.stdout.strip()
        if not remote:
            raise DevkitError(
                ErrorCode.REMOTE_COMMAND_FAILED, "mktemp returned no path for the trace"
            )
        remote_file.append(remote)
        handle.run(f"rm {remote}")

        ctx.progress("capturing")
        capture_cmd = f"gpu-trace --capture --no-gpuvis -o {remote}"
        result = handle.run(capture_cmd, check=False)
        if _trace_capture_failed(result):
            # Tracing may not be armed (upstream :1660-1668): start the service,
            # let buffers fill, then retry the capture once.
            handle.run("gpu-trace --start", check=False)
            ctx.sleep(_GPU_TRACE_START_DELAY_S)
            result = handle.run(capture_cmd, check=False)
        if _trace_capture_failed(result):
            raise DevkitError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                f"gpu-trace capture failed on {handle.device.name}",
                details={"command": capture_cmd, "stderr_tail": result.stderr[-500:]},
                remote_exit_code=result.exit_code,
            )

        ctx.progress("downloading")
        artifact = _fetch_artifact(
            core, ctx, handle, remote, kind="gpu_trace", media_type="application/zip"
        )
        core.job_store.attach_artifact(ctx.job_id, artifact.id)
        # keep /tmp clean on success (upstream :1676-1678); cancel/timeout is
        # covered by the cleanup hook
        handle.run(f"rm -f {remote}", check=False)
        return f"gpu trace captured: {artifact.id} ({artifact.bytes} bytes)"

    return core.job_runner.submit(
        _run,
        kind=JobKind.GPU_TRACE,
        device=handle.device.name,
        timeout_s=timeout_s,
        cleanup=_cleanup,
    )


def _await_rgp_file(handle: DeviceHandle, ctx: JobContext) -> str:
    """Poll for the capture file the RADV trigger produces (audit :1708-1721)."""
    deadline = time.monotonic() + _RGP_APPEAR_TIMEOUT_S
    while True:
        ctx.check_cancelled()
        result = handle.run("ls -1t /tmp/*.rgp", check=False)
        if result.exit_code == 0:
            first = result.stdout.split("\n")[0].strip()
            if first:
                return first
        if time.monotonic() >= deadline:
            raise DevkitError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                "timeout waiting for capture file; Vulkan titles only — check "
                '"Enable Graphics Profiling" in Developer settings on the device',
            )
        ctx.sleep(0.5)


def _await_rgp_stable(handle: DeviceHandle, ctx: JobContext, remote_path: str) -> int:
    """Wait until the capture size stops changing; return the final size."""
    deadline = time.monotonic() + _RGP_STABLE_TIMEOUT_S
    size = 0
    stable = 0
    while True:
        ctx.check_cancelled()
        if time.monotonic() >= deadline:
            raise DevkitError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                f"timeout waiting for capture file to finish writing: {remote_path}",
            )
        result = handle.run(f"stat -c %s {remote_path}")
        try:
            updated = int(result.stdout.strip())
        except ValueError as e:
            raise DevkitError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                f"unexpected stat output for {remote_path}: {result.stdout.strip()!r}",
            ) from e
        if updated != 0 and updated == size:
            stable += 1
            if stable >= _RGP_STABLE_SAMPLES:
                return size
        else:
            stable = 0
        size = updated
        ctx.sleep(1)


def rgp_capture(core: DevkitCore, sel: str, timeout_s: int = constants.TIMEOUT_RGP_S) -> JobHandle:
    """Trigger an RGP capture and download it once stable (audit :1696-1767)."""
    handle = core.device_handle(sel)

    def _cleanup(ctx: JobContext) -> None:
        # spec section 7.4: rm -f the trigger and any partial captures
        try:
            handle.run(f"rm -f {_RGP_TRIGGER} /tmp/*.rgp", check=False)
        except Exception:
            logger.warning("rgp cleanup failed for job %s", ctx.job_id, exc_info=True)

    def _run(ctx: JobContext) -> str:
        ctx.progress("triggering")
        # Steam sets RADV_THREAD_TRACE_TRIGGER=/tmp/rgp.trigger; RADV writes
        # the capture to /tmp regardless of the trigger's location (:1703-1705).
        handle.run("rm -f /tmp/*.rgp")
        handle.run(f"touch {_RGP_TRIGGER}")

        ctx.progress("waiting")
        remote_path = _await_rgp_file(handle, ctx)
        ctx.progress("stabilizing")
        remote_size = _await_rgp_stable(handle, ctx, remote_path)

        ctx.progress("downloading")
        tmp_dir = Path(tempfile.mkdtemp(prefix="devkit-rgp-"))
        local = tmp_dir / posixpath.basename(remote_path)
        try:
            handle.sftp_get(remote_path, local)
            local_size = local.stat().st_size
            if local_size != remote_size:
                raise DevkitError(
                    ErrorCode.REMOTE_COMMAND_FAILED,
                    f"downloaded capture truncated: {local_size} bytes locally vs "
                    f"{remote_size} on device",
                    details={
                        "remote_path": remote_path,
                        "remote_size": remote_size,
                        "local_size": local_size,
                    },
                )
            artifact = register_artifact(
                core.state,
                job_id=ctx.job_id,
                device=handle.device.name,
                kind="rgp",
                source=local,
                remote_source=remote_path,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        core.job_store.attach_artifact(ctx.job_id, artifact.id)
        return f"rgp capture downloaded: {artifact.id} ({artifact.bytes} bytes)"

    return core.job_runner.submit(
        _run,
        kind=JobKind.RGP,
        device=handle.device.name,
        timeout_s=timeout_s,
        cleanup=_cleanup,
    )


def set_perf_overlay(core: DevkitCore, sel: str, enabled: bool) -> Result:
    """Toggle the MangoHud performance overlay (inverted no_display semantics)."""
    handle = core.device_handle(sel)
    handle.run(f"mangohudctl set no_display {'false' if enabled else 'true'}")
    return Result.success(
        device=handle.device,
        message=f"performance overlay {'enabled' if enabled else 'disabled'}",
    )


def set_frametime_logging(core: DevkitCore, sel: str, enabled: bool) -> Result:
    """Toggle MangoHud frametime capture; CSVs are fetched via fetch_logs."""
    handle = core.device_handle(sel)
    handle.run(f"mangohudctl set log_session {'true' if enabled else 'false'}")
    message = f"frametime logging {'enabled' if enabled else 'disabled'}"
    if not enabled:
        message += "; fetch mangoapp_*.csv via `devkit logs --kind frametimes`"
    return Result.success(device=handle.device, message=message)


def set_renderdoc_layer(core: DevkitCore, sel: str, enabled: bool) -> Result:
    """Write/remove the RenderDoc capture layer flag (audit :1770-1784).

    ``$XDG_RUNTIME_DIR`` must expand on the device — the command strings below
    deliberately carry it literally (no local interpolation).
    """
    handle = core.device_handle(sel)
    if enabled:
        handle.run(
            f"mkdir -p {_STEAM_ENV_DIR} && echo 1 > {_STEAM_ENV_DIR}/{_RENDERDOC_LAYER_FLAG}"
        )
    else:
        handle.run(f"rm -f {_STEAM_ENV_DIR}/{_RENDERDOC_LAYER_FLAG}")
    return Result.success(
        device=handle.device,
        message=f"renderdoc capture layer {'enabled' if enabled else 'disabled'}",
    )


def renderdoc_server(
    core: DevkitCore, sel: str, action: Literal["start", "stop", "status"]
) -> Result:
    """Start/stop/status the RenderDoc replay server (audit :2057-2081)."""
    if action not in _RENDERDOC_ACTIONS:
        raise DevkitError(ErrorCode.VALIDATION, f"unknown renderdoc server action: {action!r}")
    handle = core.device_handle(sel)
    if action == "stop":
        # both legs may legitimately fail (nothing running); tolerate nonzero
        handle.run(
            f"systemctl --user stop {_RENDERDOC_UNIT} 2>/dev/null; killall -9 renderdoccmd",
            check=False,
        )
        return Result.success(device=handle.device, message="renderdoc replay server stopped")
    if action == "start":
        # renderdoccmd adds a RenderDoc/ subfolder under RENDERDOC_TEMP
        handle.run(
            f"systemd-run --user --unit={_RENDERDOC_UNIT} --collect "
            f"env RENDERDOC_TEMP=/home/{handle.device.login} renderdoccmd remoteserver"
        )
        check = handle.run("sleep 2; pgrep -x renderdoccmd", check=False)
        if check.exit_code != 0 or not check.stdout.strip():
            raise DevkitError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                "renderdoc replay server did not stay running after launch",
                details={"stderr_tail": check.stderr[-500:]},
                remote_exit_code=check.exit_code,
            )
        return Result.success(device=handle.device, message="renderdoc replay server running")
    check = handle.run("pgrep -x renderdoccmd", check=False)
    running = check.exit_code == 0 and bool(check.stdout.strip())
    return Result.success(
        device=handle.device,
        message=(
            "renderdoc replay server is running"
            if running
            else "renderdoc replay server is not running"
        ),
    )


def set_proton_logging(
    core: DevkitCore,
    sel: str,
    mode: Literal["off", "on"],
    winedebug: str | None = None,
) -> Result:
    """Toggle Proton logging via steam/env flag files (audit gui2.py:3266-3289).

    Enabling writes ``PROTON_LOG=1``; a ``winedebug`` string is written to the
    ``WINEDEBUG`` flag file. Enabling without ``winedebug`` removes any legacy
    ``WINEDEBUG`` file first, exactly as upstream does.
    """
    if mode not in _PROTON_MODES:
        raise DevkitError(ErrorCode.VALIDATION, f"unknown proton logging mode: {mode!r}")
    handle = core.device_handle(sel)
    handle.run(f"mkdir -p {_STEAM_ENV_DIR}")
    if mode == "off":
        handle.run(f"rm -f {_STEAM_ENV_DIR}/{_PROTON_LOG_FLAG}")
        handle.run(f"rm -f {_STEAM_ENV_DIR}/{_WINEDEBUG_FLAG}")
        return Result.success(device=handle.device, message="proton logging disabled")
    if winedebug is None:
        handle.run(f"rm -f {_STEAM_ENV_DIR}/{_WINEDEBUG_FLAG}")
    handle.run(f"echo 1 > {_STEAM_ENV_DIR}/{_PROTON_LOG_FLAG}")
    if winedebug is not None:
        handle.run(f"echo {shlex.quote(winedebug)} > {_STEAM_ENV_DIR}/{_WINEDEBUG_FLAG}")
    return Result.success(device=handle.device, message="proton logging enabled")
