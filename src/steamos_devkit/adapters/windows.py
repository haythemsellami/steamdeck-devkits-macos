"""Windows host adapter (spec section 12).

Best-effort port preserving upstream behaviors: cygwin tool discovery via the
registry (``SOFTWARE\\Cygwin\\setup``), the icacls key-permission dance, and a
powershell-launched temp .ps1 for interactive shells. Compile-gated, not a
release gate.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

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

_CYGWIN_REGISTRY_KEY = r"SOFTWARE\Cygwin\setup"


def _require_windows() -> None:
    if sys.platform != "win32":
        raise DevkitError(ErrorCode.UNSUPPORTED, "WindowsAdapter only runs on win32")


def _winreg() -> Any:
    import importlib

    return importlib.import_module("winreg")


def icacls_commands(username: str, path: Path) -> list[list[str]]:
    """Upstream's key-permission dance (:637-670): reset, strip inheritance,
    grant read to the current ``DOMAIN\\user``, then echo for diagnostics."""
    target = str(path)
    return [
        ["icacls.exe", target, "/Reset"],
        ["icacls.exe", target, "/Inheritance:r"],
        ["icacls.exe", target, "/Grant:r", f"{username}:(R)"],
        ["icacls.exe", target],
    ]


def _domain_and_name() -> str:
    return subprocess.check_output(["whoami"], text=True).strip()


class WindowsAdapter:
    """win32 implementation of the HostAdapter protocol."""

    def state_dir(self) -> Path:
        return platform_default_state_dir()

    def _cygwin_root(self) -> Path | None:
        _require_windows()
        winreg = _winreg()
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                key = winreg.OpenKey(hive, _CYGWIN_REGISTRY_KEY)
            except OSError:
                continue
            try:
                rootdir, _ = winreg.QueryValueEx(key, "rootdir")
            except OSError:
                continue
            return Path(rootdir)
        return None

    def _cygwin_tool(self, name: str) -> Path | None:
        # Frozen builds package the tools next to the executable (upstream :351-357).
        if getattr(sys, "frozen", False):
            bundled = Path(sys.executable).parent / "cygroot" / "bin" / name
            if bundled.is_file():
                return bundled
        root = self._cygwin_root()
        if root is None:
            return None
        candidate = root / "bin" / name
        return candidate if candidate.is_file() else None

    def rsync(self) -> RsyncInfo:
        env = os.environ.get(constants.RSYNC_ENV_VAR)
        if env:
            # An explicit override is authoritative (see macos.py).
            return probe_rsync(Path(env).expanduser(), "env")
        candidates: list[tuple[Path, RsyncSource]] = []
        try:
            cygwin = self._cygwin_tool("rsync.exe")
        except DevkitError:
            cygwin = None
        if cygwin is not None:
            candidates.append((cygwin, "cygwin"))
        candidates.extend((p, "path") for p in path_scan("rsync"))
        return probe_rsync_candidates(candidates)

    def ssh(self) -> Path:
        env = os.environ.get(constants.SSH_ENV_VAR)
        if env:
            return Path(env).expanduser()
        # Windows' own ssh.exe is not a compatible rsync transport; prefer cygwin.
        try:
            cygwin = self._cygwin_tool("ssh.exe")
        except DevkitError:
            cygwin = None
        if cygwin is not None:
            return cygwin
        return which_path("ssh") or Path("ssh")

    def fix_key_permissions(self, path: Path) -> None:
        username = _domain_and_name()
        for cmd in icacls_commands(username, path):
            subprocess.run(cmd, capture_output=True, text=True, check=False)

    def open_folder(self, path: Path) -> None:
        _require_windows()
        os.startfile(str(path))  # type: ignore[attr-defined]

    def open_url(self, url: str) -> None:
        _require_windows()
        os.startfile(url)  # type: ignore[attr-defined]

    def terminal_launch(self, cmd: list[str]) -> subprocess.Popen[bytes]:
        """Upstream's approach (:1491-1541): a temp .ps1 run by powershell."""
        _require_windows()
        ssh_path = cmd[0]
        args_list = ",".join(f'"{c}"' for c in cmd[1:])
        script = (
            '$ErrorActionPreference = "Stop"\n'
            f'Start-Process -NoNewWindow -Wait -FilePath "{ssh_path}" -ArgumentList {args_list}\n'
        )
        handle, script_path = tempfile.mkstemp(prefix="devkit-remote-shell-", suffix=".ps1")
        os.close(handle)
        Path(script_path).write_text(script, encoding="utf-8")
        powershell = which_path("powershell.exe")
        if powershell is None:
            raise DevkitError(
                ErrorCode.DEPENDENCY_MISSING, "powershell.exe not found; cannot open a terminal"
            )
        return subprocess.Popen([str(powershell), "-ExecutionPolicy", "Bypass", script_path])

    def discover_tools(self) -> ToolAvailability:
        ssh_path = self.ssh()
        ssh_file = ssh_path if ssh_path.is_file() else None
        return ToolAvailability(
            rsync=self.rsync(),
            ssh_path=ssh_file,
            ssh_version=ssh_version(ssh_file) if ssh_file is not None else None,
            terminal="powershell.exe" if which_path("powershell.exe") else None,
        )

    def context(self) -> HostContext:
        return detect_context()
