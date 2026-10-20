"""Title operations and the deploy pipeline job (spec sections 4, 5, 8).

Every operation opens with ``handle.ensure_utils()`` so the device-side
helpers are current (audit section 1: upstream rsyncs ``devkit-utils`` on
every identify), then shells out to the verbatim helpers under
``~/devkit-utils`` (audit section 4). Remote command paths are built with the
tilde unquoted (a quoted ``~`` would not expand) around a regex-validated,
``shlex.quote``-d gameid; SFTP paths skip the shell entirely and use the
absolute ``/home/<login>/devkit-game/...`` form like upstream ``:1357``.
"""

from __future__ import annotations

import json
import re
import shlex
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, cast

import paramiko
from pydantic import ValidationError

from steamos_devkit import constants
from steamos_devkit.config import TitleState
from steamos_devkit.constants import CompatToolAlias
from steamos_devkit.device import DeviceHandle
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import (
    DeploySpec,
    JobKind,
    LaunchResult,
    Result,
    Title,
    TitleConfig,
    TitleDetail,
)
from steamos_devkit.transfer import upload_tree, validate_deploy_spec

if TYPE_CHECKING:
    from steamos_devkit.core import DevkitCore
    from steamos_devkit.jobs import JobContext, JobHandle

_UTILS_DIR = "~/devkit-utils"
"""Remote home of the uploaded device-side helpers (audit section 4)."""

_GAMEID_RE = re.compile(constants.GAMEID_REGEX)


def _check_gameid(gameid: str) -> None:
    """Reject anything outside the upstream gameid regex (spec section 4)."""
    if _GAMEID_RE.fullmatch(gameid) is None:
        raise DevkitError(ErrorCode.VALIDATION, f"invalid gameid: {gameid!r}")


def _sidecar_command_path(gameid: str, suffix: str) -> str:
    """Shell-side sidecar path; the leading ``~`` must stay unquoted."""
    return f"{constants.DEVICE_TITLE_ROOT}/{shlex.quote(gameid + suffix)}"


def _sidecar_sftp_path(handle: DeviceHandle, gameid: str, suffix: str) -> str:
    """Absolute SFTP sidecar path (SFTP does not expand ``~``)."""
    home_relative = constants.DEVICE_TITLE_ROOT.removeprefix("~")
    return f"/home/{handle.device.login}{home_relative}/{gameid}{suffix}"


def _read_json_sidecar(handle: DeviceHandle, gameid: str, suffix: str) -> Any | None:
    """``cat`` one sidecar; None when absent, DevkitError when malformed."""
    path = _sidecar_command_path(gameid, suffix)
    result = handle.run(f"cat {path}", check=False)
    if result.exit_code != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise DevkitError(
            ErrorCode.REMOTE_COMMAND_FAILED,
            f"malformed JSON in {path}: {e}",
            details={"path": path},
        ) from e


def _config_from_sidecars(gameid: str, argv: Any, env: Any, settings: Any) -> TitleConfig:
    entrypoint: str | None = None
    args: list[str] = []
    if argv is not None:
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise DevkitError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                f"argv sidecar for {gameid!r} is not a JSON string list",
            )
        if argv:
            entrypoint, args = argv[0], list(argv[1:])
    env_dict = _str_dict(env, "env", gameid)
    settings_dict = _str_dict(settings, "settings", gameid)
    # runtime is the typed view of settings["compat_tool"]; the raw key stays
    # in settings so unknown (non-alias) values round-trip losslessly.
    compat_tool = settings_dict.get("compat_tool")
    runtime = (
        cast("CompatToolAlias", compat_tool)
        if compat_tool in constants.COMPAT_TOOL_ALIASES
        else None
    )
    return TitleConfig(
        entrypoint=entrypoint, args=args, env=env_dict, runtime=runtime, settings=settings_dict
    )


