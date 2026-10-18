"""macOS host adapter (spec section 12).

rsync resolution order: ``$DEVKIT_RSYNC`` -> bundled
``Contents/Resources/bin/rsync`` when frozen -> ``/opt/homebrew/bin/rsync`` ->
PATH scan, with a flavor check on whatever is found (openrsync rejected,
protocol >= 30 required).
"""

import os
import shlex
import subprocess
import sys
from pathlib import Path

from steamos_devkit import constants
from steamos_devkit.adapters.base import (
    detect_context,
    path_scan,
    probe_rsync,
    probe_rsync_candidates,
    ssh_version,
)
from steamos_devkit.config import platform_default_state_dir
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import HostContext, RsyncInfo, RsyncSource, ToolAvailability

_HOMEBREW_RSYNC = Path("/opt/homebrew/bin/rsync")
_SYSTEM_SSH = Path("/usr/bin/ssh")
_OPEN = Path("/usr/bin/open")
_OSASCRIPT = Path("/usr/bin/osascript")
_ITERM_APP = Path("/Applications/iTerm.app")
_FILEZILLA_APP = Path("/Applications/FileZilla.app")


def _bundled_rsync() -> Path | None:
    """``Contents/Resources/bin/rsync`` when running from a frozen .app."""
    if not getattr(sys, "frozen", False):
        return None
    # sys.executable is Contents/MacOS/<name>
    candidate = Path(sys.executable).resolve().parent.parent / "Resources" / "bin" / "rsync"
    return candidate if candidate.is_file() else None


def _rsync_candidates() -> list[tuple[Path, RsyncSource]]:
    candidates: list[tuple[Path, RsyncSource]] = []
    bundled = _bundled_rsync()
    if bundled is not None:
        candidates.append((bundled, "bundled"))
    if _HOMEBREW_RSYNC.is_file():
        candidates.append((_HOMEBREW_RSYNC, "homebrew"))
    candidates.extend((p, "path") for p in path_scan("rsync"))
    seen: set[Path] = set()
    unique: list[tuple[Path, RsyncSource]] = []
    for path, source in candidates:
        key = path.resolve() if path.exists() else path
        if key not in seen:
            seen.add(key)
            unique.append((path, source))
    return unique


def _applescript_escape(text: str) -> str:
    """Escape text for embedding in a double-quoted AppleScript string."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _iterm_installed() -> bool:
    return _ITERM_APP.is_dir()


def _filezilla_installed() -> bool:
    return _FILEZILLA_APP.is_dir()


class MacOSAdapter:
    """Darwin implementation of the HostAdapter protocol."""

    def state_dir(self) -> Path:
        return platform_default_state_dir()

    def rsync(self) -> RsyncInfo:
        env = os.environ.get(constants.RSYNC_ENV_VAR)
        if env:
            # An explicit override is authoritative: a broken one must not be
            # silently ignored in favor of a fallback.
            return probe_rsync(Path(env).expanduser(), "env")
        return probe_rsync_candidates(_rsync_candidates())

    def ssh(self) -> Path:
        env = os.environ.get(constants.SSH_ENV_VAR)
        if env:
            return Path(env).expanduser()
        return _SYSTEM_SSH

    def fix_key_permissions(self, path: Path) -> None:
        """POSIX chmod 0400 — the Darwin branch upstream lacks (audit section 6.1)."""
        try:
            os.chmod(path, 0o400)
        except OSError as e:
            raise DevkitError(
                ErrorCode.PERMISSION_KEY_FILE,
                f"cannot set 0400 on key file {path}: {e}",
                details={"path": str(path)},
            ) from e

    def open_folder(self, path: Path) -> None:
        subprocess.Popen([str(_OPEN), str(path)])

    def open_url(self, url: str) -> None:
        subprocess.Popen([str(_OPEN), url])

    def terminal_launch(self, cmd: list[str]) -> subprocess.Popen[bytes]:
        """Run cmd in a new iTerm2 window if installed, else Terminal.app."""
        command = _applescript_escape(shlex.join(cmd))
        if _iterm_installed():
            script = (
                'tell application "iTerm"\n'
                f'  create window with default profile command "{command}"\n'
                "end tell"
            )
        else:
            script = f'tell application "Terminal"\n  do script "{command}"\n  activate\nend tell'
        try:
            return subprocess.Popen([str(_OSASCRIPT), "-e", script])
        except OSError as e:
            raise DevkitError(
                ErrorCode.DEPENDENCY_MISSING,
                f"cannot launch a terminal via osascript: {e}",
            ) from e

    def discover_tools(self) -> ToolAvailability:
        ssh_path = self.ssh()
        terminal = "iTerm2" if _iterm_installed() else "Terminal.app"
        return ToolAvailability(
            rsync=self.rsync(),
            ssh_path=ssh_path if ssh_path.is_file() else None,
            ssh_version=ssh_version(ssh_path) if ssh_path.is_file() else None,
            terminal=terminal,
            filezilla=_FILEZILLA_APP if _filezilla_installed() else None,
        )

    def context(self) -> HostContext:
        return detect_context()
