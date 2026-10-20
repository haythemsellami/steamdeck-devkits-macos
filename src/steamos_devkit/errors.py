"""Error model (spec section 6).

:class:`DevkitError` is the only exception that crosses the core boundary.
Frontends map it to CLI exit codes (spec section 10) or MCP ``isError``
payloads. Keys, passphrases, and the pairing magic phrase must never appear
in messages, details, logs, or tool results.
"""

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable machine-readable error codes (spec section 6)."""

    DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"
    DEVICE_UNREACHABLE = "DEVICE_UNREACHABLE"
    AMBIGUOUS_DEVICE = "AMBIGUOUS_DEVICE"
    PAIRING_DECLINED = "PAIRING_DECLINED"
    PAIRING_TIMEOUT = "PAIRING_TIMEOUT"
    PAIRING_FAILED = "PAIRING_FAILED"
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
    OPENRSYNC_DETECTED = "OPENRSYNC_DETECTED"
    PERMISSION_LOCAL_NETWORK = "PERMISSION_LOCAL_NETWORK"
    PERMISSION_KEY_FILE = "PERMISSION_KEY_FILE"
    VALIDATION = "VALIDATION"
    UNSUPPORTED = "UNSUPPORTED"
    REMOTE_COMMAND_FAILED = "REMOTE_COMMAND_FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
    STATE_CORRUPT = "STATE_CORRUPT"
    INTERNAL = "INTERNAL"


class DevkitError(Exception):
    """The only exception type raised across the DevkitCore API boundary."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        remote_exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details) if details else {}
        self.remote_exit_code = remote_exit_code

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


# --- network error classification -----------------------------------------------

_EHOSTUNREACH = 65  # errno.EHOSTUNREACH
"""macOS TCC signature: Local Network denial surfaces as EHOSTUNREACH on LAN
connects (verified on macOS 26: unicast TCP to a device on the same subnet
fails with errno 65 for unsigned processes while Apple system tools pass)."""


def map_network_oserror(err: OSError, *, target: str) -> DevkitError:
    """Map a network OSError to an honest DevkitError.

    On darwin, EHOSTUNREACH may be a genuine unreachable host OR a blocked
    Local Network permission for the responsible app — the two are
    indistinguishable from errno alone, so the message presents both. Other
    errors (and all non-darwin platforms) map to DEVICE_UNREACHABLE.
    """
    import sys

    # urllib.error.URLError wraps the real OSError as .reason; unwrap it.
    cause = getattr(err, "reason", None)
    effective = cause if isinstance(cause, OSError) else err
    if sys.platform == "darwin" and effective.errno == _EHOSTUNREACH:
        return DevkitError(
            ErrorCode.PERMISSION_LOCAL_NETWORK,
            f"cannot reach {target} (no route to host). The device may be offline or on "
            "another network — or macOS is blocking Local Network access for this app. "
            "Check System Settings > Privacy & Security > Local Network and enable your "
            "terminal/host app, then retry.",
            details={"target": target, "errno": str(effective.errno)},
        )
    return DevkitError(
        ErrorCode.DEVICE_UNREACHABLE,
        f"cannot reach {target} ({err})",
        details={"target": target, "errno": str(effective.errno) if effective.errno else ""},
    )
