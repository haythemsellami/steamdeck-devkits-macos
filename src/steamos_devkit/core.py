"""DevkitCore — the single typed API surface for CLI, MCP, and GUI (spec section 8).

Core methods take typed models and return typed results; nothing here prints,
raises a bare exception across the boundary, or reads process-global state.
Only milestone-M0 surfaces are implemented; later milestones add the rest.
"""

import logging
import os
import platform
import sys
import tempfile
from pathlib import Path

from steamos_devkit import constants
from steamos_devkit.adapters import HostAdapter, get_adapter
from steamos_devkit.config import StateStore, resolve_state_dir
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import DoctorCheck, DoctorReport, RsyncInfo

logger = logging.getLogger(__name__)

_LOCAL_NETWORK_GUIDANCE = (
    "Local Network permission is not programmatically readable on macOS. mDNS "
    "discovery from a bundled app or an MCP host (e.g. a desktop assistant) may "
    "be silently blocked until the responsible host app is granted access under "
    "System Settings > Privacy & Security > Local Network. Connecting by "
    "hostname/IP always works."
)


class DevkitCore:
    """Facade wiring the host adapter to the state store (spec section 8)."""

    def __init__(self, state_dir: Path | None = None, adapter: HostAdapter | None = None) -> None:
        self._adapter: HostAdapter = adapter if adapter is not None else get_adapter()
        self._state = StateStore(
            resolve_state_dir(state_dir, default=self._adapter.state_dir())
        )

    @property
    def adapter(self) -> HostAdapter:
        return self._adapter

    @property
    def state(self) -> StateStore:
        return self._state

    def doctor(self) -> DoctorReport:
        """Full host report: rsync, ssh, python, platform, state dir, terminal,
        and the Local Network heuristic (spec sections 9 and 15)."""
        tools = self._adapter.discover_tools()
        checks = [
            self._check_rsync(tools.rsync),
            self._check_ssh(tools.ssh_path, tools.ssh_version),
            self._check_python(),
            self._check_platform(),
            self._check_state_dir(),
            self._check_terminal(tools.terminal),
            DoctorCheck(name="local-network", status="unknown", summary=_LOCAL_NETWORK_GUIDANCE),
        ]
        return DoctorReport(
            ok=all(c.status != "error" for c in checks),
            context=self._adapter.context(),
            platform=sys.platform,
            arch=platform.machine(),
            python_version=platform.python_version(),
            state_dir=self._state.root,
            tools=tools,
            checks=checks,
        )

    # -- individual checks ------------------------------------------------------

    @staticmethod
    def _check_rsync(rsync: RsyncInfo) -> DoctorCheck:
        if rsync.ok:
            return DoctorCheck(
                name="rsync",
                status="ok",
                summary=(
                    f"{rsync.flavor} {rsync.version} (protocol {rsync.protocol_version}) "
                    f"at {rsync.path}"
                ),
                details={"path": str(rsync.path), "source": rsync.source},
            )
        return DoctorCheck(
            name="rsync",
            status="error",
            summary=rsync.message or "rsync probe failed",
            details={
                "path": str(rsync.path) if rsync.path else "",
                "source": rsync.source,
            },
        )

    @staticmethod
    def _check_ssh(ssh_path: Path | None, ssh_version: str | None) -> DoctorCheck:
        if ssh_path is None:
            return DoctorCheck(
                name="ssh",
                status="error",
                summary=f"ssh not found; set ${constants.SSH_ENV_VAR} or install OpenSSH",
            )
        return DoctorCheck(
            name="ssh",
            status="ok",
            summary=f"{ssh_version or 'version unknown'} at {ssh_path}",
            details={"path": str(ssh_path)},
        )

    @staticmethod
    def _check_python() -> DoctorCheck:
        version = platform.python_version()
        ok = sys.version_info >= (3, 11)
        return DoctorCheck(
            name="python",
            status="ok" if ok else "error",
            summary=f"python {version}" + ("" if ok else " — 3.11 or newer is required"),
        )

    @staticmethod
    def _check_platform() -> DoctorCheck:
        return DoctorCheck(
            name="platform",
            status="ok",
            summary=f"{sys.platform} {platform.machine()}",
        )

    def _check_state_dir(self) -> DoctorCheck:
        root = self._state.root
        try:
            self._state.ensure_layout()
            fd, probe = tempfile.mkstemp(dir=root, prefix=".doctor-", suffix=".tmp")
            os.close(fd)
            os.unlink(probe)
        except OSError as e:
            return DoctorCheck(
                name="state-dir",
                status="error",
                summary=f"state dir {root} is not writable: {e}",
                details={"path": str(root)},
            )
        return DoctorCheck(
            name="state-dir",
            status="ok",
            summary=f"state dir {root} is writable",
            details={"path": str(root)},
        )

    @staticmethod
    def _check_terminal(terminal: str | None) -> DoctorCheck:
        if terminal is None:
            return DoctorCheck(
                name="terminal",
                status="warning",
                summary="no terminal emulator found; interactive shells (devkit shell) "
                "will be unavailable",
            )
        return DoctorCheck(name="terminal", status="ok", summary=f"interactive shells via {terminal}")


__all__ = ["DevkitCore", "DevkitError", "ErrorCode"]
