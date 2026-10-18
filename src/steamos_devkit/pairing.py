"""Pairing: key management and the device ``/register`` flow (spec section 8).

Protocol evidence (audit section 3, upstream ``register()`` at
``_vendor/devkit_client/__init__.py:1088-1125``): the client generates an
RSA-2048 key once, then POSTs the public key line plus a shared magic phrase
to the device's HTTP service. The device shows a Steam UI approval modal; a
403 carries the device-side ``ssh-approve-key`` error as JSON lines mixed into
plain log text.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import socket
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import paramiko

from steamos_devkit import constants
from steamos_devkit.discovery import sshd_flag
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Device, Result

if TYPE_CHECKING:
    from steamos_devkit.core import DevkitCore

logger = logging.getLogger(__name__)

# Shared token appended to the public key in the /register POST body. Upstream
# is explicit that this is luck reduction, not a security boundary
# (_vendor/devkit_client/__init__.py:1088 and the comment in register():
# "Of course if someone is eavesdropping on the traffic, or gains access to
# either this source or the approve-ssh-key, this falls apart too"). Per spec
# section 6 it must never appear in messages, details, logs, or results.
MAGIC_PHRASE: Final = "900b919520e4cf601998a71eec318fec"

_KEY_BITS: Final = 2048  # RSA-2048: device-side ssh-approve-key compatibility (D4)


def public_key_line(key: paramiko.RSAKey) -> str:
    """``ssh-rsa <base64> devkit-client:<user>@<host>`` (upstream :673-683)."""
    comment = f"devkit-client:{getpass.getuser()}@{socket.gethostname()}"
    return f"ssh-rsa {key.get_base64()} {comment}\n"


def ensure_key(core: DevkitCore) -> Path:
    """Return the pairing key path, generating the RSA-2048 key on first use.

    Layout (spec section 3): ``keys/devkit_rsa[.pub]``, mode 0400, private key
    written with O_EXCL so a concurrent first-run never truncates a live key.
    """
    core.state.ensure_layout()
    key_path = core.state.key_path
    if not key_path.exists():
        key = paramiko.RSAKey.generate(_KEY_BITS)
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        except FileExistsError:
            pass  # another process won the first-write race; use its key
        else:
            with os.fdopen(fd, "w") as handle:
                key.write_private_key(handle)
            core.state.pubkey_path.write_text(public_key_line(key), encoding="utf-8")
            logger.info("generated new devkit key at %s", key_path)
    core.adapter.fix_key_permissions(key_path)
    return key_path


def _no_proxy_opener() -> urllib.request.OpenerDirector:
    """LAN requests must bypass proxy env vars; a corporate proxy cannot reach
    a SteamOS device on the local network (and must never see pairing traffic).
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _urlopen(request: urllib.request.Request, timeout: float) -> bytes:
    """Single HTTP seam (tests patch this; never leaves the LAN unproxied)."""
    with _no_proxy_opener().open(request, timeout=timeout) as response:
        data: bytes = response.read()
        return data


def _is_timeout(exc: OSError) -> bool:
    reason = getattr(exc, "reason", None)  # urllib.error.URLError wraps the cause
    return isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url)