def _str_dict(value: Any, sidecar: str, gameid: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise DevkitError(
            ErrorCode.REMOTE_COMMAND_FAILED,
            f"{sidecar} sidecar for {gameid!r} is not a JSON string dict",
        )
    return dict(value)


def _validate_entrypoint(entrypoint: str | None) -> None:
    """Entrypoints are relative to the game dir: no leading ``/``, no ``..``."""
    if entrypoint is None:
        return
    if not entrypoint.strip() or entrypoint.startswith("/"):
        raise DevkitError(
            ErrorCode.VALIDATION,
            "entrypoint must be a relative path inside the game directory "
            "(no leading '/', no '..' components)",
            details={"entrypoint": entrypoint},
        )
    if ".." in PurePosixPath(entrypoint).parts:
        raise DevkitError(
            ErrorCode.VALIDATION,
            "entrypoint must not contain '..' components",
            details={"entrypoint": entrypoint},
        )


def _write_config_sidecars(handle: DeviceHandle, gameid: str, config: TitleConfig) -> None:
    """Write sidecars mirroring the device-helper semantics (audit section 1b):

    - argv: written only when an entrypoint or extra args are supplied
      (helper ``save_argv(None)`` skips); ``entrypoint=None`` preserves the
      device's current argv[0], and supplied args replace the tail.
    - env: written only when non-empty (helper ``save_env`` skips empty),
      replacing wholesale when written.
    - settings: merged over the device's current settings (helper
      ``save_settings`` merges); ``runtime`` sets ``compat_tool``.

    Every frontend gets the same behavior because the merge lives here, not
    in the CLI/MCP layers — a call that sets only ``env`` can never clobber
    the device's entrypoint or settings.
    """
    argv_suffix = constants.ARGV_SIDECAR_SUFFIX
    env_suffix = constants.ENV_SIDECAR_SUFFIX
    settings_suffix = constants.SETTINGS_SIDECAR_SUFFIX

    payloads: dict[str, object] = {}

    if config.entrypoint is not None or config.args:
        current_argv = _read_json_sidecar(handle, gameid, argv_suffix)
        device_argv = current_argv if isinstance(current_argv, list) else []
        if config.entrypoint is not None:
            head: str | None = config.entrypoint
            tail: list[str] = list(config.args)
        else:
            head = device_argv[0] if device_argv and isinstance(device_argv[0], str) else None
            tail = list(config.args)
        payloads[argv_suffix] = ([head] if head is not None else []) + tail

    if config.env:
        payloads[env_suffix] = dict(config.env)

    if config.settings or config.runtime is not None:
        current_settings = _read_json_sidecar(handle, gameid, settings_suffix)
        merged: dict[str, str] = {}
        if isinstance(current_settings, dict):
            merged.update(
                {
                    k: v
                    for k, v in current_settings.items()
                    if isinstance(k, str) and isinstance(v, str)
                }
            )
        merged.update(config.settings)
        if config.runtime is not None:
            merged["compat_tool"] = config.runtime
        payloads[settings_suffix] = merged

    with tempfile.TemporaryDirectory(prefix="devkit-title-") as tmp:
        for suffix, payload in payloads.items():
            local = Path(tmp) / f"{gameid}{suffix}"
            local.write_text(json.dumps(payload), encoding="utf-8")
            try:
                handle.sftp_put(local, _sidecar_sftp_path(handle, gameid, suffix))
            except (OSError, paramiko.SSHException) as e:
                raise DevkitError(
                    ErrorCode.REMOTE_COMMAND_FAILED,
                    f"sidecar upload failed for {gameid}{suffix}: {e}",
                ) from e


def _persist_title_state(
    core: DevkitCore,
    gameid: str,
    *,
    config: TitleConfig | None = None,
    source_dir: Path | None = None,
) -> None:
    state = core.state.load_title(gameid)
    if state is None:
        state = TitleState(gameid=gameid)
    if config is not None:
        state.config = config
    if source_dir is not None:
        state.source_dir = source_dir
    core.state.save_title(state)


def _register_payload(gameid: str, directory: str, config: TitleConfig) -> dict[str, Any]:
    """The ``--parms`` JSON for steam-client-create-shortcut.

    ``force_appid`` and ``lepton_args`` ride along (empty) because the
    device-side script reads them unconditionally, as does
    ``settings["compat_tool"]`` — upstream sends ``""`` when the runtime is
    unspecified (``RUNTIME_ALIASES.get(..., '')``, gui2.py:300-301).
    ``argv: None`` makes the device keep its existing argv sidecar
    (``save_argv`` skips None), so registering without a local config never
    clobbers the on-device entrypoint.
    """
    argv: list[str] | None = None
    if config.entrypoint is not None:
        argv = [config.entrypoint, *config.args]
    elif config.args:
        argv = list(config.args)
    settings = dict(config.settings)
    settings["compat_tool"] = config.runtime or settings.get("compat_tool", "")
    return {
        "gameid": gameid,
        "directory": directory,
        "argv": argv,
        "env": dict(config.env),
        "settings": settings,
        "force_appid": "",
        "lepton_args": "",
    }


def _register_title(handle: DeviceHandle, gameid: str, directory: str, config: TitleConfig) -> str:
    """Run steam-client-create-shortcut; returns the device success text."""
    payload = _register_payload(gameid, directory, config)
    command = (
        f"python3 {_UTILS_DIR}/steam-client-create-shortcut "
        f"--parms {shlex.quote(json.dumps(payload))}"
    )
    result = handle.run(command)
    try:
        response: Any = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise DevkitError(
            ErrorCode.REMOTE_COMMAND_FAILED,
            "steam-client-create-shortcut did not return JSON",
            details={"stdout_head": result.stdout[:500]},
        ) from e
    if not isinstance(response, dict):
        raise DevkitError(
            ErrorCode.REMOTE_COMMAND_FAILED,
            "unexpected steam-client-create-shortcut response",
            details={"stdout_head": result.stdout[:500]},
        )
    error = response.get("error")
    if error:
        raise DevkitError(
            ErrorCode.REMOTE_COMMAND_FAILED,
            f"title registration failed: {error}",
            details={"device_error": str(error)},
        )
    return str(response.get("success") or f"{gameid} registered")


def _launch_title(handle: DeviceHandle, gameid: str) -> LaunchResult:
    """``steam-devkit-rpc run-game``; readiness honestly stays ``unknown``."""
    result = handle.run(
        f"python3 {_UTILS_DIR}/steam-devkit-rpc run-game gameid={shlex.quote(gameid)}",
        check=False,
    )
    combined = (result.stdout + "\n" + result.stderr).strip()
    # The device helper logs "success" (stderr) and writes the Steam pipe's
    # success response (stdout) only on the happy path; its timeout path exits
    # 0 without either, so the text check is load-bearing.
    launched = result.exit_code == 0 and "success" in combined.lower()
    return LaunchResult(gameid=gameid, launched=launched, device_response=combined)


def _stored_config(core: DevkitCore, gameid: str) -> TitleConfig:
    state = core.state.load_title(gameid)
    return state.config if state is not None else TitleConfig()


def list_titles(core: DevkitCore, selector: str) -> list[Title]:
    """``steamos-list-games``; listed titles are registered with Steam."""
    handle = core.device_handle(selector)
    handle.ensure_utils()
    data = handle.run_json(f"python3 {_UTILS_DIR}/steamos-list-games")
    if not isinstance(data, list):
        raise DevkitError(
            ErrorCode.REMOTE_COMMAND_FAILED, "steamos-list-games did not return a JSON array"
        )
    titles: list[Title] = []
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get("gameid"), str):
            raise DevkitError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                f"malformed title entry from device: {entry!r}",
            )
        try:
            titles.append(Title(gameid=entry["gameid"], registered_with_steam=True))
        except ValidationError as e:
            raise DevkitError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                f"malformed gameid from device: {entry['gameid']!r}",
            ) from e
    return titles


