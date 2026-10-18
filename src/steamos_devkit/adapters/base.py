"""HostAdapter protocol (spec section 12) plus shared rsync/ssh probing."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final, Protocol, cast, runtime_checkable

from steamos_devkit import constants
from steamos_devkit.models import (
    HostContext,
    RsyncFlavor,
    RsyncInfo,
    RsyncSource,
    ToolAvailability,
)

MIN_RSYNC_PROTOCOL: Final = 30
"""rsync 3.x speaks protocol >= 30; openrsync peaks at 29 (spec section 12)."""

RSYNC_INSTALL_HINT: Final = "install rsync 3.x with: brew install rsync"

_VERSION_TIMEOUT_S: Final = 10


@runtime_checkable
class HostAdapter(Protocol):
    """Platform seam for paths, permissions, processes, and tool discovery."""

    def state_dir(self) -> Path:
        """Platform-conventional state root (env overrides are config's job)."""
        ...

    def rsync(self) -> RsyncInfo:
        """Probe the best rsync candidate; never raises, ``ok`` reports fitness."""
        ...

    def ssh(self) -> Path:
        """Best-known system ssh path for interactive shells."""
        ...

    def fix_key_permissions(self, path: Path) -> None:
        """Restrict a private key file to owner-read (0400 semantics)."""
        ...

    def open_folder(self, path: Path) -> None: ...

    def open_url(self, url: str) -> None: ...

    def terminal_launch(self, cmd: list[str]) -> subprocess.Popen[bytes]:
        """Run an interactive command (e.g. ssh -t) in a new terminal window."""
        ...

    def discover_tools(self) -> ToolAvailability:
        """Aggregate probe of every host dependency (spec section 9)."""
        ...

    def context(self) -> HostContext: ...


def parse_rsync_version_output(text: str) -> tuple[RsyncFlavor, str | None, int | None]:
    """Parse ``rsync --version`` output into (flavor, version, protocol).

    Real rsync starts with ``rsync  version 3.2.7  protocol version 31 ...``;
    openrsync starts with ``openrsync: protocol version 29``.
    """
    lines = text.splitlines()
    first_line = lines[0] if lines else ""
    proto = re.search(r"protocol version (\d+)", text)
    protocol = int(proto.group(1)) if proto else None
    if re.match(r"openrsync", first_line, re.IGNORECASE):
        return "openrsync", None, protocol
    match = re.match(r"rsync\s+version\s+(\S+)", first_line)
    if match:
        return "rsync", match.group(1), protocol
    return "unknown", None, protocol


def probe_rsync(candidate: Path, source: RsyncSource) -> RsyncInfo:
    """Run ``candidate --version`` and grade it against the rsync 3.x requirement."""
    info = RsyncInfo(path=candidate, source=source)
    try:
        proc = subprocess.run(
            [str(candidate), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=_VERSION_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        info.message = f"failed to run: {e}"
        return info
    flavor, version, protocol = parse_rsync_version_output(proc.stdout)
    info.flavor = flavor
    info.version = version
    info.protocol_version = protocol
    if flavor == "openrsync":
        info.message = f"openrsync is not compatible (protocol {protocol}); {RSYNC_INSTALL_HINT}"
    elif flavor != "rsync":
        info.message = "unrecognized rsync --version output"
    elif protocol is None or protocol < MIN_RSYNC_PROTOCOL:
        info.message = (
            f"rsync protocol {protocol} is too old; need >= {MIN_RSYNC_PROTOCOL} "
            f"({RSYNC_INSTALL_HINT})"
        )
    else:
        info.ok = True
    return info


def probe_rsync_candidates(candidates: list[tuple[Path, RsyncSource]]) -> RsyncInfo:
    """Probe candidates in resolution order.

    Returns the first fit candidate; otherwise the first probed candidate (its
    message explains the failure); otherwise an empty not-found result.
    """
    first: RsyncInfo | None = None
    for path, source in candidates:
        info = probe_rsync(path, source)
        if info.ok:
            return info
        if first is None:
            first = info
    if first is not None:
        return first
    return RsyncInfo(source="none", ok=False, message=f"rsync not found; {RSYNC_INSTALL_HINT}")


def path_scan(binary: str) -> list[Path]:
    """Every executable match for binary on PATH, in PATH order."""
    found: list[Path] = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / binary
        if candidate.is_file() and os.access(candidate, os.X_OK):
            found.append(candidate)
    return found


def which_path(binary: str) -> Path | None:
    resolved = shutil.which(binary)
    return Path(resolved) if resolved else None


def ssh_version(ssh_path: Path) -> str | None:
    """``ssh -V`` prints e.g. ``OpenSSH_9.9p2, ...`` to stderr."""
    try:
        proc = subprocess.run(
            [str(ssh_path), "-V"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=_VERSION_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = proc.stdout.strip()
    return output.split(",", 1)[0] if output else None


def detect_context() -> HostContext:
    """Process context: env override (MCP server), frozen bundle, else cli."""
    override = os.environ.get(constants.CONTEXT_ENV_VAR)
    if override in ("cli", "bundled_app", "mcp_stdio"):
        return cast("HostContext", override)
    if getattr(sys, "frozen", False):
        return "bundled_app"
    return "cli"
