"""MCP server surface (spec section 11) — the official SDK's MCPServer over stdio.

One long-lived :class:`DevkitCore` per server process, constructed lazily on
the first tool call. This process is the real no-wait/cancel context (spec
appendix A, wave-2): job-starting tools return ``{"job_id", "status":
"queued"}`` immediately and register the :class:`JobHandle` in a process-local
table so ``devkit_job_cancel``/``devkit_job_status`` reach the live job. When
the request carries a ``progressToken``, a small asyncio poller bridges journal
progress to ``notifications/progress`` until the job reaches a terminal state.

Every tool returns the typed model as structured JSON; a :class:`DevkitError`
crossing the core boundary becomes an ``isError`` result whose text is a JSON
payload carrying the error ``code`` and ``message``. Per spec section 6 and 11,
no private key material, passphrase, or the pairing magic phrase can appear in
results: the core never puts them there (asserted by review, not redaction).
Tools never refuse on capability grounds — ``devkit_capabilities`` reports,
execution errors surface naturally (spec section 9).

Not exposed, per spec section 11 (upstream has no such operations):
stop-title, launch-readiness polling, title stdout streaming, arbitrary remote
shell.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeVar

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import (
    ResourceError,
    ResourceNotFoundError,
    ToolError,
)
from mcp.server.mcpserver.resources import FunctionResource, Resource, ResourceTemplate
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

from steamos_devkit import (
    artifacts,
    capabilities,
    constants,
    discovery,
    pairing,
    profiling,
    status,
    titles,
    transfer,
)
from steamos_devkit import session as session_control
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import (
    Artifact,
    Capabilities,
    DeployPreview,
    DeploySpec,
    Device,
    DeviceStatus,
    DiscoveredDevice,
    DoctorReport,
    FilterSpec,
    GameId,
    JobInfo,
    JobState,
    LaunchResult,
    LogKind,
    Result,
    ScreenshotMode,
    Title,
    TitleConfig,
    TitleDetail,
)
from steamos_devkit.version import __version__

if TYPE_CHECKING:
    from steamos_devkit.jobs import JobHandle

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

SERVER_NAME: str = constants.APP_NAME
"""MCP server identity (spec section 1)."""

PROGRESS_POLL_S: float = 0.5
"""Journal poll interval for the progress-notification bridge (spec section 11)."""

_TEXTISH_ARTIFACT_KINDS = frozenset({"logs", "proton_log", "frametimes", "controller_config"})
"""Kinds served as bounded text from the artifact resource; everything else is bytes."""


class JobStarted(BaseModel):
    """Immediate answer of every job-starting tool (spec section 11)."""

    job_id: str
    status: Literal["queued"] = "queued"


class ArtifactEntry(BaseModel):
    """An artifact record plus its MCP resource link (spec section 11)."""

    artifact: Artifact
    resource_uri: str
    """``devkit://artifact/<id>`` — bytes for binary kinds, bounded text for text kinds."""


class ArtifactRead(BaseModel):
    """Answer of ``devkit_read_artifact``: bounded excerpt and/or resource link."""

    artifact_id: str
    kind: str
    media_type: str | None
    resource_uri: str
    excerpt: str | None
    """Bounded tail (<= 2000 lines / 64 KiB) for text file artifacts; None otherwise."""


class _ServerState:
    """Per-server process state: the lazy core, live job handles, poller tasks."""

    def __init__(self, state_dir: Path | None) -> None:
        self._state_dir = state_dir
        self._core: DevkitCore | None = None
        self.jobs: dict[str, JobHandle] = {}
        self.progress_tasks: set[asyncio.Task[None]] = set()

    def core(self) -> DevkitCore:
        """The one long-lived core, built on first use (spec section 11)."""
        if self._core is None:
            self._core = DevkitCore(state_dir=self._state_dir)
        return self._core


# --- error mapping -------------------------------------------------------------