def inspect_title(core: DevkitCore, selector: str, gameid: str) -> TitleDetail:
    """A title plus its on-device argv/env/settings sidecars (missing = defaults)."""
    _check_gameid(gameid)
    handle = core.device_handle(selector)
    handle.ensure_utils()
    argv = _read_json_sidecar(handle, gameid, constants.ARGV_SIDECAR_SUFFIX)
    env = _read_json_sidecar(handle, gameid, constants.ENV_SIDECAR_SUFFIX)
    settings = _read_json_sidecar(handle, gameid, constants.SETTINGS_SIDECAR_SUFFIX)
    return TitleDetail(
        gameid=gameid,
        registered_with_steam=True,  # sidecars under ~/devkit-game imply a deployed title
        config=_config_from_sidecars(gameid, argv, env, settings),
        remote_dir=constants.device_title_dir(gameid),
    )


def configure_title(core: DevkitCore, selector: str, gameid: str, config: TitleConfig) -> Result:
    """Write the argv/env/settings sidecars and persist the config locally."""
    _check_gameid(gameid)
    _validate_entrypoint(config.entrypoint)
    handle = core.device_handle(selector)
    handle.ensure_utils()
    _write_config_sidecars(handle, gameid, config)
    _persist_title_state(core, gameid, config=config)
    return Result.success(device=handle.device, message=f"title {gameid} configured")


