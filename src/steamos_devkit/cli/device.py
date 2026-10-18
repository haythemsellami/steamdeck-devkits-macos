"""Device lifecycle commands (spec section 10): discover, devices, pair,
status, capabilities. Device selectors fall back to the last-device marker
(spec section 5); all output goes through emit/die (JSON on stdout only).
"""

from __future__ import annotations

import json as jsonlib

import typer

from steamos_devkit import capabilities as capabilities_mod
from steamos_devkit import constants, discovery, pairing
from steamos_devkit import status as status_mod
from steamos_devkit.cli.common import die, emit, get_options, make_core
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import (
    Capabilities,
    Device,
    DeviceStatus,
    DiscoveredDevice,
    Result,
)

_TIMEOUT_PAIR_HELP = "Seconds to wait for on-device approval of the pairing request."


def _selector_or_last(core: DevkitCore, selector: str | None) -> str:
    """Explicit selector wins; otherwise the last-device marker (spec section 5)."""
    if selector is not None:
        return selector
    last = core.state.last_device
    if last is None:
        raise DevkitError(
            ErrorCode.VALIDATION,
            "no device given and no device on record; pass a name or IP "
            "(see `devkit discover` / `devkit pair`)",
        )
    return last


def _dump_models(models: list[DiscoveredDevice] | list[Device]) -> str:
    return jsonlib.dumps([m.model_dump(mode="json") for m in models])


def _render_discovered(devices: list[DiscoveredDevice]) -> str:
    if not devices:
        return "no devices discovered via mDNS; connect by IP instead: devkit pair <ip>"
    lines = []
    for device in devices:
        sshd = {True: "yes", False: "no", None: "?"}[device.sshd]
        lines.append(
            f"{device.name}  {device.address}:{device.port}  "
            f"login={device.login or '?'} sshd={sshd}"
        )
    return "\n".join(lines)


def _render_devices(devices: list[Device]) -> str:
    if not devices:
        return "no registered devices; run `devkit discover` or `devkit pair <ip>`"
    lines = []
    for device in devices:
        seen = device.last_seen.isoformat() if device.last_seen else "never"
        paired = device.paired_at.isoformat() if device.paired_at else "unpaired"
        lines.append(
            f"{device.name}  {device.address}:{device.port}  login={device.login} "
            f"family={device.family} paired={paired} last_seen={seen}"
        )
    return "\n".join(lines)


def _render_status(report: DeviceStatus) -> str:
    family = "Steam Frame" if report.is_deckard else "Steam Deck"
    version = report.steam_osclient_version
    lines = [
        f"{report.hostname} — {report.os_name} {report.os_version} ({family})",
        f"session: {report.session_status}   steam: {report.steam_status}",
        f"steam branch: {report.steam_osclient_branch or '?'}   version: {version or '?'}",
        f"cef debugging: {'on' if report.cef_debugging_enabled else 'off'}   "
        f"renderdoc layer: {'on' if report.renderdoc_layer_enabled else 'off'}   "
        f"renderdoc server: {'running' if report.renderdoc_replay_server_running else 'stopped'}",
    ]
    return "\n".join(lines)


def _render_capabilities(caps: Capabilities) -> str:
    lines = [
        f"host: {caps.host.platform} {caps.host.arch} (context: {caps.host.context})",
        f"  rsync: {caps.host.rsync.flavor or 'not found'} {caps.host.rsync.version or ''} "
        f"— {'ok' if caps.host.rsync.ok else caps.host.rsync.message or 'unusable'}",
        f"  ssh: {caps.host.ssh_path or 'not found'}   terminal: {caps.host.terminal or 'none'}",
        f"  local network: {caps.host.local_network} (not programmatically readable)",
    ]
    if caps.device is not None:
        device = caps.device
        lines.append(
            f"device: family={device.family} os={device.os_version or '?'} "
            f"steam={device.steam_branch or '?'} {device.steam_version or ''}"
        )
        lines.append(
            f"  gpu-trace: {_tri(device.has_gpu_trace)}   "
            f"mangohudctl: {_tri(device.has_mangohudctl)}   "
            f"session-select: {device.session_select}"
        )
    lines.append("operations:")
    for name in sorted(caps.operations):
        op = caps.operations[name]
        detail = op.reason or op.dependency or ""
        suffix = f" — {detail}" if detail else ""
        lines.append(f"  {name}: {op.status}{suffix}")
    return "\n".join(lines)