def fetch_properties(address: str, port: int, timeout: float) -> dict[str, Any]:
    """GET ``/properties.json``; legacy fallback ``GET /login-name`` (audit §3).

    Returns ``{"login": str | None, "settings": dict, "sshd": bool | None}`` —
    ``settings`` is the embedded JSON string parsed into a dict.
    """
    base = f"http://{address}:{port}"
    try:
        payload = _urlopen(_request(base + constants.DEVICE_HTTP_PROPERTIES_PATH), timeout)
    except OSError as e:
        if _is_timeout(e):
            raise DevkitError(
                ErrorCode.TIMEOUT,
                f"timed out querying device properties at {address}:{port}",
            ) from e
        logger.debug(
            "properties.json unavailable at %s:%s (%s); trying /login-name", address, port, e
        )
    else:
        try:
            data: Any = json.loads(payload)
        except ValueError:
            logger.debug("properties.json at %s:%s was not JSON", address, port)
        else:
            if isinstance(data, dict):
                settings_raw = data.get("settings")
                settings: dict[str, Any] = {}
                if isinstance(settings_raw, str):
                    try:
                        parsed: Any = json.loads(settings_raw)
                        if isinstance(parsed, dict):
                            settings = parsed
                    except ValueError:
                        logger.debug("properties.json settings was not embedded JSON")
                login = data.get("login")
                return {
                    "login": login if isinstance(login, str) else None,
                    "settings": settings,
                    "sshd": sshd_flag(settings),
                }
    # Legacy devices only answer /login-name (upstream :1062-1085).
    try:
        payload = _urlopen(_request(base + constants.DEVICE_HTTP_LOGIN_NAME_PATH), timeout)
    except OSError as e:
        if _is_timeout(e):
            raise DevkitError(
                ErrorCode.TIMEOUT,
                f"timed out querying device login at {address}:{port}",
            ) from e
        raise DevkitError(
            ErrorCode.DEVICE_UNREACHABLE,
            f"device HTTP service at {address}:{port} is not responding "
            "(is the devkit service running?); pairing needs port 32000",
            details={"address": address, "port": port},
        ) from e
    login = payload.decode("utf-8", errors="replace").strip()
    if not login:
        raise DevkitError(
            ErrorCode.DEVICE_UNREACHABLE,
            f"device HTTP service at {address}:{port} returned an empty login name",
            details={"address": address, "port": port},
        )
    return {"login": login, "settings": {}, "sshd": None}


def _parse_decline_reason(body: bytes) -> str:
    """403 bodies mix plain log lines with JSON (fixture pairing-403-body.txt);
    the first JSON-parseable line's ``error`` key carries the reason."""
    for line in body.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed: Any = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, str):
                return error
    return "pairing was declined on the device"


def pair(core: DevkitCore, selector: str, timeout_s: int = constants.TIMEOUT_PAIR_S) -> Result:
    """Register our key with a device (spec section 8; upstream :1088-1125).

    Explicit IPs work unpaired: resolution never requires a registry entry.
    Protocol outcomes (declined, timeout, unreachable) come back as failure
    Results; only local API-boundary violations raise DevkitError.
    """
    device = core.resolve_device(selector)
    try:
        properties = fetch_properties(
            device.address,
            device.port,
            timeout=min(float(timeout_s), float(constants.LAN_REQUEST_TIMEOUT_S)),
        )
    except DevkitError as e:
        code = ErrorCode.PAIRING_TIMEOUT if e.code == ErrorCode.TIMEOUT else e.code
        return Result.failure(code, e.message, device=device)
    login = properties["login"] or device.login

    key_path = ensure_key(core)
    pubkey_line = core.state.pubkey_path.read_text(encoding="utf-8").strip("\n")
    if not pubkey_line.startswith("ssh-rsa "):
        raise DevkitError(
            ErrorCode.STATE_CORRUPT,
            f"public key at {core.state.pubkey_path} is malformed; delete it and re-pair",
            details={"path": str(core.state.pubkey_path)},
        )
    body = f"{pubkey_line} {MAGIC_PHRASE}\n".encode("ascii")
    url = f"http://{device.address}:{device.port}{constants.DEVICE_HTTP_REGISTER_PATH}"
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "text/plain"}, method="POST"
    )
    logger.info("registering key %s with %s", key_path, device.address)
    try:
        _urlopen(request, timeout=float(timeout_s))
    except urllib.error.HTTPError as e:
        detail = e.read()
        if e.code == 403:
            return Result.failure(
                ErrorCode.PAIRING_DECLINED, _parse_decline_reason(detail), device=device
            )
        return Result.failure(
            ErrorCode.PAIRING_FAILED,
            f"device returned HTTP {e.code} during pairing",
            device=device,
        )
    except OSError as e:
        if _is_timeout(e):
            return Result.failure(
                ErrorCode.PAIRING_TIMEOUT,
                f"no answer to the pairing request within {timeout_s}s; "
                "approve the request on the device and retry",
                device=device,
            )
        return Result.failure(
            ErrorCode.DEVICE_UNREACHABLE,
            f"device HTTP service at {device.address}:{device.port} is not responding ({e})",
            device=device,
        )

    now = datetime.now(UTC)
    paired = Device(
        name=device.name,
        address=device.address,
        port=device.port,
        login=login,
        family=device.family,
        paired_at=now,
        added_via=device.added_via,
        last_seen=now,
    )
    core.state.devices.upsert(paired)
    core.state.set_last_device(paired.name)
    return Result.success(device=paired, message="paired")
