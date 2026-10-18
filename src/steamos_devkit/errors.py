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