def _error_payload(err: DevkitError) -> str:
    """Structured isError payload: the DevkitError code/message as JSON text."""
    payload: dict[str, Any] = {"code": str(err.code), "message": err.message}
    if err.details:
        payload["details"] = err.details
    if err.remote_exit_code is not None:
        payload["remote_exit_code"] = err.remote_exit_code
    return json.dumps(payload, default=str)


def _validation_payload(err: ValidationError) -> str:
    """Pydantic rejections inside a tool body map to the VALIDATION code."""
    payload = {
        "code": str(ErrorCode.VALIDATION),
        "message": f"invalid arguments: {err.error_count()} validation error(s)",
        "details": {
            "fields": [
                ".".join(str(part) for part in issue.get("loc", ())) for issue in err.errors()
            ]
        },
    }
    return json.dumps(payload)


async def _run(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Run a blocking core call on a worker thread; map failures to ToolError.

    ToolError lands as an ``isError`` tool result whose text is the JSON
    payload from :func:`_error_payload`; anything else stays a crash and never
    leaks internals (the SDK genericizes unexpected exceptions itself).
    """
    try:
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    except DevkitError as err:
        raise ToolError(_error_payload(err)) from err
    except ValidationError as err:
        raise ToolError(_validation_payload(err)) from err


def _resource_error(err: DevkitError) -> ResourceError:
    """Map a DevkitError to a resource error; unknown entities are NotFound."""
    text = f"[{err.code}] {err.message}"
    if err.code in (
        ErrorCode.JOB_NOT_FOUND,
        ErrorCode.ARTIFACT_NOT_FOUND,
        ErrorCode.DEVICE_NOT_FOUND,
    ):
        return ResourceNotFoundError(text)
    return ResourceError(text)


# --- progress bridge -----------------------------------------------------------


def _progress_token(ctx: Context[Any, Any] | None) -> str | int | None:
    """The request's ``_meta.progressToken`` (parsed form: ``progress_token``)."""
    if ctx is None:
        return None
    try:
        meta = ctx.request_context.meta
    except ValueError:
        return None
    if meta is None:
        return None
    token = meta.get("progress_token")
    return token if isinstance(token, str | int) and not isinstance(token, bool) else None


async def _poll_job_progress(
    server_state: _ServerState, ctx: Context[Any, Any], token: str | int, job_id: str
) -> None:
    """Bridge journal progress to ``notifications/progress`` until the job ends.

    Sends on the connection-level channel (``send_progress_notification``), not
    the request-scoped one: the job outlives the tool call that started it.
    """
    session = ctx.session
    last: tuple[float, float | None, str] | None = None
    ticks = 0
    try:
        while True:
            try:
                info = await anyio.to_thread.run_sync(server_state.core().job_store.load, job_id)
            except DevkitError:
                return  # journal gone — nothing more to report
            progress = info.progress
            message = progress.phase if progress is not None else str(info.state)
            total: float | None = None
            if progress is not None and progress.bytes_done is not None:
                value = float(progress.bytes_done)
                if progress.bytes_total is not None:
                    total = float(progress.bytes_total)
            elif progress is not None and progress.items_done is not None:
                value = float(progress.items_done)
            else:
                ticks += 1
                value = float(ticks)
            snapshot = (value, total, message)
            if snapshot != last:
                last = snapshot
                await session.send_progress_notification(token, value, total, message)
            if info.state.is_terminal:
                return
            await asyncio.sleep(PROGRESS_POLL_S)
    except asyncio.CancelledError:
        raise
    except Exception:  # a disconnected client must not kill the server loop
        logger.debug("progress bridge for %s stopped", job_id, exc_info=True)


def _start_job(
    server_state: _ServerState, handle: JobHandle, ctx: Context[Any, Any] | None
) -> JobStarted:
    """Register the handle and, when requested, the progress bridge."""
    server_state.jobs[handle.job_id] = handle
    token = _progress_token(ctx)
    if token is not None and ctx is not None:
        task = asyncio.create_task(_poll_job_progress(server_state, ctx, token, handle.job_id))
        server_state.progress_tasks.add(task)
        task.add_done_callback(server_state.progress_tasks.discard)
    return JobStarted(job_id=handle.job_id)


def _diagnostics() -> Any:
    """Lazy import of the M3 diagnostics module (built in parallel — spec
    section 8's core surface defines the signatures this calls)."""
    return importlib.import_module("steamos_devkit.diagnostics")


# --- server factory --------------------------------------------------------------


def create_server(state_dir: Path | None = None) -> MCPServer[None]:
    """Build the ``steamos-devkit`` MCP server (spec section 11).

    ``state_dir`` overrides the state root (tests and ``--state-dir``); the
    core itself is constructed lazily on the first tool call.
    """
    server_state = _ServerState(state_dir)
    server: MCPServer[None] = MCPServer(name=SERVER_NAME, version=__version__)

    read_only = ToolAnnotations(read_only_hint=True)
    mutating = ToolAnnotations(read_only_hint=False)

    # Annotation honesty (M4): read_only is only for tools that never mutate
    # the device or local state. Anything reaching ``ensure_utils`` (status,
    # list/inspect titles, capabilities with a device) uploads devkit-utils at
    # least once per process; ``devkit_doctor``/``devkit_capabilities`` create
    # the state-dir layout plus a probe file. Those are annotated mutating.
    # ``devkit_preview_deploy`` stays read-only: it is an rsync ``-n`` dry run
    # and never calls ensure_utils (see transfer.preview_deploy).

    # -- discovery, registry, pairing, status ------------------------------------

    @server.tool(annotations=mutating)
    async def devkit_doctor() -> DoctorReport:
        """Probe host dependencies: rsync/ssh/terminal/state dir. Creates the
        state-dir layout and a writability probe file (local mutation)."""
        return await _run(server_state.core().doctor)

    @server.tool(annotations=read_only)
    async def devkit_discover(
        timeout_s: Annotated[
            float, Field(description="Discovery window in seconds", gt=0, le=300)
        ] = float(constants.TIMEOUT_DISCOVER_S),
    ) -> list[DiscoveredDevice]:
        """Browse mDNS for SteamOS devkits. Discovery is progressive
        enhancement: an empty result may mean the MCP host lacks macOS Local
        Network permission — connect by name/IP instead (decision D3)."""
        return await _run(discovery.discover, server_state.core(), timeout_s)

    @server.tool(annotations=read_only)
    async def devkit_list_devices() -> list[Device]:
        """List the registered (paired or explicitly added) devices."""
        return await _run(lambda: server_state.core().state.devices.list())

    @server.tool(annotations=mutating)
    async def devkit_forget_device(device: str) -> Result:
        """Remove a device from the local registry; the device is untouched."""
        core = server_state.core()

        def _forget() -> Result:
            entry = core.state.devices.get(device)
            if entry is None:
                raise DevkitError(
                    ErrorCode.DEVICE_NOT_FOUND,
                    f"no registered device named {device!r}",
                    details={"selector": device},
                )
            core.state.devices.remove(device)
            if core.state.last_device == device:
                core.state.clear_last_device()
            return Result.success(message=f"forgot {device}")

        return await _run(_forget)

    @server.tool(annotations=mutating)
    async def devkit_pair(
        device: str,
        timeout_s: Annotated[
            int, Field(description="Pairing window in seconds", gt=0, le=3600)
        ] = constants.TIMEOUT_PAIR_S,
    ) -> Result:
        """Register our key with a device; approval happens on the device UI.
        A declined/timed-out request is a normal failure Result, not an error."""
        return await _run(pairing.pair, server_state.core(), device, timeout_s)

    @server.tool(annotations=mutating)
    async def devkit_status(device: str) -> DeviceStatus:
        """Read device status via steamos-get-status. Mutates the device on
        first contact per process (devkit-utils upload) and refreshes the
        registry entry's last_seen/family."""
        return await _run(status.get_status, server_state.core(), device)

    @server.tool(annotations=mutating)
    async def devkit_capabilities(device: str | None = None) -> Capabilities:
        """Honest capability report (probes, never hardcoded). With ``device``
        omitted, host capabilities only. Probes mutate local state (state-dir
        probe file) and, with a device, may upload devkit-utils once per
        process. Tools never refuse on capability grounds — this report is how
        a caller learns support status."""
        return await _run(capabilities.capabilities, server_state.core(), device)

    # -- titles and deploy --------------------------------------------------------

    @server.tool(annotations=mutating)
    async def devkit_list_titles(device: str) -> list[Title]:
        """List devkit titles deployed on the device (steamos-list-games).
        May upload devkit-utils on first contact per process."""
        return await _run(titles.list_titles, server_state.core(), device)

    @server.tool(annotations=mutating)
    async def devkit_inspect_title(device: str, gameid: GameId) -> TitleDetail:
        """A title plus its on-device argv/env/settings launch configuration.
        May upload devkit-utils on first contact per process."""
        return await _run(titles.inspect_title, server_state.core(), device, gameid)

    @server.tool(annotations=mutating)
    async def devkit_configure_title(
        device: str,
        gameid: GameId,
        entrypoint: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        runtime: constants.CompatToolAlias | None = None,
        settings: dict[str, str] | None = None,
    ) -> Result:
        """Write a title's argv/env/settings sidecars (TitleConfig fields)."""

        def _configure() -> Result:
            config = TitleConfig(
                entrypoint=entrypoint,
                args=args or [],
                env=env or {},
                runtime=runtime,
                settings=settings or {},
            )
            return titles.configure_title(server_state.core(), device, gameid, config)

        return await _run(_configure)

    @server.tool(annotations=read_only)
    async def devkit_preview_deploy(
        device: str,
        gameid: GameId,
        source_dir: Path,
        filters: FilterSpec | None = None,
        clean: bool = False,
    ) -> DeployPreview:
        """Dry-run a deploy (rsync -n --itemize-changes); never mutates the device."""

        def _preview() -> DeployPreview:
            spec = DeploySpec(
                device=device,
                gameid=gameid,
                source_dir=source_dir,
                filters=filters or FilterSpec(),
                clean=clean,
            )
            return transfer.preview_deploy(server_state.core(), device, spec)

        return await _run(_preview)

    @server.tool(annotations=mutating)
    async def devkit_deploy(
        device: str,
        gameid: GameId,
        source_dir: Path,
        filters: FilterSpec | None = None,
        clean: bool = False,
        checksum: bool = False,
        update: bool = False,
        compress: bool = True,
        register: bool = True,
        launch: bool = False,
        config: TitleConfig | None = None,
        ctx: Context[Any, Any] | None = None,
    ) -> JobStarted:
        """Upload a title tree to the device as a background job.

        Returns the job id immediately; follow with devkit_job_status /
        devkit_job_cancel. ``checksum`` and ``update`` are mutually exclusive."""

        def _submit() -> JobHandle:
            spec = DeploySpec(
                device=device,
                gameid=gameid,
                source_dir=source_dir,
                filters=filters or FilterSpec(),
                clean=clean,
                checksum=checksum,
                update=update,
                compress=compress,
                register=register,
                launch=launch,
                config=config,
            )
            return titles.deploy(server_state.core(), device, spec)

        handle = await _run(_submit)
        return _start_job(server_state, handle, ctx)

    @server.tool(annotations=mutating)
    async def devkit_register_title(device: str, gameid: GameId) -> Result:
        """Register an uploaded title with the device's Steam client."""
        return await _run(titles.register_title, server_state.core(), device, gameid)

    @server.tool(annotations=mutating)
    async def devkit_launch_title(device: str, gameid: GameId) -> LaunchResult:
        """Ask Steam to run a title. ``readiness`` is honestly ``unknown`` —
        the upstream protocol has no readiness signal."""
        return await _run(titles.launch_title, server_state.core(), device, gameid)

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
    async def devkit_delete_title(
        device: str,
        gameid: GameId,
        confirm: Literal[True],
    ) -> Result:
        """Delete a title from the device. Destructive: ``confirm`` must be
        exactly ``true``."""
        return await _run(titles.delete_title, server_state.core(), device, gameid, confirm=confirm)

    # -- diagnostics (jobs; the module lands with M3 — imported lazily) ----------

    @server.tool(annotations=mutating)
    async def devkit_fetch_logs(
        device: str,
        kinds: list[LogKind] | None = None,
        ctx: Context[Any, Any] | None = None,
    ) -> JobStarted:
        """Fetch device logs (steam/proton/dumps/frametimes) as artifacts."""
        handle = await _run(
            _diagnostics().fetch_logs, server_state.core(), device, kinds or ["steam"]
        )
        return _start_job(server_state, handle, ctx)

    @server.tool(annotations=mutating)
    async def devkit_screenshot(
        device: str,
        mode: ScreenshotMode = 3,
        ctx: Context[Any, Any] | None = None,
    ) -> JobStarted:
        """Capture a screenshot as an image artifact (gamescope modes 1-4)."""
        handle = await _run(_diagnostics().screenshot, server_state.core(), device, mode)
        return _start_job(server_state, handle, ctx)

    @server.tool(annotations=mutating)
    async def devkit_controller_config(
        device: str,
        gameid: GameId,
        ctx: Context[Any, Any] | None = None,
    ) -> JobStarted:
        """Fetch the active controller configuration (VDF) as an artifact."""
        handle = await _run(_diagnostics().controller_config, server_state.core(), device, gameid)
        return _start_job(server_state, handle, ctx)

    # -- profiling ------------------------------------------------------------------

    @server.tool(annotations=mutating)
    async def devkit_gpu_trace(device: str, ctx: Context[Any, Any] | None = None) -> JobStarted:
        """Capture a GPU system trace (Deck-only upstream) as a zip artifact."""
        handle = await _run(profiling.gpu_trace, server_state.core(), device)
        return _start_job(server_state, handle, ctx)

    @server.tool(annotations=mutating)
    async def devkit_rgp_capture(device: str, ctx: Context[Any, Any] | None = None) -> JobStarted:
        """Trigger an RGP capture (Deck-only upstream) and download it."""
        handle = await _run(profiling.rgp_capture, server_state.core(), device)
        return _start_job(server_state, handle, ctx)

    @server.tool(annotations=mutating)
    async def devkit_perf_overlay(device: str, enabled: bool) -> Result:
        """Toggle the MangoHud performance overlay on the device."""
        return await _run(profiling.set_perf_overlay, server_state.core(), device, enabled)

    @server.tool(annotations=mutating)
    async def devkit_frametimes(device: str, enabled: bool) -> Result:
        """Toggle MangoHud frametime logging; fetch CSVs via devkit_fetch_logs
        (kinds=["frametimes"]) after disabling."""
        return await _run(profiling.set_frametime_logging, server_state.core(), device, enabled)

    @server.tool(annotations=mutating)
    async def devkit_renderdoc_layer(device: str, enabled: bool) -> Result:
        """Enable/disable the RenderDoc Vulkan capture layer."""
        return await _run(profiling.set_renderdoc_layer, server_state.core(), device, enabled)

    @server.tool(annotations=mutating)
    async def devkit_renderdoc_server(
        device: str, action: Literal["start", "stop", "status"]
    ) -> Result:
        """Start/stop/status the RenderDoc replay server on the device."""
        return await _run(profiling.renderdoc_server, server_state.core(), device, action)

    @server.tool(annotations=mutating)
    async def devkit_proton_logging(
        device: str,
        mode: Literal["off", "on"],
        winedebug: str | None = None,
    ) -> Result:
        """Toggle Proton logging; ``winedebug`` sets the WINEDEBUG channels."""
        return await _run(
            profiling.set_proton_logging, server_state.core(), device, mode, winedebug
        )

    # -- session and steam mode ------------------------------------------------------

    @server.tool(annotations=mutating)
    async def devkit_set_steam_mode(
        device: str,
        mode: Literal["OS", "OS_DEV"],
        gameid: GameId | None = None,
        args: str | None = None,
        gdbserver: bool = False,
    ) -> Result:
        """Set the main-session Steam client mode (OS or OS_DEV; SIDELOADED is
        Valve-internal and rejected as UNSUPPORTED)."""
        return await _run(
            session_control.set_steam_mode,
            server_state.core(),
            device,
            mode,
            gameid,
            args,
            gdbserver,
        )

    @server.tool(annotations=mutating)
    async def devkit_set_session(
        device: str, session: Literal["gamescope", "plasma-x11", "plasma-wayland"]
    ) -> Result:
        """Switch the device session (gamescope or a plasma desktop)."""
        return await _run(session_control.set_session, server_state.core(), device, session)

    @server.tool(annotations=mutating)
    async def devkit_reload_steam(device: str) -> Result:
        """Restart the Steam client (gamescope re-entry on Deck, systemd user
        service restart on Frame)."""
        return await _run(session_control.reload_steam, server_state.core(), device)

    # -- jobs and artifacts -------------------------------------------------------------

    @server.tool(annotations=read_only)
    async def devkit_job_status(job_id: str) -> JobInfo:
        """Current state, progress, and artifact ids of one job."""
        return await _run(server_state.core().job_store.load, job_id)

    @server.tool(annotations=mutating)
    async def devkit_job_cancel(job_id: str) -> Result:
        """Cancel a live job owned by this server process. Terminal jobs are a
        no-op; jobs from other processes cannot be cancelled (spec appendix A)."""

        def _cancel() -> Result:
            core = server_state.core()
            handle = server_state.jobs.get(job_id)
            if handle is not None:
                handle.cancel()
                return Result.success(message=f"cancel requested for {job_id}")
            info = core.job_store.load(job_id)
            if info.state.is_terminal:
                return Result.success(message=f"job {job_id} already finished ({info.state})")
            return Result.failure(
                ErrorCode.UNSUPPORTED,
                f"job {job_id} is not owned by this process; cancel it where it was started",
            )

        return await _run(_cancel)

    @server.tool(annotations=read_only)
    async def devkit_list_jobs(
        device: str | None = None, state: JobState | None = None
    ) -> list[JobInfo]:
        """List jobs, oldest first, optionally filtered by device and/or state."""

        def _list() -> list[JobInfo]:
            return server_state.core().job_store.list_jobs(device=device, state=state)

        return await _run(_list)

    @server.tool(annotations=read_only)
    async def devkit_list_artifacts(
        device: str | None = None, kind: str | None = None
    ) -> list[ArtifactEntry]:
        """List captured artifacts with their ``devkit://artifact/<id>`` links."""

        def _list() -> list[ArtifactEntry]:
            return [
                ArtifactEntry(artifact=a, resource_uri=f"devkit://artifact/{a.id}")
                for a in artifacts.list_artifacts(
                    server_state.core().state, device=device, kind=kind
                )
            ]

        return await _run(_list)

    @server.tool(annotations=read_only)
    async def devkit_read_artifact(
        artifact_id: str,
        tail_lines: Annotated[int, Field(gt=0, le=2000)] = 200,
    ) -> ArtifactRead:
        """Bounded text excerpt (<= 2000 lines / 64 KiB) for text artifacts;
        binary and directory artifacts come back as their resource link."""

        def _read() -> ArtifactRead:
            core = server_state.core()
            artifact = artifacts.get_artifact(core.state, artifact_id)
            excerpt: str | None = None
            if not artifact.path.is_dir() and _is_textish(artifact):
                excerpt = artifacts.read_artifact_excerpt(
                    core.state, artifact_id, tail_lines=tail_lines
                )
            return ArtifactRead(
                artifact_id=artifact.id,
                kind=artifact.kind,
                media_type=artifact.media_type,
                resource_uri=f"devkit://artifact/{artifact.id}",
                excerpt=excerpt,
            )

        return await _run(_read)

    # -- resources -------------------------------------------------------------------

    @server.resource(
        "devkit://devices",
        name="devices",
        description="Registered devices (the pairing registry) as JSON.",
        mime_type="application/json",
    )
    async def devices_resource() -> str:
        devices = await _run(lambda: server_state.core().state.devices.list())
        return json.dumps([d.model_dump(mode="json") for d in devices], indent=2)

    @server.resource(
        "devkit://device/{name}/titles",
        name="device-titles",
        description="Titles deployed on one device as JSON (contacts the device).",
        mime_type="application/json",
    )
    async def device_titles_resource(name: str) -> str:
        found = await _run(titles.list_titles, server_state.core(), name)
        return json.dumps([t.model_dump(mode="json") for t in found], indent=2)

    @server.resource(
        "devkit://job/{job_id}",
        name="job",
        description="One job's current JobInfo (state, progress, artifacts) as JSON.",
        mime_type="application/json",
    )
    async def job_resource(job_id: str) -> str:
        info = await _run(server_state.core().job_store.load, job_id)
        return info.model_dump_json(indent=2)

    class ArtifactResourceTemplate(ResourceTemplate):
        """``devkit://artifact/{artifact_id}`` with the payload's own MIME type:
        bytes for image/binary kinds, bounded text (<= 2000 lines / 64 KiB) for
        text kinds (spec section 11). The stock template pins one MIME type at
        registration, so this subclass resolves it per artifact."""

        async def create_resource(
            self, uri: str, params: dict[str, Any], context: Context[Any, Any]
        ) -> Resource:
            artifact_id = str(params["artifact_id"])
            try:
                core = server_state.core()
                artifact = artifacts.get_artifact(core.state, artifact_id)
                if artifact.path.is_dir():
                    raise DevkitError(
                        ErrorCode.VALIDATION,
                        f"artifact {artifact_id} is a directory of files; use "
                        "devkit_read_artifact or fetch files from its path",
                    )
                if _is_textish(artifact):
                    content: str | bytes = artifacts.read_artifact_excerpt(
                        core.state, artifact_id, tail_lines=2000
                    )
                    mime = artifact.media_type or "text/plain"
                else:
                    content = artifact.path.read_bytes()
                    mime = artifact.media_type or "application/octet-stream"
            except DevkitError as err:
                raise _resource_error(err) from err
            return FunctionResource(uri=uri, name=artifact_id, mime_type=mime, fn=lambda: content)

    def _artifact_payload_marker(artifact_id: str) -> None:
        """Signature anchor for the artifact template; create_resource overrides it."""

    artifact_template = ArtifactResourceTemplate.from_function(
        _artifact_payload_marker,
        uri_template="devkit://artifact/{artifact_id}",
        name="artifact",
        description=(
            "One captured artifact's payload: bytes for image/binary kinds, a "
            "bounded text excerpt (<= 2000 lines / 64 KiB) for text kinds."
        ),
    )
    # The manager has no public add-template-instance path; register directly.
    server._resource_manager._templates[artifact_template.uri_template] = artifact_template

    return server


def _is_textish(artifact: Artifact) -> bool:
    """Whether an artifact payload is served as bounded text (spec section 11)."""
    media = artifact.media_type or ""
    return (
        artifact.kind in _TEXTISH_ARTIFACT_KINDS
        or media.startswith("text/")
        or media == "application/json"
    )


def run_stdio(state_dir: Path | None = None) -> None:
    """Run the server over stdio (blocking) — the `devkit mcp` / `devkit-mcp` entry."""
    create_server(state_dir=state_dir).run("stdio")


__all__ = [
    "ArtifactEntry",
    "ArtifactRead",
    "JobStarted",
    "create_server",
    "run_stdio",
]
