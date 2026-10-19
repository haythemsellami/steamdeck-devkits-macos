"""Diagnostics operations (spec section 8; audit sections 4-5).

Log pulls, screenshots, and controller-config dumps run as jobs and land as
artifacts. Every remote invocation matches the upstream protocol verbatim:
gamescope's xprop screenshot trigger (:1859-1917), ``deckard-capture --json``
on Steam Frame (:1804-1857), the filtered rsync log pulls (:1407-1458), and
``steamos-dump-controller-config`` followed by a ``config_*.vdf`` pull
(:1989-2020).

Artifact granularity: one artifact per requested log kind (steam -> ``logs``,
proton -> ``proton_log``, dumps -> ``dumps``, frametimes -> ``frametimes``) so
``artifacts list --kind`` filtering stays meaningful. Each kind is pulled with
its own rsync invocation into a per-kind staging dir; ``build_rsync_argv``
forces folder-to-folder sources, so a remote source's *contents* land directly
in the staging dir (upstream merges basename subdirs into one folder instead —
equivalent here because kinds stage separately). For the steam kind the
SteamVR log path reported by ``steamos-get-status`` rides along as a second
source, first in the list, matching upstream's priority order (:1422-1426).
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import tempfile
import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from steamos_devkit import constants
from steamos_devkit.artifacts import register_artifact
from steamos_devkit.device import DeviceHandle
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.jobs.progress import parse_progress2_line
from steamos_devkit.models import FilterSpec, JobKind, LogKind, ScreenshotMode
from steamos_devkit.transfer import build_rsync_argv, require_rsync

if TYPE_CHECKING:
    from steamos_devkit.core import DevkitCore
    from steamos_devkit.jobs import JobContext, JobHandle

logger = logging.getLogger(__name__)

_UTILS_DIR = "~/devkit-utils"
"""Remote home of the uploaded device-side helpers (audit section 4)."""

_STATUS_COMMAND = f"python3 {_UTILS_DIR}/steamos-get-status --json"

_STEAM_LOGS_PATH = "/home/{login}/.local/share/Steam/logs"
"""Upstream ``REMOTE_STEAM_LOGS_PATH`` (:132)."""

_DUMPS_PATH = "/tmp/dumps"

_SCREENSHOT_APPEAR_TIMEOUT_S = 10.0
"""Upstream polls 100 x 0.1 s for the gamescope screenshot (:1868-1915)."""
_CONFIG_APPEAR_TIMEOUT_S = 10.0
"""Upstream pulls immediately; we briefly poll because the dump crosses the
Steam pipe asynchronously."""
_POLL_INTERVAL_S = 0.5

_FIND_SCREENSHOT = 'find /tmp -maxdepth 1 -type f -name "gamescope*.png"'
_FIND_CONTROLLER_CONFIG = 'find /tmp -maxdepth 1 -type f -name "config_*.vdf"'

_OUTPUT_TAIL_RECORDS = 25
_OUTPUT_TAIL_CHARS = 500

_LOG_KINDS: frozenset[str] = frozenset({"steam", "proton", "dumps", "frametimes"})
_ARTIFACT_KIND: dict[str, str] = {
    "steam": "logs",
    "proton": "proton_log",
    "dumps": "dumps",
    "frametimes": "frametimes",
}
_GAMEID_RE = re.compile(constants.GAMEID_REGEX)


def _check_gameid(gameid: str) -> None:
    if _GAMEID_RE.fullmatch(gameid) is None:
        raise DevkitError(ErrorCode.VALIDATION, f"invalid gameid: {gameid!r}")


def _require_key(core: DevkitCore) -> Path:
    key_path = core.state.key_path
    if not key_path.exists():
        raise DevkitError(
            ErrorCode.PAIRING_FAILED,
            "no devkit key; run `devkit pair <device>` first",
        )
    return key_path


def _run_pull(ctx: JobContext, argv: Sequence[str], *, phase: str) -> tuple[int, str]:
    """Run one rsync pull under the job context, feeding progress2 records.

    Returns the exit code and an output tail for error messages; the caller
    decides what a nonzero code means (per-kind tolerance vs job failure).
    """
    tail: deque[str] = deque(maxlen=_OUTPUT_TAIL_RECORDS)

    def on_record(record: str) -> None:
        tail.append(record)
        progress = parse_progress2_line(record, phase=phase)
        if progress is None:
            return
        bytes_total = progress.bytes_total
        if progress.bytes_done is not None and bytes_total is not None:
            # progress2 percentages are estimates; clamp to 100%.
            bytes_total = max(bytes_total, progress.bytes_done)
        ctx.progress(
            phase,
            bytes_done=progress.bytes_done,
            bytes_total=bytes_total,
            items_done=progress.items_done,
            rate_bps=progress.rate_bps,
        )

    exit_code = ctx.run_process(argv, on_output=on_record)
    return exit_code, "\n".join(tail)[-_OUTPUT_TAIL_CHARS:]


def _pull_argv(
    core: DevkitCore,
    handle: DeviceHandle,
    *,
    remotes: Sequence[str],
    staging: Path,
    filters: FilterSpec | None = None,
) -> list[str]:
    """Download-direction rsync argv: remote ``login@address:path`` sources."""
    login, address = handle.device.login, handle.device.address
    return build_rsync_argv(
        rsync_path=require_rsync(core),
        ssh_path=core.adapter.ssh(),
        key_path=_require_key(core),
        sources=[Path(f"{login}@{address}:{remote}") for remote in remotes],
        dest=f"{staging}/",
        filters=filters,
        extra_args=["--info=progress2"],
    )


def _fetch_remote_file(
    core: DevkitCore,
    ctx: JobContext,
    handle: DeviceHandle,
    remote_path: str,
    *,
    kind: str,
    media_type: str | None = None,
) -> str:
    """SFTP one remote file into the store and attach it to the job."""
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"devkit-{kind}-"))
    local = tmp_dir / PurePosixPath(remote_path).name
    try:
        ctx.progress("downloading")
        handle.sftp_get(remote_path, local)
        artifact = register_artifact(
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
    core.job_store.attach_artifact(ctx.job_id, artifact.id)
    return f"{artifact.id} ({artifact.bytes} bytes)"


# --- fetch_logs ----------------------------------------------------------------


def _steamvr_logpath(handle: DeviceHandle, base: str, notes: list[str]) -> list[str]:
    """Extra steamvr log source from the status payload; [] when unavailable."""
    try:
        handle.ensure_utils()
        payload = handle.run_json(_STATUS_COMMAND, timeout=constants.TIMEOUT_STATUS_S)
    except DevkitError as e:
        notes.append(f"steam: status probe failed ({e.message}); steamvr logpath skipped")
        return []
    if not isinstance(payload, dict):
        return []
    logpath = payload.get("steamvr_logpath")
    # Upstream prepends it when set and different from the steam logs path
    # (:1423-1426); contents merge into the same staging dir, steamvr first.
    if isinstance(logpath, str) and logpath and logpath != base:
        return [logpath]
    return []


def fetch_logs(
    core: DevkitCore,
    sel: str,
    kinds: Sequence[LogKind] | None = None,
    timeout_s: int = constants.TIMEOUT_FETCH_LOGS_S,
) -> JobHandle:
    """Pull device logs as one job with one artifact per kind (audit :1407-1458).

    Per-kind failures (e.g. a missing ``/tmp/dumps``) are tolerated and noted
    in the job message; the job fails only when *every* requested kind failed.
    """
    requested = list(kinds) if kinds else ["steam"]
    for kind in requested:
        if kind not in _LOG_KINDS:
            valid = ", ".join(sorted(_LOG_KINDS))
            raise DevkitError(ErrorCode.VALIDATION, f"unknown log kind {kind!r}; one of: {valid}")
    kinds_list = list(dict.fromkeys(requested))
    handle = core.device_handle(sel)
    _require_key(core)
    staging_box: list[Path] = []  # created inside the job; shared with cleanup

    def _cleanup(ctx: JobContext) -> None:
        # spec section 7.4: rsync is killed via ctx.run_process; remove the
        # local partial staging dir. No artifact was registered for it.
        if staging_box:
            shutil.rmtree(staging_box[0], ignore_errors=True)

    def _run(ctx: JobContext) -> str:
        staging_root = Path(tempfile.mkdtemp(prefix="devkit-logs-"))
        staging_box.append(staging_root)
        fetched: list[str] = []
        failed: list[str] = []
        notes: list[str] = []
        try:
            ctx.progress("preparing")
            for kind in kinds_list:
                ctx.check_cancelled()
                staging = staging_root / kind
                staging.mkdir()
                phase = f"pulling {kind}"
                filters: FilterSpec | None = None
                if kind == "steam":
                    base = _STEAM_LOGS_PATH.format(login=handle.device.login)
                    remotes = [*_steamvr_logpath(handle, base, notes), base]
                elif kind == "dumps":
                    remotes = [_DUMPS_PATH]
                elif kind == "proton":
                    remotes = [f"/home/{handle.device.login}"]
                    filters = FilterSpec(
                        mode="raw", patterns=["--include=steam-*.log", "--exclude=*"]
                    )
                else:  # frametimes
                    remotes = [f"/home/{handle.device.login}"]
                    filters = FilterSpec(
                        mode="raw", patterns=["--include=mangoapp_*.csv", "--exclude=*"]
                    )
                argv = _pull_argv(core, handle, remotes=remotes, staging=staging, filters=filters)
                ctx.progress(phase)
                exit_code, output_tail = _run_pull(ctx, argv, phase=phase)
                if exit_code != 0:
                    # Missing remote dirs surface as rsync exit 23 link_stat
                    # errors; any per-kind failure is a note, not a job failure.
                    failed.append(f"{kind} (rsync exit {exit_code})")
                    logger.info(
                        "logs pull %r failed for job %s: exit %d: %s",
                        kind,
                        ctx.job_id,
                        exit_code,
                        output_tail,
                    )
                    shutil.rmtree(staging, ignore_errors=True)
                    continue
                artifact = register_artifact(
                    core.state,
                    job_id=ctx.job_id,
                    device=handle.device.name,
                    kind=_ARTIFACT_KIND[kind],
                    source=staging,
                    remote_source=" ".join(remotes),
                )
                core.job_store.attach_artifact(ctx.job_id, artifact.id)
                fetched.append(f"{kind} ({artifact.id})")
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
        if not fetched:
            raise DevkitError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                f"every requested log pull failed on {handle.device.name}: " + ", ".join(failed),
                details={"failed": failed},
            )
        message = "logs fetched: " + ", ".join(fetched)
        if failed:
            message += "; skipped: " + ", ".join(failed)
        if notes:
            message += " (" + "; ".join(notes) + ")"
        return message

    return core.job_runner.submit(
        _run,
        kind=JobKind.FETCH_LOGS,
        device=handle.device.name,
        spec={"kinds": kinds_list},
        timeout_s=timeout_s,
        cleanup=_cleanup,
    )


# --- screenshot ------------------------------------------------------------------


def _is_deckard(handle: DeviceHandle) -> bool:
    """Device family detection via steamos-get-status (audit :1943)."""
    handle.ensure_utils()
    payload = handle.run_json(_STATUS_COMMAND, timeout=constants.TIMEOUT_STATUS_S)
    if not isinstance(payload, dict):
        raise DevkitError(
            ErrorCode.REMOTE_COMMAND_FAILED,
            "steamos-get-status returned a non-object JSON payload",
        )
    return payload.get("is_deckard") is True


def _screenshot_frame(core: DevkitCore, ctx: JobContext, handle: DeviceHandle) -> str:
    """``deckard-capture --json`` flow (audit :1804-1857)."""
    ctx.progress("capturing")
    result = handle.run(f"python3 {_UTILS_DIR}/deckard-capture --json", check=False)
    if result.exit_code != 0:
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            parsed = None
        error = parsed.get("error", result.stdout) if isinstance(parsed, dict) else result.stdout
        raise DevkitError(
            ErrorCode.REMOTE_COMMAND_FAILED,
            f"Steam Frame capture failed: {error}",
            remote_exit_code=result.exit_code,
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise DevkitError(
            ErrorCode.REMOTE_COMMAND_FAILED, "failed to parse deckard-capture output"
        ) from e
    if not isinstance(payload, dict) or not payload.get("success", False):
        error = (
            payload.get("error", "Unknown error") if isinstance(payload, dict) else result.stdout
        )
        raise DevkitError(ErrorCode.REMOTE_COMMAND_FAILED, f"Steam Frame capture failed: {error}")
    remote_path = payload.get("output")
    if not isinstance(remote_path, str) or not remote_path:
        raise DevkitError(
            ErrorCode.REMOTE_COMMAND_FAILED, "deckard-capture returned no output path"
        )
    description = _fetch_remote_file(
        core, ctx, handle, remote_path, kind="screenshot", media_type="image/png"
    )
    # keep the device tidy on success, exactly as upstream (:1853)
    handle.run(f"rm {shlex.quote(remote_path)}", check=False)
    return f"screenshot captured: {description}"


def _screenshot_deck(
    core: DevkitCore, ctx: JobContext, handle: DeviceHandle, mode: ScreenshotMode
) -> str:
    """Gamescope xprop trigger plus ``/tmp/gamescope*.png`` poll (:1859-1917)."""
    # Wipe first: screenshots must not accumulate in tmpfs, and the poll below
    # can then confirm a *new* screenshot was delivered.
    handle.run("rm -f /tmp/gamescope*.png", check=False)
    ctx.progress("capturing")
    handle.run(
        "DISPLAY=:0 xprop -root -f GAMESCOPECTRL_DEBUG_REQUEST_SCREENSHOT 32c "
        f"-set GAMESCOPECTRL_DEBUG_REQUEST_SCREENSHOT {mode}"
    )
    deadline = time.monotonic() + _SCREENSHOT_APPEAR_TIMEOUT_S
    remote_path: str | None = None
    while remote_path is None:
        ctx.check_cancelled()
        found = handle.run(_FIND_SCREENSHOT, check=False)
        candidates = [line.strip() for line in found.stdout.splitlines() if line.strip()]
        if found.exit_code == 0 and candidates:
            # Gamescope's timestamped names sort chronologically; the max is
            # the newest (matters for mode 2, which writes one file per layer).
            remote_path = max(candidates)
            break
        if time.monotonic() >= deadline:
            raise DevkitError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                "timeout waiting for gamescope screenshot on the device",
            )
        ctx.sleep(_POLL_INTERVAL_S)
    description = _fetch_remote_file(
        core, ctx, handle, remote_path, kind="screenshot", media_type="image/png"
    )
    handle.run(f"rm -f {shlex.quote(remote_path)}", check=False)
    return f"screenshot captured: {description}"


def screenshot(
    core: DevkitCore,
    sel: str,
    mode: ScreenshotMode = 3,
    timeout_s: int = constants.TIMEOUT_SCREENSHOT_S,
) -> JobHandle:
    """Capture a screenshot (audit section 4): deckard-capture on Steam Frame,
    the gamescope xprop trigger on Deck."""
    if mode not in constants.SCREENSHOT_MODES:
        raise DevkitError(
            ErrorCode.VALIDATION,
            f"unknown screenshot mode {mode!r}; one of: 1, 2, 3, 4",
        )
    handle = core.device_handle(sel)

    def _cleanup(ctx: JobContext) -> None:
        # spec section 7.4: wipe any partial gamescope screenshot remotely
        try:
            handle.run("rm -f /tmp/gamescope*.png", check=False)
        except Exception:
            logger.warning("screenshot cleanup failed for job %s", ctx.job_id, exc_info=True)

    def _run(ctx: JobContext) -> str:
        ctx.progress("preparing")
        if _is_deckard(handle):
            return _screenshot_frame(core, ctx, handle)
        return _screenshot_deck(core, ctx, handle, mode)

    return core.job_runner.submit(
        _run,
        kind=JobKind.SCREENSHOT,
        device=handle.device.name,
        spec={"mode": mode},
        timeout_s=timeout_s,
        cleanup=_cleanup,
    )


# --- controller_config ------------------------------------------------------------


def controller_config(
    core: DevkitCore,
    sel: str,
    gameid: str,
    timeout_s: int = constants.TIMEOUT_CONTROLLER_CONFIG_S,
) -> JobHandle:
    """Dump a title's controller configuration via the Steam pipe (:1989-2020)."""
    _check_gameid(gameid)
    handle = core.device_handle(sel)
    _require_key(core)
    staging_box: list[Path] = []

    def _cleanup(ctx: JobContext) -> None:
        # spec section 7.4: wipe the temp/dump files remotely; drop staging
        try:
            handle.run("rm -f /tmp/config_*.tmp /tmp/config_*.vdf", check=False)
        except Exception:
            logger.warning("controller_config cleanup failed for job %s", ctx.job_id, exc_info=True)
        if staging_box:
            shutil.rmtree(staging_box[0], ignore_errors=True)

    def _run(ctx: JobContext) -> str:
        ctx.progress("preparing")
        handle.ensure_utils()
        # wipe old temp data first, exactly as upstream (:1992)
        handle.run("rm -f /tmp/config_*.tmp", check=False)

        ctx.progress("dumping")
        result = handle.run(
            f"python3 {_UTILS_DIR}/steamos-dump-controller-config --gameid {shlex.quote(gameid)}"
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise DevkitError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                "steamos-dump-controller-config did not return JSON",
                details={"stdout_head": result.stdout[:500]},
            ) from e
        if not isinstance(payload, dict):
            raise DevkitError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                "unexpected steamos-dump-controller-config response",
                details={"stdout_head": result.stdout[:500]},
            )
        if payload.get("error"):
            raise DevkitError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                f"controller config dump failed: {payload['error']}",
                details={"device_error": str(payload["error"])},
            )

        ctx.progress("waiting")
        deadline = time.monotonic() + _CONFIG_APPEAR_TIMEOUT_S
        while True:
            ctx.check_cancelled()
            found = handle.run(_FIND_CONTROLLER_CONFIG, check=False)
            if found.exit_code == 0 and found.stdout.strip():
                break
            if time.monotonic() >= deadline:
                raise DevkitError(
                    ErrorCode.REMOTE_COMMAND_FAILED,
                    f"timeout waiting for /tmp/config_*.vdf for {gameid}",
                )
            ctx.sleep(_POLL_INTERVAL_S)

        staging = Path(tempfile.mkdtemp(prefix="devkit-controller-config-"))
        staging_box.append(staging)
        try:
            ctx.progress("pulling")
            argv = _pull_argv(
                core,
                handle,
                remotes=["/tmp"],
                staging=staging,
                filters=FilterSpec(mode="raw", patterns=["--include=config_*.vdf", "--exclude=*"]),
            )
            exit_code, output_tail = _run_pull(ctx, argv, phase="pulling")
            if exit_code != 0:
                raise DevkitError(
                    ErrorCode.REMOTE_COMMAND_FAILED,
                    f"rsync exited {exit_code}",
                    details={
                        "rsync_exit_code": exit_code,
                        "output_tail": output_tail,
                    },
                    remote_exit_code=exit_code,
                )
            if not any(staging.glob("config_*.vdf")):
                raise DevkitError(
                    ErrorCode.REMOTE_COMMAND_FAILED,
                    f"the device produced no config_*.vdf for {gameid}",
                )
            artifact = register_artifact(
                core.state,
                job_id=ctx.job_id,
                device=handle.device.name,
                kind="controller_config",
                source=staging,
                remote_source="/tmp/config_*.vdf",
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        core.job_store.attach_artifact(ctx.job_id, artifact.id)
        success = str(payload.get("success") or f"controller config dumped for {gameid}")
        return f"{success}: {artifact.id} ({artifact.bytes} bytes)"

    return core.job_runner.submit(
        _run,
        kind=JobKind.CONTROLLER_CONFIG,
        device=handle.device.name,
        gameid=gameid,
        timeout_s=timeout_s,
        cleanup=_cleanup,
    )


__all__ = ["controller_config", "fetch_logs", "screenshot"]
