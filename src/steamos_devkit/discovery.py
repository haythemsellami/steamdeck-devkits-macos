"""mDNS discovery of SteamOS devkits (spec sections 4, 8, and 9).

Unlike upstream's GUI-owned zeroconf singleton (audit section 8, seam a), the
:class:`DiscoveryService` here is core-owned: one instance manages one
``Zeroconf`` + ``ServiceBrowser`` lifecycle and collects announcements on a
thread-safe queue, closed deterministically by the caller.

Protocol constants (audit section 3): service type
``_steamos-devkit._tcp.local.``, TXT ``txtvers`` must be ``b"1"`` when present
(upstream ``:540-544``), IPv4 only (``socket.inet_ntoa``, upstream ``:561``),
TXT ``settings`` is an embedded JSON string and ``login`` carries the SSH
user.
"""

from __future__ import annotations

import errno as errnomod
import json
import logging
import queue
import socket
import string
import time
import unicodedata
from datetime import UTC, datetime
from ipaddress import IPv4Address
from typing import TYPE_CHECKING, Any

import zeroconf

from steamos_devkit import constants
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Device, DiscoveredDevice

if TYPE_CHECKING:
    from steamos_devkit.core import DevkitCore

logger = logging.getLogger(__name__)

# Upstream case-folds ASCII only, leaving non-ASCII unaltered
# (_vendor/devkit_client/__init__.py: ASCII_LOWERCASE); mDNS is
# ASCII-case-insensitive and mandates fully-composed UTF-8.
_ASCII_LOWERCASE = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)

_TXT_TXTVERS = b"txtvers"
_TXT_SETTINGS = b"settings"
_TXT_LOGIN = b"login"


def normalize_mdns_name(name: str) -> str:
    """Case-fold ASCII and NFC-normalize, as upstream does before comparing."""
    return unicodedata.normalize("NFC", name.translate(_ASCII_LOWERCASE).rstrip("."))


def _parse_settings(raw: bytes | None) -> dict[str, Any] | None:
    """TXT ``settings`` is an embedded JSON string (audit section 3)."""
    if raw is None:
        return None
    try:
        parsed: Any = json.loads(raw)
    except ValueError:
        logger.warning("ignoring unparseable TXT settings: %r", raw[:100])
        return None
    return parsed if isinstance(parsed, dict) else None


def sshd_flag(settings: dict[str, Any] | None) -> bool | None:
    """The ``sshd`` TXT/settings flag arrives as a string (e.g. ``"1"``)."""
    if settings is None or "sshd" not in settings:
        return None
    value = settings["sshd"]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes")


def _device_from_service_info(
    type_: str, name: str, info: zeroconf.ServiceInfo | None
) -> DiscoveredDevice | None:
    """Build a DiscoveredDevice from one announced service, or reject it."""
    if info is None:
        return None
    suffix = "." + type_
    service_name = name[: -len(suffix)] if name.endswith(suffix) else name
    properties = info.properties or {}
    txtvers = properties.get(_TXT_TXTVERS)
    if txtvers is not None and txtvers != constants.MDNS_TXT_VERSION.encode():
        # Accept only txtvers=1 (upstream :540-544); absent means pre-versioning.
        logger.warning("incompatible txtvers %r, ignoring %s", txtvers, service_name)
        return None
    if not info.addresses:
        logger.warning("no IPv4 address published for %s, ignoring", service_name)
        return None
    try:
        address = IPv4Address(socket.inet_ntoa(info.addresses[0]))
    except (OSError, ValueError) as e:
        logger.warning("unparseable IPv4 address for %s: %s", service_name, e)
        return None
    settings = _parse_settings(properties.get(_TXT_SETTINGS))
    login_raw = properties.get(_TXT_LOGIN)
    login = login_raw.decode("utf-8", errors="replace") if login_raw is not None else None
    return DiscoveredDevice(
        name=service_name,
        address=address,
        port=info.port or constants.DEVICE_HTTP_PORT_DEFAULT,
        login=login,
        sshd=sshd_flag(settings),
        via="mdns",
    )


class _DevkitListener(zeroconf.ServiceListener):
    """Pushes parsed announcements onto the owning service's queue.

    Callbacks run on zeroconf's engine thread; they never block the caller
    beyond one bounded ``get_service_info`` query each.
    """

    def __init__(self, zc: zeroconf.Zeroconf, found: queue.Queue[DiscoveredDevice]) -> None:
        self._zc = zc
        self._found = found

    def add_service(self, zc: zeroconf.Zeroconf, type_: str, name: str) -> None:
        info = self._zc.get_service_info(type_, name)
        device = _device_from_service_info(type_, name, info)
        if device is not None:
            logger.info("discovered %s at %s:%s", device.name, device.address, device.port)
            self._found.put(device)

    def update_service(self, zc: zeroconf.Zeroconf, type_: str, name: str) -> None:
        # Upstream treats updates as adds (upstream :569-571); the collector
        # deduplicates by name and keeps the freshest announcement.
        self.add_service(zc, type_, name)

    def remove_service(self, zc: zeroconf.Zeroconf, type_: str, name: str) -> None:
        # A bounded discovery window reports a snapshot; removals mid-window
        # do not retract an already-observed announcement.
        pass