def register_title(core: DevkitCore, selector: str, gameid: str) -> Result:
    """Register the title with the device Steam client (audit section 3 step 3)."""
    _check_gameid(gameid)
    handle = core.device_handle(selector)
    handle.ensure_utils()
    config = _stored_config(core, gameid)
    directory = (
        f"/home/{handle.device.login}{constants.DEVICE_TITLE_ROOT.removeprefix('~')}/{gameid}"
    )
    message = _register_title(handle, gameid, directory, config)
    return Result(ok=True, device=handle.device, message=message, registration_complete=True)


def launch_title(core: DevkitCore, selector: str, gameid: str) -> LaunchResult:
    """Ask Steam to run the title; readiness is never inferable (audit section 7)."""
    _check_gameid(gameid)
    handle = core.device_handle(selector)
    handle.ensure_utils()
    return _launch_title(handle, gameid)


def delete_title(core: DevkitCore, selector: str, gameid: str, *, confirm: Literal[True]) -> Result:
    """Delete the title on the device. ``confirm=True`` is the static guard —
    the CLI refuses to call this without an explicit ``--confirm`` flag. The
    local ``titles/<gameid>.json`` is device-agnostic and is left alone."""
    _check_gameid(gameid)
    handle = core.device_handle(selector)
    handle.ensure_utils()
    result = handle.run(f"python3 {_UTILS_DIR}/steamos-delete --delete-title {shlex.quote(gameid)}")
    return Result.success(
        device=handle.device, message=result.stdout.strip() or f"title {gameid} deleted"
    )


def deploy(
    core: DevkitCore,
    selector: str,
    spec: DeploySpec,
    *,
    timeout_s: int | None = None,
) -> JobHandle:
    """Submit the deploy pipeline as a job (audit section 3, spec section 7.3).

    Phases: preparing (steamos-prepare-upload) -> transferring (rsync with
    progress2) -> registering (steam-client-create-shortcut, when
    ``spec.register``) -> launching (steam-devkit-rpc run-game, when
    ``spec.launch``). Validation failures raise before the job exists; remote
    failures land in the journal as FAILED. No cleanup hook is needed:
    ``ctx.run_process`` kills the rsync process group and ``--partial`` keeps
    remote partials for resume (spec section 7.4).
    """
    validate_deploy_spec(spec)
    device = core.resolve_device(selector)

    def _run(ctx: JobContext) -> str:
        handle = DeviceHandle(core, device)
        handle.ensure_utils()
        ctx.progress("preparing")
        prepared = handle.run_json(
            f"python3 {_UTILS_DIR}/steamos-prepare-upload --gameid {shlex.quote(spec.gameid)}"
        )
        if not isinstance(prepared, dict) or not isinstance(prepared.get("directory"), str):
            raise DevkitError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                "steamos-prepare-upload did not return a directory",
            )
        remote_dir = prepared["directory"]
        ctx.check_cancelled()

        ctx.progress("transferring")
        upload_tree(ctx, handle, spec, remote_dir)
        outcomes = ["upload complete"]

        if spec.config is not None:
            _validate_entrypoint(spec.config.entrypoint)
            _write_config_sidecars(handle, spec.gameid, spec.config)
            outcomes.append("config applied")
        _persist_title_state(
            core,
            spec.gameid,
            config=spec.config,
            source_dir=spec.source_dir,
        )

        if spec.register:
            ctx.check_cancelled()
            ctx.progress("registering")
            config = spec.config if spec.config is not None else _stored_config(core, spec.gameid)
            _register_title(handle, spec.gameid, remote_dir, config)
            outcomes.append("registered with Steam")

        if spec.launch:
            ctx.check_cancelled()
            ctx.progress("launching")
            launch = _launch_title(handle, spec.gameid)
            if not launch.launched:
                raise DevkitError(
                    ErrorCode.REMOTE_COMMAND_FAILED,
                    f"launch failed: {launch.device_response}",
                    details={"device_response": launch.device_response},
                )
            outcomes.append("launch requested")

        return "deploy complete: " + ", ".join(outcomes)

    return core.job_runner.submit(
        _run,
        kind=JobKind.DEPLOY,
        device=device.name,
        gameid=spec.gameid,
        spec=spec.model_dump(mode="json"),
        timeout_s=timeout_s if timeout_s is not None else constants.TIMEOUT_DEPLOY_S,
    )


__all__ = [
    "configure_title",
    "delete_title",
    "deploy",
    "inspect_title",
    "launch_title",
    "list_titles",
    "register_title",
]
