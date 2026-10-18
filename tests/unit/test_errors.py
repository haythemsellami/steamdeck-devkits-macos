"""Unit tests for steamos_devkit.errors (spec section 6)."""

import pytest

from steamos_devkit.errors import DevkitError, ErrorCode

SPEC_CODES = (
    "DEVICE_NOT_FOUND",
    "DEVICE_UNREACHABLE",
    "AMBIGUOUS_DEVICE",
    "PAIRING_DECLINED",
    "PAIRING_TIMEOUT",
    "PAIRING_FAILED",
    "DEPENDENCY_MISSING",
    "OPENRSYNC_DETECTED",
    "PERMISSION_LOCAL_NETWORK",
    "PERMISSION_KEY_FILE",
    "VALIDATION",
    "UNSUPPORTED",
    "REMOTE_COMMAND_FAILED",
    "TIMEOUT",
    "CANCELLED",
    "JOB_NOT_FOUND",
    "ARTIFACT_NOT_FOUND",
    "STATE_CORRUPT",
)


def test_error_code_enum_matches_spec_exactly() -> None:
    assert [c.name for c in ErrorCode] == list(SPEC_CODES)
    for name in SPEC_CODES:
        assert ErrorCode[name].value == name


def test_devkit_error_carries_code_message_details() -> None:
    details = {"path": "/tmp/x"}
    err = DevkitError(ErrorCode.STATE_CORRUPT, "broken", details=details, remote_exit_code=17)
    assert err.code == ErrorCode.STATE_CORRUPT
    assert err.message == "broken"
    assert err.details == details
    assert err.details is not details  # copied, not aliased
    assert err.remote_exit_code == 17
    assert str(err) == "[STATE_CORRUPT] broken"
    assert isinstance(err, Exception)


def test_devkit_error_defaults() -> None:
    err = DevkitError(ErrorCode.TIMEOUT, "slow")
    assert err.details == {}
    assert err.remote_exit_code is None


def test_details_default_is_not_shared_between_instances() -> None:
    a = DevkitError(ErrorCode.TIMEOUT, "a")
    b = DevkitError(ErrorCode.TIMEOUT, "b")
    a.details["x"] = 1
    assert b.details == {}


def test_devkit_error_is_raisable_and_catchable() -> None:
    with pytest.raises(DevkitError) as exc_info:
        raise DevkitError(ErrorCode.CANCELLED, "user abort")
    assert exc_info.value.code is ErrorCode.CANCELLED
