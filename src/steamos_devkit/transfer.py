"""rsync upload machinery: argv builder, dry-run preview, job-context upload.

Flag semantics follow upstream ``rsync_transfer`` (``:815-902``); on top of
those we add ``--info=progress2 --partial`` for progress reporting and
resumable uploads (audit section 7 gaps). Preview is ``rsync -n
--itemize-changes --stats`` only — it never calls ``steamos-prepare-upload``
and never mutates device state beyond the read-only SSH connection rsync
itself opens (spec section 8).
"""

from __future__ import annotations

import shlex
import subprocess
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from steamos_devkit import constants
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.jobs.progress import (
    parse_itemize_line,
    parse_progress2_line,
    parse_stats_transferred_bytes,
)
from steamos_devkit.models import DeployPreview, DeploySpec, FilterSpec

if TYPE_CHECKING:
    from steamos_devkit.core import DevkitCore
    from steamos_devkit.device import DeviceHandle
    from steamos_devkit.jobs import JobContext

RSYNC_CHMOD: str = "Du=rwx,Dgo=rx,Fu=rwx,Fog=rx"
"""Upstream's forced permission mask (``:849``)."""

MAX_SAMPLE_CHANGES: int = 200
"""DeployPreview.sample_changes cap (spec section 5)."""

_OUTPUT_TAIL_RECORDS: int = 25
_OUTPUT_TAIL_CHARS: int = 500


