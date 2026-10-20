"""Device resolution and the SSH substrate used by every feature module.

Resolution order (spec section 8): exact registry name, then IPv4 literal,
then DNS hostname (contains a dot). mDNS-based name resolution is layered on
by the discovery module. All remote execution is paramiko with the pairing
key; rsync is only used for bulk trees (here: the devkit-utils upload).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import shlex
import subprocess
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final

import paramiko

from steamos_devkit import constants
from steamos_devkit._vendor import utils_dir
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Device

if TYPE_CHECKING:
    from steamos_devkit.core import DevkitCore

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_S = 5.0

REMOTE_COMMAND_TIMEOUT_S: Final = 120
"""Default per-command deadline for ``DeviceHandle.run``/``run_json`` (M7):
blocking SSH commands without an explicit caller timeout must not hang
forever. Callers with their own budget (e.g. the status probe's 15 s) keep
passing it; job-level timeouts bound whole operations separately."""

_ENSURE_UTILS_TIMEOUT_S: Final = 300
"""Deadline for the local rsync that uploads devkit-utils (M7)."""


@dataclass
class RemoteResult:
    """One remote command's outcome."""

    exit_code: int
    stdout: str
    stderr: str


def _parse_selector_port(port_text: str, selector: str) -> int:
    """Validate the ``:port`` suffix of an explicit selector (1-65535)."""
    try:
        port = int(port_text)
    except ValueError:
        raise DevkitError(
            ErrorCode.VALIDATION,
            f"invalid port in selector {selector!r}: {port_text!r} is not a number",
            details={"selector": selector},
        ) from None
    if not 1 <= port <= 65535:
        raise DevkitError(
            ErrorCode.VALIDATION,
            f"invalid port in selector {selector!r}: must be in range 1-65535",
            details={"selector": selector},
        )
    return port


def resolve_device(core: DevkitCore, selector: str) -> Device:
    """Resolve a selector to a registry entry or a transient explicit device.

    Selector forms (spec section 8): an exact registry name; an IPv4 literal,
    optionally with a ``:port`` suffix for the device HTTP service —
    ``192.0.2.10:32001`` for port-forwarded kits (constants
    DEVICE_HTTP_PORT_FORWARDED), defaulting to 32000; or a DNS hostname
    containing a dot (hostnames carry no port suffix). Bare names fall back to
    an mDNS lookup.
    """
    entry = core.state.devices.get(selector)
    if entry is not None:
        return entry
    address = selector
    port = constants.DEVICE_HTTP_PORT_DEFAULT
    if selector.count(":") == 1:
        host, _, port_text = selector.partition(":")
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            pass  # not IPv4:port — fall through to normal handling
        else:
            address = host
            port = _parse_selector_port(port_text, selector)
    try:
        ipaddress.IPv4Address(address)
        is_address = True
    except ValueError:
        is_address = "." in address
    if is_address:
        return Device(
            name=selector,
            address=address,
            port=port,
            login="deck",
            added_via="explicit",
        )
    # Bare name: last resort is an mDNS lookup (spec section 8, "discover-
    # capable contexts"). A lookup that found nothing falls through to
    # DEVICE_NOT_FOUND; a discovery infrastructure failure (e.g. blocked Local
    # Network permission) propagates as its own DevkitError.
    from steamos_devkit import discovery

    resolved = discovery.resolve_name(core, selector)
    if resolved is not None:
        return resolved
    raise DevkitError(
        ErrorCode.DEVICE_NOT_FOUND,
        f"unknown device {selector!r}; run `devkit discover`/`devkit pair` or use an IP address",
        details={"selector": selector},
    )


class DeviceHandle:
    """Paramiko-based handle for one device (spec sections 4 and 8)."""

    _utils_synced_at: ClassVar[dict[tuple[str, str], float]] = {}
    """Successful devkit-utils syncs this process, keyed by
    ``(state root, device address)``; values are monotonic timestamps for
    debugging only (M3)."""
    _utils_sync_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, core: DevkitCore, device: Device) -> None:
        self._core = core
        self.device = device

    @property
    def core(self) -> DevkitCore:
        """The owning core (read-only): state root, adapter, settings."""
        return self._core

    @classmethod
    def for_selector(cls, core: DevkitCore, selector: str) -> DeviceHandle:
        return cls(core, resolve_device(core, selector))

    @contextmanager
    def connect(self) -> Iterator[paramiko.SSHClient]:
        """Open an SSH session with the pairing key (spec section 4)."""
        key_path = self._core.state.key_path
        if not key_path.exists():
            raise DevkitError(
                ErrorCode.PAIRING_FAILED,
                "no devkit key; run `devkit pair <device>` first",
            )
        self._core.adapter.fix_key_permissions(key_path)
        pkey = paramiko.RSAKey.from_private_key_file(str(key_path))
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                self.device.address,
                port=22,
                username=self.device.login,
                pkey=pkey,
                timeout=_CONNECT_TIMEOUT_S,
                look_for_keys=False,
            )
        except paramiko.AuthenticationException as e:
            raise DevkitError(
                ErrorCode.PAIRING_FAILED,
                f"key rejected by {self.device.address}; re-run `devkit pair`",
            ) from e
        except (OSError, paramiko.SSHException) as e:
            # covers socket.timeout, gaierror, NoValidConnectionsError, banner errors
            raise DevkitError(
                ErrorCode.DEVICE_UNREACHABLE,
                f"cannot reach {self.device.address}:22 ({e})",
            ) from e
        try:
            yield client
        finally:
            client.close()

    def run(
        self,
        command: str,
        *,
        timeout: float | None = None,
        check: bool = True,
    ) -> RemoteResult:
        """Run a remote command; nonzero exit raises when check is set.

        ``timeout=None`` applies the default per-command deadline
        :data:`REMOTE_COMMAND_TIMEOUT_S` (M7) so a stuck channel cannot block
        a caller forever; an explicit timeout is honored as-is.

        Only DevkitError crosses this boundary: transport failures during
        exec/read map to DEVICE_UNREACHABLE, channel timeouts to TIMEOUT.
        """
        effective_timeout = timeout if timeout is not None else REMOTE_COMMAND_TIMEOUT_S
        with self.connect() as ssh:
            try:
                _stdin, stdout, stderr = ssh.exec_command(command, timeout=effective_timeout)
                out = stdout.read().decode("utf-8", errors="replace")
                err = stderr.read().decode("utf-8", errors="replace")
                code = stdout.channel.recv_exit_status()
            except TimeoutError as e:
                raise DevkitError(
                    ErrorCode.TIMEOUT,
                    f"remote command timed out after {effective_timeout}s: {command}",
                    details={"command": command},
                ) from e
            except (OSError, paramiko.SSHException) as e:
                raise DevkitError(
                    ErrorCode.DEVICE_UNREACHABLE,
                    f"connection lost during remote command: {command} ({e})",
                    details={"command": command},
                ) from e
        result = RemoteResult(exit_code=code, stdout=out, stderr=err)
        if check and code != 0:
            raise DevkitError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                f"remote command failed ({code}): {command}",
                details={"command": command, "stderr_tail": err[-500:]},
                remote_exit_code=code,
            )
        return result

    def run_json(self, command: str, *, timeout: float | None = None) -> Any:
        """Run a remote command whose stdout must be JSON.

        ``timeout=None`` inherits the same default deadline as ``run``."""
        result = self.run(command, timeout=timeout)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise DevkitError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                f"invalid JSON from remote command: {command}",
                details={"command": command, "stdout_head": result.stdout[:500]},
                remote_exit_code=result.exit_code,
            ) from e

    def _sftp_call(self, op: str, remote_path: str, local_path: Path) -> None:
        """One SFTP get/put with the same DevkitError-only boundary as run().

        Transport failures map to DEVICE_UNREACHABLE, channel timeouts to
        TIMEOUT; file-level errors (missing remote file, permissions) map to
        REMOTE_COMMAND_FAILED.
        """
        with self.connect() as ssh:
            try:
                sftp = ssh.open_sftp()
                try:
                    if op == "get":
                        sftp.get(remote_path, str(local_path))
                    else:
                        sftp.put(str(local_path), remote_path)
                finally:
                    sftp.close()
            except TimeoutError as e:
                raise DevkitError(
                    ErrorCode.TIMEOUT,
                    f"sftp {op} timed out: {remote_path}",
                    details={"remote_path": remote_path},
                ) from e
            except paramiko.SSHException as e:
                raise DevkitError(
                    ErrorCode.DEVICE_UNREACHABLE,
                    f"connection lost during sftp {op}: {remote_path} ({e})",
                    details={"remote_path": remote_path},
                ) from e
            except OSError as e:
                raise DevkitError(
                    ErrorCode.REMOTE_COMMAND_FAILED,
                    f"sftp {op} failed for {remote_path}: {e}",
                    details={"remote_path": remote_path, "local_path": str(local_path)},
                ) from e

    def sftp_get(self, remote_path: str, local_path: Path) -> None:
        self._sftp_call("get", remote_path, local_path)

    def sftp_put(self, local_path: Path, remote_path: str) -> None:
        self._sftp_call("put", remote_path, local_path)

    def ensure_utils(self, force: bool = False) -> None:
        """rsync the vendored devkit-utils tree to ~/devkit-utils (audit section 1a).

        Synced once per process per ``(state root, device address)`` and
        skipped on later calls (M3) — upstream syncs once per identify
        (``:1919-1938``), not on every operation. The tradeoff: a long-lived
        MCP server never re-syncs helpers mid-process; ``force=True``
        re-uploads explicitly. Threadsafe via a class-level lock; two threads
        racing a first sync may run two identical rsyncs (idempotent), and a
        failed sync is never cached.
        """
        cache_key = (str(self._core.state.root), self.device.address)
        with self._utils_sync_lock:
            if not force and cache_key in self._utils_synced_at:
                return
        rsync = self._core.adapter.rsync()
        if not rsync.ok or rsync.path is None:
            if rsync.flavor == "openrsync":
                raise DevkitError(
                    ErrorCode.OPENRSYNC_DETECTED,
                    rsync.message or "openrsync is not supported; install rsync 3.x",
                )
            raise DevkitError(ErrorCode.DEPENDENCY_MISSING, rsync.message or "rsync 3.x not found")
        ssh = self._core.adapter.ssh()
        key_path = self._core.state.key_path
        rsh = (
            f"{ssh} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-i {shlex.quote(str(key_path))}"
        )
        argv = [
            str(rsync.path),
            "-a",
            "--chmod=Du=rwx,Dgo=rx,Fu=rwx,Fog=rx",
            "-e",
            rsh,
            f"{utils_dir()}/",
            f"{self.device.login}@{self.device.address}:devkit-utils/",
        ]
        result = self._run_local(argv)
        if result.returncode != 0:
            raise DevkitError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                f"devkit-utils upload failed ({result.returncode})",
                details={"stderr_tail": result.stderr[-500:]},
                remote_exit_code=result.returncode,
            )
        with self._utils_sync_lock:
            self._utils_synced_at[cache_key] = time.monotonic()

    def _run_local(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Local-subprocess seam (tests patch this); bounded by
        ``_ENSURE_UTILS_TIMEOUT_S`` so a hung rsync cannot block forever (M7)."""
        try:
            return subprocess.run(
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=_ENSURE_UTILS_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as e:
            raise DevkitError(
                ErrorCode.TIMEOUT,
                f"local command timed out after {_ENSURE_UTILS_TIMEOUT_S}s: {argv[0]}",
                details={"argv": [str(a) for a in argv]},
            ) from e
