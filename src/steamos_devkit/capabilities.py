"""Honest capability reporting (spec section 9).

Everything is built from probes, never hardcoded: host probes come from the
adapter's ``discover_tools()`` plus ``context()``; device probes (when a
selector is given) come from :func:`steamos_devkit.status.get_status` plus
``command -v`` feature checks. Nothing is ``supported_tested`` until
``docs/hardware-matrix.md`` records it green; operations upstream cannot do
are ``unsupported_upstream`` and are never faked (audit section 7).
"""

from __future__ import annotations

import logging
import os
import platform
import sys
import tempfile
from typing import TYPE_CHECKING, Literal

from steamos_devkit.errors import DevkitError
from steamos_devkit.models import (
    Capabilities,
    DeviceCapabilities,
    HostCapabilities,
    OperationCapability,
)
from steamos_devkit.status import get_status

if TYPE_CHECKING:
    from steamos_devkit.core import DevkitCore
    from steamos_devkit.device import DeviceHandle
    from steamos_devkit.models import DeviceStatus

logger = logging.getLogger(__name__)

# Operations whose device-side helpers ship in the vendored devkit-utils tree
# (uploaded over rsync on identify, upstream :1919-1938) or that bulk-transfer
# with rsync themselves. When the host has no fit rsync these degrade to
# dependency_required instead of pretending they would work.
_RSYNC_DEPENDENT = frozenset(
    {
        "status",
        "list_titles",
        "inspect_title",
        "configure_title",
        "register_title",
        "delete_title",
        "set_steam_mode",
        "preview_deploy",
        "deploy",
        "fetch_logs",
        "frametimes",
        "controller_config",
    }
)

_OperationStatus = Literal[
    "supported_tested",
    "supported_untested",
    "dependency_required",
    "capture_export_only",
    "unavailable_host",
    "unavailable_device",
    "unsupported_upstream",
]

_UNSUPPORTED_UPSTREAM_REASON = (
    "the upstream protocol has no such device operation (audit-upstream.md section 7); "
    "we report this honestly instead of faking it"
)


def _probe_state_dir_writable(core: DevkitCore) -> bool:
    try:
        core.state.ensure_layout()
        fd, probe = tempfile.mkstemp(dir=core.state.root, prefix=".caps-", suffix=".tmp")
        os.close(fd)
        os.unlink(probe)
    except OSError:
        return False
    return True


def _has_command(handle: DeviceHandle, name: str) -> bool | None:
    """``command -v <name>`` on the device; None when the probe itself failed."""
    try:
        return handle.run(f"command -v {name}", check=False).exit_code == 0
    except DevkitError:
        logger.debug("command -v %s probe failed", name, exc_info=True)
        return None


def _steam_version(status: DeviceStatus) -> int | None:
    version = status.steam_osclient_version
    if isinstance(version, int):
        return version
    if isinstance(version, str):
        try:
            return int(version)
        except ValueError:
            return None
    return None


def _probe_device(
    core: DevkitCore, selector: str
) -> tuple[DeviceCapabilities, dict[str, bool | None]]:
    status = get_status(core, selector)
    handle = core.device_handle(selector)
    probes = {
        "gpu-trace": _has_command(handle, "gpu-trace"),
        "mangohudctl": _has_command(handle, "mangohudctl"),
        "renderdoccmd": _has_command(handle, "renderdoccmd"),
    }
    session_select: Literal["holo-session-select", "steamos-session-select", "unknown"]
    raw_session_select = status.raw.get("session_select")
    if raw_session_select in ("holo-session-select", "steamos-session-select"):
        session_select = raw_session_select
    elif _has_command(handle, "holo-session-select"):
        # Older devices do not report it: probe (holo landed in SteamOS 3.9).
        session_select = "holo-session-select"
    elif _has_command(handle, "steamos-session-select"):
        session_select = "steamos-session-select"
    else:
        session_select = "unknown"
    device = DeviceCapabilities(
        family="frame" if status.is_deckard else "deck",
        os_version=status.os_version,
        steam_branch=status.steam_osclient_branch,
        steam_version=_steam_version(status),
        has_gpu_trace=probes["gpu-trace"],
        renderdoc_layer_enabled=status.renderdoc_layer_enabled,
        renderdoc_replay_server_running=status.renderdoc_replay_server_running,
        cef_debugging_enabled=status.cef_debugging_enabled,
        has_mangohudctl=probes["mangohudctl"],
        session_select=session_select,
    )
    return device, probes