def build_rsync_argv(
    *,
    rsync_path: Path,
    ssh_path: Path,
    key_path: Path,
    sources: Sequence[Path],
    dest: str,
    clean: bool = False,
    checksum: bool = False,
    update: bool = False,
    compress: bool = True,
    filters: FilterSpec | None = None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Build one rsync argv with upstream's flag semantics (``:843-879``).

    Every source is forced folder-to-folder with a trailing slash, exactly
    like upstream (``:870-877``). ``filters`` maps to rsync filter rules:
    ``exclude`` emits ``--exclude=<pattern>`` per pattern; ``include`` emits
    ``--include=<pattern>`` per pattern followed by ``--include=*/``,
    ``--exclude=*`` and ``--prune-empty-dirs`` (the classic include-only
    idiom); ``raw`` passes patterns through verbatim as rsync arguments.
    """
    if checksum and update:
        raise DevkitError(
            ErrorCode.VALIDATION, "rsync --checksum and --update are mutually exclusive"
        )
    if not sources:
        raise DevkitError(ErrorCode.VALIDATION, "at least one source path is required")
    rsh = (
        f"{shlex.quote(str(ssh_path))} -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null -i {shlex.quote(str(key_path))}"
    )
    argv = [str(rsync_path), "-av"]
    if compress:
        argv.append("-z")
    argv += [f"--chmod={RSYNC_CHMOD}", "-e", rsh]
    if clean:
        argv += ["--delete", "--delete-excluded", "--delete-delay"]
    if update:
        argv.append("--update")
    if checksum:
        argv.append("--checksum")
    argv += _filter_args(filters)
    argv += list(extra_args)
    argv += [f"{str(source).rstrip('/')}/" for source in sources]
    argv.append(dest)
    return argv


def _filter_args(filters: FilterSpec | None) -> list[str]:
    if filters is None or not filters.patterns:
        return []
    if filters.mode == "exclude":
        return [f"--exclude={pattern}" for pattern in filters.patterns]
    if filters.mode == "include":
        args = [f"--include={pattern}" for pattern in filters.patterns]
        args += ["--include=*/", "--exclude=*", "--prune-empty-dirs"]
        return args
    return list(filters.patterns)  # raw: patterns already are rsync arguments


def validate_deploy_spec(spec: DeploySpec) -> None:
    """Synchronous deploy validation, before any job or device contact."""
    if spec.gameid in constants.RESERVED_GAMEIDS:
        raise DevkitError(
            ErrorCode.UNSUPPORTED,
            f"{spec.gameid!r} is a reserved Valve-only gameid (spec section 4)",
            details={"gameid": spec.gameid},
        )
    if not spec.source_dir.is_dir():
        raise DevkitError(
            ErrorCode.VALIDATION,
            f"source directory does not exist: {spec.source_dir}",
            details={"source_dir": str(spec.source_dir)},
        )


def require_rsync(core: DevkitCore) -> Path:
    """The probed rsync 3.x path, or the matching dependency error."""
    info = core.adapter.rsync()
    if info.ok and info.path is not None:
        return info.path
    if info.flavor == "openrsync":
        raise DevkitError(
            ErrorCode.OPENRSYNC_DETECTED,
            info.message or "openrsync is not supported; install rsync 3.x",
        )
    raise DevkitError(ErrorCode.DEPENDENCY_MISSING, info.message or "rsync 3.x not found")


def parse_preview_output(text: str) -> DeployPreview:
    """Turn ``rsync -n --itemize-changes --stats`` output into a DeployPreview.

    Counts cover files only (directory creation/removal lines appear in
    ``sample_changes`` but not in the counts); the byte figure comes from
    ``Total transferred file size`` and stays 0 with a warning when rsync did
    not emit stats.
    """
    added = changed = deleted = 0
    entries = 0
    sample: list[str] = []
    for line in text.splitlines():
        entry = parse_itemize_line(line)
        if entry is None:
            continue
        entries += 1
        if len(sample) < MAX_SAMPLE_CHANGES:
            sample.append(line)
        if entry.is_dir or entry.path.endswith("/"):
            continue
        if entry.change == "added":
            added += 1
        elif entry.change == "changed":
            changed += 1
        else:
            deleted += 1
    warnings: list[str] = []
    transferred = parse_stats_transferred_bytes(text)
    if transferred is None:
        warnings.append(
            "rsync did not report 'Total transferred file size'; bytes_to_transfer is 0"
        )
        transferred = 0
    if entries > MAX_SAMPLE_CHANGES:
        warnings.append(f"sample truncated to {MAX_SAMPLE_CHANGES} of {entries} changes")
    return DeployPreview(
        files_added=added,
        files_changed=changed,
        files_deleted=deleted,
        bytes_to_transfer=transferred,
        sample_changes=sample,
        warnings=warnings,
    )


def preview_deploy(core: DevkitCore, selector: str, spec: DeploySpec) -> DeployPreview:
    """Dry-run a deploy against the device (spec section 8).

    Runs ``rsync -n --itemize-changes --stats`` as a plain subprocess — never
    a job — and touches nothing on the device beyond the read-only SSH
    connection rsync opens; the remote directory is assumed to be
    ``~/devkit-game/<gameid>/``.
    """
    validate_deploy_spec(spec)
    device = core.resolve_device(selector)
    key_path = core.state.key_path
    if not key_path.exists():
        raise DevkitError(
            ErrorCode.PAIRING_FAILED,
            "no devkit key; run `devkit pair <device>` first",
        )
    argv = build_rsync_argv(
        rsync_path=require_rsync(core),
        ssh_path=core.adapter.ssh(),
        key_path=key_path,
        sources=[spec.source_dir],
        dest=f"{device.login}@{device.address}:{constants.device_title_dir(spec.gameid)}/",
        clean=spec.clean,
        checksum=spec.checksum,
        update=spec.update,
        compress=spec.compress,
        filters=spec.filters,
        extra_args=["-n", "--itemize-changes", "--stats"],
    )
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=constants.TIMEOUT_DEPLOY_S,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise DevkitError(
            ErrorCode.TIMEOUT, f"rsync dry-run exceeded {constants.TIMEOUT_DEPLOY_S}s"
        ) from e
    except OSError as e:
        raise DevkitError(ErrorCode.DEPENDENCY_MISSING, f"cannot run rsync: {e}") from e
    if proc.returncode != 0:
        raise DevkitError(
            ErrorCode.REMOTE_COMMAND_FAILED,
            f"rsync dry-run failed ({proc.returncode})",
            details={"stderr_tail": proc.stderr[-_OUTPUT_TAIL_CHARS:]},
            remote_exit_code=proc.returncode,
        )
    return parse_preview_output(proc.stdout)


def upload_tree(ctx: JobContext, handle: DeviceHandle, spec: DeploySpec, remote_dir: str) -> None:
    """Run the deploy rsync under the job context, reporting progress2 records.

    ``--partial`` keeps remote partial files so a later run resumes (spec
    section 7.4); cancellation kills the rsync process group via
    ``ctx.run_process``. Any nonzero exit — including the partial-transfer
    codes 23/24 — fails the job with REMOTE_COMMAND_FAILED.
    """
    core = handle.core  # the handle's owning core (state root, adapter)
    argv = build_rsync_argv(
        rsync_path=require_rsync(core),
        ssh_path=core.adapter.ssh(),
        key_path=core.state.key_path,
        sources=[spec.source_dir],
        dest=f"{handle.device.login}@{handle.device.address}:{remote_dir.rstrip('/')}/",
        clean=spec.clean,
        checksum=spec.checksum,
        update=spec.update,
        compress=spec.compress,
        filters=spec.filters,
        extra_args=["--info=progress2", "--partial"],
    )
    tail: deque[str] = deque(maxlen=_OUTPUT_TAIL_RECORDS)

    def on_record(record: str) -> None:
        tail.append(record)
        progress = parse_progress2_line(record, phase="transferring")
        if progress is None:
            return
        bytes_total = progress.bytes_total
        if progress.bytes_done is not None and bytes_total is not None:
            # progress2 percentages are estimates; clamp so a partial or
            # re-sent transfer never reports more than 100%.
            bytes_total = max(bytes_total, progress.bytes_done)
        ctx.progress(
            "transferring",
            bytes_done=progress.bytes_done,
            bytes_total=bytes_total,
            items_done=progress.items_done,
            rate_bps=progress.rate_bps,
        )

    exit_code = ctx.run_process(argv, on_output=on_record)
    if exit_code != 0:
        raise DevkitError(
            ErrorCode.REMOTE_COMMAND_FAILED,
            f"rsync exited {exit_code}",
            details={
                "rsync_exit_code": exit_code,
                "output_tail": "\n".join(tail)[-_OUTPUT_TAIL_CHARS:],
            },
            remote_exit_code=exit_code,
        )


__all__ = [
    "MAX_SAMPLE_CHANGES",
    "RSYNC_CHMOD",
    "build_rsync_argv",
    "parse_preview_output",
    "preview_deploy",
    "require_rsync",
    "upload_tree",
    "validate_deploy_spec",
]
