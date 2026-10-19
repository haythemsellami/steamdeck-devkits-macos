"""Session and Steam client commands (spec section 10)."""

from __future__ import annotations

from typing import cast

import typer

from steamos_devkit import session as ops
from steamos_devkit.cli.common import die, emit, get_options, make_core, resolve_selector
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Result
from steamos_devkit.session import SessionName, SteamMode

_STEAM_MODES: dict[str, SteamMode] = {"os": "OS", "os-dev": "OS_DEV"}
_SESSIONS = ("gamescope", "plasma-x11", "plasma-wayland")

_SEL_HELP = "Device selector (registry name, IP, or hostname; default: last device)."


def _shift(sel: str | None, value: str | None, name: str) -> tuple[str | None, str]:
    """``<sel> <value>`` positionals with an optional selector.

    One positional given is the value (device falls back to the last-device
    marker); two given means the first is the device.
    """
    if value is not None:
        return sel, value
    if sel is None:
        raise DevkitError(ErrorCode.VALIDATION, f"missing {name} argument")
    return None, sel


def _emit_result(ctx: typer.Context, result: Result) -> None:
    emit(ctx, result.model_dump_json(), result.message or "")


def _steam_mode(
    ctx: typer.Context,
    sel: str | None = typer.Argument(None, help=_SEL_HELP),
    mode: str | None = typer.Argument(None, metavar="os|os-dev", help="Steam client mode."),
    gameid: str | None = typer.Option(None, "--gameid", help="Gameid passed to the helper."),
    args: str | None = typer.Option(
        None, "--args", help="Extra Steam client command line arguments."
    ),
    gdbserver: bool = typer.Option(
        False, "--gdbserver", help="Launch the client under gdbserver (port 2345)."
    ),
) -> None:
    """Set the main-session Steam client mode (OS default or OS_DEV override)."""
    try:
        device, mode = _shift(sel, mode, "os|os-dev")
        target = _STEAM_MODES.get(mode.lower())
        if target is None:
            raise DevkitError(ErrorCode.VALIDATION, f"expected os|os-dev, got {mode!r}")
        result = ops.set_steam_mode(
            make_core(ctx),
            resolve_selector(ctx, device),
            target,
            gameid=gameid,
            args=args,
            gdbserver=gdbserver,
        )
    except DevkitError as err:
        die(err, get_options(ctx).json)
    _emit_result(ctx, result)


def _session(
    ctx: typer.Context,
    sel: str | None = typer.Argument(None, help=_SEL_HELP),
    session: str | None = typer.Argument(
        None, metavar="gamescope|plasma-x11|plasma-wayland", help="Target session."
    ),
) -> None:
    """Switch the device session (holo-session-select with legacy fallback)."""
    try:
        device, session = _shift(sel, session, "gamescope|plasma-x11|plasma-wayland")
        lowered = session.lower()
        if lowered not in _SESSIONS:
            raise DevkitError(
                ErrorCode.VALIDATION,
                f"expected gamescope|plasma-x11|plasma-wayland, got {session!r}",
            )
        result = ops.set_session(
            make_core(ctx), resolve_selector(ctx, device), cast(SessionName, lowered)
        )
    except DevkitError as err:
        die(err, get_options(ctx).json)
    _emit_result(ctx, result)


def _reload_steam(
    ctx: typer.Context,
    sel: str | None = typer.Argument(None, help=_SEL_HELP),
) -> None:
    """Restart the Steam client (gamescope re-entry on Deck, service on Frame)."""
    try:
        result = ops.reload_steam(make_core(ctx), resolve_selector(ctx, sel))
    except DevkitError as err:
        die(err, get_options(ctx).json)
    _emit_result(ctx, result)


def register(app: typer.Typer) -> None:
    app.command(name="steam-mode")(_steam_mode)
    app.command(name="session")(_session)
    app.command(name="reload-steam")(_reload_steam)
