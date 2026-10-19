"""``devkit titles`` commands and top-level ``devkit launch`` (spec section 10)."""

from __future__ import annotations

import json
import shlex
from typing import cast

import typer

from steamos_devkit import constants
from steamos_devkit.cli.common import die, emit, get_options, make_core, resolve_selector
from steamos_devkit.constants import CompatToolAlias
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import TitleConfig
from steamos_devkit.titles import (
    configure_title,
    delete_title,
    inspect_title,
    launch_title,
    list_titles,
    register_title,
)

_SEL_HELP = "Device selector (registry name, IP, or hostname; default: last device)."
_GAMEID_ARG = (
    "Title gameid. With one positional given it is the gameid "
    "and the device defaults to the last device."
)


def _gameid_or_shift(sel: str | None, gameid: str | None) -> tuple[str | None, str]:
    """``<sel> <gameid>`` with an optional selector: one positional = gameid."""
    if gameid is not None:
        return sel, gameid
    if sel is None:
        raise DevkitError(ErrorCode.VALIDATION, "missing gameid")
    return None, sel


def parse_kv_pairs(pairs: list[str], flag: str) -> dict[str, str]:
    """``--env``/``--set`` repeated KEY=VALUE flags into a dict."""
    result: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise DevkitError(
                ErrorCode.VALIDATION, f"{flag} expects KEY=VALUE with a non-empty key, got {pair!r}"
            )
        result[key] = value
    return result


def parse_runtime(value: str) -> CompatToolAlias:
    if value not in constants.COMPAT_TOOL_ALIASES:
        valid = ", ".join(sorted(constants.COMPAT_TOOL_ALIASES))
        raise DevkitError(
            ErrorCode.VALIDATION, f"unknown runtime {value!r}; valid aliases: {valid}"
        )
    return cast("CompatToolAlias", value)


def _list(ctx: typer.Context, sel: str | None = typer.Argument(None, help=_SEL_HELP)) -> None:
    """List devkit titles deployed on the device."""
    core = make_core(ctx)
    try:
        found = list_titles(core, resolve_selector(ctx, sel))
    except DevkitError as err:
        die(err, get_options(ctx).json)
    payload = json.dumps([t.model_dump(mode="json") for t in found])
    human = "\n".join(t.gameid for t in found) if found else "no titles on device"
    emit(ctx, payload, human)


def _inspect(
    ctx: typer.Context,
    sel: str | None = typer.Argument(None, help=_SEL_HELP),
    gameid: str | None = typer.Argument(None, help=_GAMEID_ARG),
) -> None:
    """Show one title with its parsed on-device argv/env/settings sidecars."""
    core = make_core(ctx)
    try:
        device, gameid = _gameid_or_shift(sel, gameid)
        detail = inspect_title(core, resolve_selector(ctx, device), gameid)
    except DevkitError as err:
        die(err, get_options(ctx).json)
    human_lines = [f"{detail.gameid}  ({detail.remote_dir})"]
    if detail.config.entrypoint:
        human_lines.append(
            "entrypoint: " + shlex.join([detail.config.entrypoint, *detail.config.args])
        )
    if detail.config.runtime:
        human_lines.append(f"runtime: {detail.config.runtime}")
    human_lines.append(
        f"env: {len(detail.config.env)} var(s), settings: {len(detail.config.settings)} key(s)"
    )
    emit(ctx, detail.model_dump_json(), "\n".join(human_lines))