def _tri(value: bool | None) -> str:
    return {True: "yes", False: "no", None: "?"}[value]


def _discover(
    ctx: typer.Context,
    timeout: float = typer.Option(
        constants.TIMEOUT_DISCOVER_S, "--timeout", help="Discovery window in seconds."
    ),
) -> None:
    """Discover SteamOS devkits on the local network via mDNS."""
    core = make_core(ctx)
    try:
        devices = discovery.discover(core, timeout_s=timeout)
    except DevkitError as err:
        die(err, get_options(ctx).json)
    emit(ctx, _dump_models(devices), _render_discovered(devices))


def _devices_list(ctx: typer.Context) -> None:
    """List registered (paired or added-by-IP) devices."""
    core = make_core(ctx)
    devices = core.state.devices.list()
    emit(ctx, _dump_models(devices), _render_devices(devices))


def _devices_forget(
    ctx: typer.Context,
    selector: str | None = typer.Argument(None, help="Registry name (default: last device)."),
) -> None:
    """Remove a device from the local registry; the device itself is untouched."""
    core = make_core(ctx)
    try:
        name = _selector_or_last(core, selector)
        entry = core.state.devices.get(name)
        if entry is None:
            raise DevkitError(
                ErrorCode.DEVICE_NOT_FOUND,
                f"no registered device named {name!r}",
                details={"selector": name},
            )
        core.state.devices.remove(name)
        if core.state.last_device == name:
            core.state.clear_last_device()
        result = Result.success(device=entry, message=f"forgot {name}")
    except DevkitError as err:
        die(err, get_options(ctx).json)
    emit(
        ctx,
        result.model_dump_json(),
        f"forgot {entry.name} (local registry only; the device is untouched)",
    )


def _pair(
    ctx: typer.Context,
    selector: str | None = typer.Argument(
        None, help="Device name, IP, or hostname (default: last device)."
    ),
    timeout: int = typer.Option(constants.TIMEOUT_PAIR_S, "--timeout", help=_TIMEOUT_PAIR_HELP),
) -> None:
    """Pair with a device: register our SSH key via the device approval prompt."""
    core = make_core(ctx)
    try:
        name = _selector_or_last(core, selector)
        result = pairing.pair(core, name, timeout_s=timeout)
        if not result.ok:
            die(
                DevkitError(result.code or ErrorCode.INTERNAL, result.message or "pairing failed"),
                get_options(ctx).json,
            )
    except DevkitError as err:
        die(err, get_options(ctx).json)
    device = result.device
    human = (
        f"paired with {device.name} ({device.address}, login {device.login})"
        if device is not None
        else "paired"
    )
    emit(ctx, result.model_dump_json(), human)


def _status(
    ctx: typer.Context,
    selector: str | None = typer.Argument(
        None, help="Device name, IP, or hostname (default: last device)."
    ),
) -> None:
    """Read device status via steamos-get-status."""
    core = make_core(ctx)
    try:
        report = status_mod.get_status(core, _selector_or_last(core, selector))
    except DevkitError as err:
        die(err, get_options(ctx).json)
    emit(ctx, report.model_dump_json(), _render_status(report))


def _capabilities(
    ctx: typer.Context,
    selector: str | None = typer.Argument(
        None, help="Device to probe (default: last device; omit for host-only)."
    ),
) -> None:
    """Report honest host/device capabilities and the operations matrix."""
    core = make_core(ctx)
    try:
        name = selector if selector is not None else core.state.last_device
        caps = capabilities_mod.capabilities(core, name)
    except DevkitError as err:
        die(err, get_options(ctx).json)
    emit(ctx, caps.model_dump_json(), _render_capabilities(caps))


def register(app: typer.Typer) -> None:
    devices_app = typer.Typer(help="Local device registry.", no_args_is_help=True)
    devices_app.command(name="list")(_devices_list)
    devices_app.command(name="forget")(_devices_forget)
    app.add_typer(devices_app, name="devices")
    app.command(name="discover")(_discover)
    app.command(name="pair")(_pair)
    app.command(name="status")(_status)
    app.command(name="capabilities")(_capabilities)
