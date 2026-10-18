"""Linux host adapter (spec section 12).

Best-effort port preserving upstream behaviors: rsync from PATH (flavor-checked
the same way as macOS), system ssh, POSIX key permissions, and upstream's
konsole/gnome-terminal/xterm terminal search list.
"""

import os
import subprocess
from pathlib import Path

from steamos_devkit import constants
from steamos_devkit.adapters.base import (
    detect_context,
    path_scan,
    probe_rsync,
    probe_rsync_candidates,
    ssh_version,
    which_path,
)
from steamos_devkit.config import platform_default_state_dir
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import HostContext, RsyncInfo, RsyncSource, ToolAvailability

TERMINAL_SEARCH_LIST: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("konsole", ("-e",)),
    ("gnome-terminal", ("--",)),
    ("xterm", ("-e",)),
)
"""Terminal emulators tried in order for interactive shells (upstream :1546-1548)."""


def _rsync_candidates() -> list[tuple[Path, RsyncSource]]:
    return [(p, "path") for p in path_scan("rsync")]


class LinuxAdapter:
    """Linux implementation of the HostAdapter protocol."""

    def state_dir(self) -> Path:
        # Upstream-compatible: ~/.config/steamos-devkit (spec section 3).
        return platform_default_state_dir()

    def rsync(self) -> RsyncInfo:
        env = os.environ.get(constants.RSYNC_ENV_VAR)
        if env:
            # An explicit override is authoritative (see macos.py).
            return probe_rsync(Path(env).expanduser(), "env")
        return probe_rsync_candidates(_rsync_candidates())

    def ssh(self) -> Path:
        env = os.environ.get(constants.SSH_ENV_VAR)
        if env:
            return Path(env).expanduser()
        return which_path("ssh") or Path("/usr/bin/ssh")

    def fix_key_permissions(self, path: Path) -> None:
        try:
            os.chmod(path, 0o400)
        except OSError as e:
            raise DevkitError(
                ErrorCode.PERMISSION_KEY_FILE,
                f"cannot set 0400 on key file {path}: {e}",
                details={"path": str(path)},
            ) from e

    def _xdg_open(self, target: str) -> None:
        opener = which_path("xdg-open")
        if opener is None:
            raise DevkitError(
                ErrorCode.DEPENDENCY_MISSING, "xdg-open not found; cannot open files or URLs"
            )
        subprocess.Popen([str(opener), target])

    def open_folder(self, path: Path) -> None:
        self._xdg_open(str(path))

    def open_url(self, url: str) -> None:
        self._xdg_open(url)

    def terminal_launch(self, cmd: list[str]) -> subprocess.Popen[bytes]:
        for name, prefix in TERMINAL_SEARCH_LIST:
            resolved = which_path(name)
            if resolved is None:
                continue
            try:
                return subprocess.Popen([str(resolved), *prefix, *cmd])
            except OSError as e:
                raise DevkitError(ErrorCode.DEPENDENCY_MISSING, f"cannot launch {name}: {e}") from e
        searched = ", ".join(name for name, _ in TERMINAL_SEARCH_LIST)
        raise DevkitError(
            ErrorCode.DEPENDENCY_MISSING,
            f"no terminal emulator found (searched: {searched})",
        )

    def discover_tools(self) -> ToolAvailability:
        ssh_path = self.ssh()
        terminal = next(
            (name for name, _ in TERMINAL_SEARCH_LIST if which_path(name) is not None),
            None,
        )
        return ToolAvailability(
            rsync=self.rsync(),
            ssh_path=ssh_path if ssh_path.is_file() else None,
            ssh_version=ssh_version(ssh_path) if ssh_path.is_file() else None,
            terminal=terminal,
        )

    def context(self) -> HostContext:
        return detect_context()