class DiscoveryService:
    """Owns one Zeroconf + ServiceBrowser browsing the devkit service type.

    Not a singleton (upstream's was, and belonged to the GUI): callers create,
    start, collect, and close instances explicitly.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[DiscoveredDevice] = queue.Queue()
        self._zc: zeroconf.Zeroconf | None = None
        self._browser: zeroconf.ServiceBrowser | None = None

    def start(self) -> None:
        """Bind mDNS sockets and start browsing. Raises on zeroconf failure."""
        if self._zc is not None:
            raise DevkitError(ErrorCode.INTERNAL, "discovery service already started")
        zc = zeroconf.Zeroconf()
        self._zc = zc
        self._browser = zeroconf.ServiceBrowser(
            zc, constants.MDNS_SERVICE_TYPE, listener=_DevkitListener(zc, self._queue)
        )

    def collect(self, timeout_s: float) -> list[DiscoveredDevice]:
        """Gather announcements until the deadline; deduplicated by name."""
        deadline = time.monotonic() + timeout_s
        found: dict[str, DiscoveredDevice] = {}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                device = self._queue.get(timeout=remaining)
            except queue.Empty:
                break
            found[device.name] = device
        return sorted(found.values(), key=lambda d: d.name)

    def close(self) -> None:
        """Tear down the browser and zeroconf sockets; never raises."""
        browser, self._browser = self._browser, None
        zc, self._zc = self._zc, None
        if browser is not None:
            try:
                browser.cancel()
            except Exception:
                logger.debug("ServiceBrowser.cancel() failed", exc_info=True)
        if zc is not None:
            try:
                zc.close()
            except Exception:
                logger.debug("Zeroconf.close() failed", exc_info=True)

    def __enter__(self) -> DiscoveryService:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _permission_error(core: DevkitCore, cause: Exception) -> DevkitError:
    # Local Network (TCC) is not programmatically readable on macOS; a
    # permission-shaped socket error (EPERM/EACCES, e.g. zeroconf's sendto
    # blocked by TCC) in a bundled app or MCP host means the host app lacks
    # the permission (spec section 12, decision D3). The guidance text is
    # shared with `devkit doctor` (core owns the wording).
    from steamos_devkit.core import _LOCAL_NETWORK_GUIDANCE

    context = core.adapter.context()
    where = "your terminal" if context == "cli" else f"the {context} host"
    return DevkitError(
        ErrorCode.PERMISSION_LOCAL_NETWORK,
        f"mDNS discovery was denied for {where}. {_LOCAL_NETWORK_GUIDANCE}",
        details={"context": context, "cause": f"{type(cause).__name__}: {cause}"},
    )


_PERMISSION_ERRNOS = frozenset({errnomod.EPERM, errnomod.EACCES})


def _discovery_error(core: DevkitCore, cause: Exception) -> DevkitError:
    """Classify a zeroconf-level failure honestly (L4).

    Only permission-shaped OS errors (EPERM/EACCES, the signature of a macOS
    Local Network/TCC block on multicast sendto) map to
    PERMISSION_LOCAL_NETWORK. Other OS-level failures — offline, no route,
    multicast unavailable — are DEVICE_UNREACHABLE without claiming a TCC
    block; non-OS failures are an internal error.
    """
    if isinstance(cause, OSError):
        if cause.errno in _PERMISSION_ERRNOS:
            return _permission_error(core, cause)
        return DevkitError(
            ErrorCode.DEVICE_UNREACHABLE,
            f"mDNS discovery failed ({cause}); the host may be offline or "
            "multicast may be unavailable on this network",
            details={"cause": f"{type(cause).__name__}: {cause}"},
        )
    return DevkitError(
        ErrorCode.INTERNAL,
        f"mDNS discovery failed unexpectedly ({type(cause).__name__}: {cause})",
        details={"cause": f"{type(cause).__name__}: {cause}"},
    )


def discover(
    core: DevkitCore, timeout_s: float = constants.TIMEOUT_DISCOVER_S
) -> list[DiscoveredDevice]:
    """Browse for devkits for up to ``timeout_s`` seconds (spec section 8).

    Returns a (possibly empty) snapshot of announcements. A zeroconf-level
    failure is classified by cause (L4): permission-shaped socket errors raise
    PERMISSION_LOCAL_NETWORK with the Local Network guidance; other OS-level
    failures raise DEVICE_UNREACHABLE.
    """
    service = DiscoveryService()
    try:
        service.start()
        return service.collect(timeout_s)
    except DevkitError:
        raise
    except Exception as e:
        raise _discovery_error(core, e) from e
    finally:
        service.close()


def resolve_name(
    core: DevkitCore, name: str, timeout_s: float = constants.TIMEOUT_DISCOVER_S
) -> Device | None:
    """Resolve a bare device name over mDNS; None when nothing answers.

    Used by ``resolve_device`` as the last-resort fallback for bare names
    (spec section 8). The returned device is transient: it is not written to
    the registry (pairing does that).
    """
    target = normalize_mdns_name(name)
    for device in discover(core, timeout_s=timeout_s):
        if normalize_mdns_name(device.name) == target:
            return Device(
                name=device.name,
                address=str(device.address),
                port=device.port,
                login=device.login or constants.SSH_USER_DECK,
                added_via="mdns",
                last_seen=datetime.now(UTC),
            )
    return None