def _build_operations(
    host: HostCapabilities,
    device: DeviceCapabilities | None,
    probes: dict[str, bool | None],
) -> dict[str, OperationCapability]:
    ops: dict[str, OperationCapability] = {}

    def add(
        name: str,
        status: _OperationStatus,
        reason: str | None = None,
        dependency: str | None = None,
    ) -> None:
        ops[name] = OperationCapability(status=status, reason=reason, dependency=dependency)

    # Host-local operations: no device involvement.
    for name in (
        "doctor",
        "discover",
        "list_devices",
        "forget_device",
        "pair",
        "capabilities",
        "job_status",
        "job_cancel",
        "list_jobs",
        "list_artifacts",
        "read_artifact",
        "migrate",
        "setup",
        "version",
    ):
        add(name, "supported_untested")
    if host.terminal is None:
        add(
            "shell",
            "dependency_required",
            reason="no terminal emulator found for interactive shells",
            dependency="Terminal.app or iTerm2",
        )
    else:
        add("shell", "supported_untested")

    # Device operations the upstream protocol supports.
    for name in (
        "status",
        "list_titles",
        "inspect_title",
        "configure_title",
        "preview_deploy",
        "deploy",
        "register_title",
        "launch_title",
        "delete_title",
        "fetch_logs",
        "screenshot",
        "controller_config",
        "perf_overlay",
        "frametimes",
        "renderdoc_layer",
        "renderdoc_server",
        "proton_logging",
        "set_steam_mode",
        "set_session",
        "reload_steam",
    ):
        add(name, "supported_untested")

    # Deck-only capture paths (upstream :1644-1767): gpu-trace and the RGP
    # trigger flow target the Deck's AMD GPU; they are absent on Steam Frame.
    for name in ("gpu_trace", "rgp_capture"):
        if device is not None and device.family == "frame":
            add(name, "unavailable_device", reason="gpu-trace/RGP capture is Deck-only upstream")
        elif device is not None and probes.get("gpu-trace") is False:
            add(name, "unavailable_device", reason="gpu-trace not found on the device")
        else:
            add(name, "supported_untested")

    # Device probes that came back negative downgrade the affected operations.
    if device is not None and device.has_mangohudctl is False:
        for name in ("perf_overlay", "frametimes"):
            add(name, "unavailable_device", reason="mangohudctl not found on the device")
    if device is not None and probes.get("renderdoccmd") is False:
        for name in ("renderdoc_layer", "renderdoc_server"):
            add(name, "unavailable_device", reason="renderdoccmd not found on the device")

    # Windows-host-only upstream feature (audit section 4).
    add(
        "vs_remote_debug",
        "unavailable_host",
        reason="msvsmon deployment is a Windows-host-only upstream feature",
    )

    # Things upstream cannot do at all — never faked (audit section 7).
    add("stop_title", "unsupported_upstream", reason=_UNSUPPORTED_UPSTREAM_REASON)
    add("launch_readiness", "unsupported_upstream", reason=_UNSUPPORTED_UPSTREAM_REASON)
    add("title_stdout_streaming", "unsupported_upstream", reason=_UNSUPPORTED_UPSTREAM_REASON)

    # A broken/missing rsync degrades every helper- or bulk-transfer-based op.
    if not host.rsync.ok:
        for name in sorted(_RSYNC_DEPENDENT):
            if ops[name].status == "supported_untested":
                add(
                    name,
                    "dependency_required",
                    reason=host.rsync.message or "rsync 3.x not found",
                    dependency="rsync 3.x",
                )
    return ops


def capabilities(core: DevkitCore, selector: str | None = None) -> Capabilities:
    """Host capabilities, plus device probes when a selector is given."""
    tools = core.adapter.discover_tools()
    host = HostCapabilities(
        platform=sys.platform,
        arch=platform.machine(),
        context=core.adapter.context(),
        python_version=platform.python_version(),
        state_dir=core.state.root,
        state_dir_writable=_probe_state_dir_writable(core),
        rsync=tools.rsync,
        ssh_path=tools.ssh_path,
        ssh_version=tools.ssh_version,
        terminal=tools.terminal,
        # TCC is not programmatically readable; reported honestly as unknown.
        local_network="unknown",
    )
    device: DeviceCapabilities | None = None
    probes: dict[str, bool | None] = {}
    if selector is not None:
        device, probes = _probe_device(core, selector)
    return Capabilities(
        host=host,
        device=device,
        operations=_build_operations(host, device, probes),
    )
