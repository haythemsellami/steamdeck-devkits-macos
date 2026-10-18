"""Steam client mode and session control (spec section 8; audit section 4).

These wrap the device-side helpers uploaded by ``ensure_utils()``
(``steamos-set-steam-client``, ``steamos-get-status``) plus the OS-shipped
session selectors (``holo-session-select`` on SteamOS >= 3.9, falling back to
``steamos-session-select`` — the choice is made on the device at run time).
"""

from __future__ import annotations

import logging
import re
import shlex
from typing import TYPE_CHECKING, Literal

from steamos_devkit import constants
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Result

if TYPE_CHECKING:
    from steamos_devkit.core import DevkitCore

logger = logging.getLogger(__name__)

SteamMode = Literal["OS", "OS_DEV"]
SessionName = Literal["gamescope", "plasma-x11", "plasma-wayland"]

# upstream :1790 — decide on the device which selector binary exists
_SESSION_SELECT = (
    "if command -v holo-session-select >/dev/null 2>&1; "
    "then holo-session-select {arg}; else steamos-session-select {arg}; fi"
)
# upstream :1969-1971 — wayland marker honored by the session scripts
_WAYLAND_MARKER = '${XDG_CONF_DIR:-"$HOME/.config"}/steamos-session-type'

# The helper's --gameid is required by its argparse but unused for OS/OS_DEV
# (steamos-set-steam-client); upstream's GUI passes "steam" here.
_HELPER_PLACEHOLDER_GAMEID = "steam"

_STEAM_MODES: frozenset[str] = frozenset({"OS", "OS_DEV"})
_SIDELOADED_MODES: frozenset[str] = frozenset({"SIDELOADED"})
_SESSIONS: frozenset[str] = frozenset({"gamescope", "plasma-x11", "plasma-wayland"})


def _session_select_command(arg: str) -> str:
    return _SESSION_SELECT.format(arg=arg)


def set_steam_mode(
    core: DevkitCore,
    sel: str,
    mode: SteamMode,
    gameid: str | None = None,
    args: str | None = None,
    gdbserver: bool = False,
) -> Result:
    """Set the main-session Steam client mode (audit :1397-1401).

    SIDELOADED is Valve-internal (audit section 4) and rejected with
    UNSUPPORTED; the device-side helper is uploaded first via ensure_utils().
    """
    # set[str] operands: runtime validation for untyped (MCP) callers without
    # literal narrowing on the Literal-typed parameter
    if mode in _SIDELOADED_MODES:
        raise DevkitError(ErrorCode.UNSUPPORTED, "sideload is Valve-internal (audit section 4)")
    if mode not in _STEAM_MODES:
        raise DevkitError(ErrorCode.VALIDATION, f"unknown steam mode: {mode!r}")
    effective_gameid = gameid if gameid is not None else _HELPER_PLACEHOLDER_GAMEID
    if not re.fullmatch(constants.GAMEID_REGEX, effective_gameid):
        raise DevkitError(ErrorCode.VALIDATION, f"invalid gameid: {effective_gameid!r}")
    handle = core.device_handle(sel)
    handle.ensure_utils()
    command = (
        "python3 ~/devkit-utils/steamos-set-steam-client "
        f"--client SteamStatus.{mode} --gameid {effective_gameid}"
    )
    if args is not None:
        command += f" --args={shlex.quote(args)}"
    if gdbserver:
        command += " --gdbserver"
    handle.run(command)
    return Result.success(device=handle.device, message=f"steam client mode set to {mode}")


def set_session(core: DevkitCore, sel: str, session: SessionName) -> Result:
    """Switch the device session (audit :1787-1799, :1959-1975)."""
    if session not in _SESSIONS:
        raise DevkitError(ErrorCode.VALIDATION, f"unknown session: {session!r}")
    handle = core.device_handle(sel)
    if session == "plasma-wayland":
        handle.run(f"echo wayland > {_WAYLAND_MARKER}")
        select_arg = "plasma"
    else:
        handle.run(f"rm -f {_WAYLAND_MARKER}")
        select_arg = "plasma" if session == "plasma-x11" else "gamescope"
    handle.run(_session_select_command(select_arg))
    return Result.success(device=handle.device, message=f"session switch to {session} requested")


def reload_steam(core: DevkitCore, sel: str) -> Result:
    """Restart the Steam client (audit :1793-1799).

    Deck: re-select the gamescope session. Frame (Deckard): restart the steam
    user service. The family comes from a fresh steamos-get-status run.
    """
    handle = core.device_handle(sel)
    handle.ensure_utils()
    status = handle.run_json("python3 ~/devkit-utils/steamos-get-status --json")
    if not isinstance(status, dict):
        raise DevkitError(
            ErrorCode.REMOTE_COMMAND_FAILED,
            "unexpected steamos-get-status payload (expected a JSON object)",
        )
    if status.get("is_deckard"):
        handle.run("systemctl --user unmask steam ; systemctl --user restart steam")
        return Result.success(device=handle.device, message="steam service restarted")
    handle.run(_session_select_command("gamescope"))
    return Result.success(device=handle.device, message="gamescope session restarted")
