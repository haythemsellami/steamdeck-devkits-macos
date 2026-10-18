"""Device status via the vendored ``steamos-get-status`` helper (spec section 8).

The helper tree is uploaded to ``~/devkit-utils`` on the device first (audit
section 1, upstream ``:1919-1938``), then queried as
``python3 ~/devkit-utils/steamos-get-status --json`` (audit section 4). The
model tolerates real device output (``SteamStatus.OS`` enum strings, a raw
builddate string on Frame); unrecognized keys are preserved in ``raw``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from steamos_devkit import constants
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import DeviceStatus

if TYPE_CHECKING:
    from steamos_devkit.core import DevkitCore

logger = logging.getLogger(__name__)

STATUS_COMMAND = "python3 ~/devkit-utils/steamos-get-status --json"


def parse_status(payload: Any) -> DeviceStatus:
    """Parse one ``steamos-get-status --json`` document into DeviceStatus.

    Keys the model does not know are preserved verbatim in ``raw`` so newer
    devices never lose information.
    """
    if not isinstance(payload, dict):
        raise DevkitError(
            ErrorCode.REMOTE_COMMAND_FAILED,
            "steamos-get-status returned a non-object JSON payload",
            details={"payload_type": type(payload).__name__},
        )
    known = set(DeviceStatus.model_fields) - {"raw"}
    fields = {key: value for key, value in payload.items() if key in known}
    extras = {key: value for key, value in payload.items() if key not in known}
    try:
        return DeviceStatus(**fields, raw=extras)
    except ValidationError as e:
        first = e.errors()[0] if e.errors() else {}
        raise DevkitError(
            ErrorCode.REMOTE_COMMAND_FAILED,
            f"unexpected steamos-get-status payload: {e.error_count()} validation error(s)",
            details={
                "field": ".".join(str(part) for part in first.get("loc", ())),
                "reason": str(first.get("msg", "")),
            },
        ) from e


def get_status(core: DevkitCore, selector: str) -> DeviceStatus:
    """Upload devkit-utils if needed, then read and parse device status.

    On success the device's registry entry (when it has one) gets a fresh
    ``last_seen`` timestamp; transient devices are not registered as a side
    effect.
    """
    handle = core.device_handle(selector)
    handle.ensure_utils()
    payload = handle.run_json(STATUS_COMMAND, timeout=constants.TIMEOUT_STATUS_S)
    status = parse_status(payload)
    entry = core.state.devices.get(handle.device.name)
    if entry is not None:
        entry.last_seen = datetime.now(UTC)
        core.state.devices.upsert(entry)
    return status