def _configure(
    ctx: typer.Context,
    sel: str | None = typer.Argument(None, help=_SEL_HELP),
    gameid: str | None = typer.Argument(None, help=_GAMEID_ARG),
    entrypoint: str | None = typer.Option(
        None, "--entrypoint", help="Start command (argv[0]), relative to the game directory."
    ),
    args: str | None = typer.Option(
        None, "--args", help="Extra command-line arguments, shell-style split into argv[1:]."
    ),
    env: list[str] = typer.Option([], "--env", help="Environment variable, KEY=VALUE. Repeatable."),
    runtime: str | None = typer.Option(None, "--runtime", help="compat_tool alias."),
    settings: list[str] = typer.Option(
        [], "--set", help="Launch setting, KEY=VALUE (steam_play, gdbserver, ...). Repeatable."
    ),
) -> None:
    """Configure a title's launch sidecars; flags merge over the stored config."""
    core = make_core(ctx)
    try:
        device, gameid = _gameid_or_shift(sel, gameid)
        selector = resolve_selector(ctx, device)
        stored = core.state.load_title(gameid)
        base = stored.config if stored is not None else TitleConfig()
        merged = TitleConfig(
            entrypoint=entrypoint if entrypoint is not None else base.entrypoint,
            args=shlex.split(args) if args is not None else base.args,
            env={**base.env, **parse_kv_pairs(env, "--env")},
            runtime=parse_runtime(runtime) if runtime is not None else base.runtime,
            settings={**base.settings, **parse_kv_pairs(settings, "--set")},
        )
        result = configure_title(core, selector, gameid, merged)
    except DevkitError as err:
        die(err, get_options(ctx).json)
    except ValueError as err:  # shlex.split on unbalanced quotes
        die(DevkitError(ErrorCode.VALIDATION, str(err)), get_options(ctx).json)
    emit(ctx, result.model_dump_json(), result.message or f"title {gameid} configured")


def _register(
    ctx: typer.Context,
    sel: str | None = typer.Argument(None, help=_SEL_HELP),
    gameid: str | None = typer.Argument(None, help=_GAMEID_ARG),
) -> None:
    """(Re)register a deployed title with the device's Steam client."""
    core = make_core(ctx)
    try:
        device, gameid = _gameid_or_shift(sel, gameid)
        result = register_title(core, resolve_selector(ctx, device), gameid)
    except DevkitError as err:
        die(err, get_options(ctx).json)
    emit(ctx, result.model_dump_json(), result.message or f"{gameid} registered")


def _delete(
    ctx: typer.Context,
    sel: str | None = typer.Argument(None, help=_SEL_HELP),
    gameid: str | None = typer.Argument(None, help=_GAMEID_ARG),
    confirm: bool = typer.Option(
        False, "--confirm", help="Required: deleting a title is destructive and irreversible."
    ),
) -> None:
    """Delete a deployed title from the device (destructive; needs --confirm)."""
    core = make_core(ctx)
    try:
        device, gameid = _gameid_or_shift(sel, gameid)
        if not confirm:
            raise DevkitError(
                ErrorCode.VALIDATION,
                f"refusing to delete {gameid!r} without --confirm",
            )
        result = delete_title(core, resolve_selector(ctx, device), gameid, confirm=True)
    except DevkitError as err:
        die(err, get_options(ctx).json)
    emit(ctx, result.model_dump_json(), result.message or f"{gameid} deleted")


def _launch(
    ctx: typer.Context,
    sel: str | None = typer.Argument(None, help=_SEL_HELP),
    gameid: str | None = typer.Argument(None, help=_GAMEID_ARG),
) -> None:
    """Ask the device's Steam client to run a title."""
    core = make_core(ctx)
    try:
        device, gameid = _gameid_or_shift(sel, gameid)
        launched = launch_title(core, resolve_selector(ctx, device), gameid)
        if not launched.launched:
            raise DevkitError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                f"launch of {gameid} was not confirmed by the device",
            )
    except DevkitError as err:
        die(err, get_options(ctx).json)
    human = f"{launched.gameid} launched (readiness: {launched.readiness})"
    emit(ctx, launched.model_dump_json(), human)


def register(app: typer.Typer) -> None:
    titles_app = typer.Typer(
        name="titles",
        help="List, inspect, configure, register, and delete devkit titles.",
        no_args_is_help=True,
    )
    titles_app.command(name="list")(_list)
    titles_app.command(name="inspect")(_inspect)
    titles_app.command(name="configure")(_configure)
    titles_app.command(name="register")(_register)
    titles_app.command(name="delete")(_delete)
    app.add_typer(titles_app, name="titles")
    app.command(name="launch")(_launch)
