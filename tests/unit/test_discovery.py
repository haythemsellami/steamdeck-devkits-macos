"""Unit tests for mDNS discovery (spec sections 4 and 8).

Zeroconf is always faked: tests never open real sockets.
"""

from __future__ import annotations

import errno
import json as jsonlib
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from steamos_devkit import cli, constants, discovery
from steamos_devkit.cli import common
from steamos_devkit.core import DevkitCore
from steamos_devkit.device import resolve_device
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Device, DiscoveredDevice

runner = CliRunner()

SERVICE_TYPE = constants.MDNS_SERVICE_TYPE


class FakeZeroconf:
    """Stand-in for zeroconf.Zeroconf: canned get_service_info, no sockets."""

    instances: ClassVar[list[FakeZeroconf]] = []
    construct_error: ClassVar[Exception | None] = None

    def __init__(self) -> None:
        if FakeZeroconf.construct_error is not None:
            raise FakeZeroconf.construct_error
        self.infos: dict[str, Any] = {}
        self.closed = False
        FakeZeroconf.instances.append(self)

    def get_service_info(self, type_: str, name: str) -> Any:
        return self.infos.get(name)

    def close(self) -> None:
        self.closed = True

    # constructor-failure path needs a fresh class per test
    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.construct_error = None


class FakeServiceBrowser:
    instances: ClassVar[list[FakeServiceBrowser]] = []

    def __init__(self, zc: Any, type_: str, listener: Any = None, **kwargs: Any) -> None:
        self.zc = zc
        self.type = type_
        self.listener = listener
        self.canceled = False
        FakeServiceBrowser.instances.append(self)

    def cancel(self) -> None:
        self.canceled = True

    @classmethod
    def reset(cls) -> None:
        cls.instances = []


@pytest.fixture(autouse=True)
def fake_zeroconf(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeZeroconf.reset()
    FakeServiceBrowser.reset()
    monkeypatch.setattr(discovery.zeroconf, "Zeroconf", FakeZeroconf)
    monkeypatch.setattr(discovery.zeroconf, "ServiceBrowser", FakeServiceBrowser)


@pytest.fixture
def core(state_dir: Path) -> DevkitCore:
    return DevkitCore(state_dir=state_dir)


def _info(
    *,
    address: str | None = "192.0.2.10",
    port: int = 32000,
    properties: dict[bytes, bytes | None] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        addresses=[socket.inet_aton(address)] if address else [],
        port=port,
        properties=properties if properties is not None else {b"txtvers": b"1"},
    )


def _start() -> tuple[discovery.DiscoveryService, FakeZeroconf, Any]:
    service = discovery.DiscoveryService()
    service.start()
    zc = FakeZeroconf.instances[-1]
    listener = FakeServiceBrowser.instances[-1].listener
    return service, zc, listener


def _announce(zc: FakeZeroconf, listener: Any, name: str, info: SimpleNamespace) -> None:
    full_name = f"{name}.{SERVICE_TYPE}"
    zc.infos[full_name] = info
    listener.add_service(zc, SERVICE_TYPE, full_name)


def test_discover_collects_device(core: DevkitCore) -> None:
    service, zc, listener = _start()
    _announce(
        zc,
        listener,
        "deck",
        _info(properties={b"txtvers": b"1", b"settings": b'{"sshd": "1"}', b"login": b"deck"}),
    )
    devices = service.collect(0.05)
    service.close()
    assert len(devices) == 1
    device = devices[0]
    assert device.name == "deck"
    assert str(device.address) == "192.0.2.10"
    assert device.port == 32000
    assert device.login == "deck"
    assert device.sshd is True
    assert device.via == "mdns"


def test_discover_empty_when_nothing_announces(core: DevkitCore) -> None:
    service, _zc, _listener = _start()
    assert service.collect(0.05) == []
    service.close()


def test_txtvers_mismatch_rejected(core: DevkitCore) -> None:
    service, zc, listener = _start()
    _announce(zc, listener, "future", _info(properties={b"txtvers": b"2", b"login": b"deck"}))
    _announce(zc, listener, "legacy", _info(properties={b"login": b"deck"}))  # no txtvers: accept
    devices = service.collect(0.05)
    service.close()
    assert [d.name for d in devices] == ["legacy"]


def test_no_ipv4_address_skipped(core: DevkitCore) -> None:
    service, zc, listener = _start()
    _announce(zc, listener, "v6only", _info(address=None))
    assert service.collect(0.05) == []
    service.close()


def test_unresolvable_service_info_skipped(core: DevkitCore) -> None:
    service, zc, listener = _start()
    listener.add_service(zc, SERVICE_TYPE, f"gone.{SERVICE_TYPE}")  # get_service_info -> None
    assert service.collect(0.05) == []
    service.close()


@pytest.mark.parametrize(
    ("raw_settings", "expected"),
    [
        (b'{"sshd": "1"}', True),
        (b'{"sshd": "0"}', False),
        (b'{"sshd": true}', True),
        (b"{}", None),
        (b"not json", None),
        (None, None),
    ],
)
def test_settings_sshd_parsing(
    core: DevkitCore, raw_settings: bytes | None, expected: bool | None
) -> None:
    service, zc, listener = _start()
    properties: dict[bytes, bytes | None] = {b"txtvers": b"1"}
    if raw_settings is not None:
        properties[b"settings"] = raw_settings
    _announce(zc, listener, "deck", _info(properties=properties))
    (device,) = service.collect(0.05)
    service.close()
    assert device.sshd is expected


def test_login_absent_is_none(core: DevkitCore) -> None:
    service, zc, listener = _start()
    _announce(zc, listener, "deck", _info())
    (device,) = service.collect(0.05)
    service.close()
    assert device.login is None


def test_non_default_port_preserved(core: DevkitCore) -> None:
    service, zc, listener = _start()
    _announce(zc, listener, "forwarded", _info(port=32001))
    (device,) = service.collect(0.05)
    service.close()
    assert device.port == 32001


def test_update_deduplicates_and_refreshes(core: DevkitCore) -> None:
    service, zc, listener = _start()
    _announce(zc, listener, "deck", _info())
    full_name = f"deck.{SERVICE_TYPE}"
    zc.infos[full_name] = _info(address="192.0.2.20", port=32001)
    listener.update_service(zc, SERVICE_TYPE, full_name)
    devices = service.collect(0.05)
    service.close()
    assert len(devices) == 1
    assert str(devices[0].address) == "192.0.2.20"
    assert devices[0].port == 32001


def test_discover_module_function_uses_service_lifecycle(core: DevkitCore) -> None:
    assert discovery.discover(core, timeout_s=0.05) == []
    zc = FakeZeroconf.instances[-1]
    browser = FakeServiceBrowser.instances[-1]
    assert zc.closed is True
    assert browser.canceled is True


@pytest.mark.parametrize("context", ["cli", "bundled_app", "mcp_stdio"])
def test_zeroconf_failure_raises_permission_local_network(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch, context: str
) -> None:
    monkeypatch.setenv(constants.CONTEXT_ENV_VAR, context)
    # permission-shaped socket error (EPERM): the macOS Local Network / TCC
    # signature on multicast sendto (L4 — only this errno class maps here)
    FakeZeroconf.construct_error = PermissionError(errno.EPERM, "Operation not permitted")
    with pytest.raises(DevkitError) as exc_info:
        discovery.discover(core, timeout_s=0.01)
    assert exc_info.value.code == ErrorCode.PERMISSION_LOCAL_NETWORK
    assert "Local Network" in exc_info.value.message
    assert exc_info.value.details["context"] == context


def test_close_tolerates_zeroconf_teardown_errors(core: DevkitCore) -> None:
    service, zc, _listener = _start()
    zc.close = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
    service.close()  # must not raise
    service.close()  # idempotent


def test_double_start_rejected(core: DevkitCore) -> None:
    service, _zc, _listener = _start()
    with pytest.raises(DevkitError) as exc_info:
        service.start()
    assert exc_info.value.code == ErrorCode.INTERNAL
    service.close()


def test_normalize_mdns_name() -> None:
    assert discovery.normalize_mdns_name("SteamDeck.") == "steamdeck"
    assert discovery.normalize_mdns_name("deck") == "deck"
    # NFC composition of decomposed UTF-8 (mDNS mandates fully-composed)
    assert discovery.normalize_mdns_name("café") == "café"


def _discovered(name: str, address: str = "192.0.2.10", login: str | None = "deck"):
    return DiscoveredDevice(name=name, address=address, login=login, via="mdns")


def test_resolve_name_matches_case_insensitively(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        discovery, "discover", lambda core, timeout_s: [_discovered("SteamDeck", "192.0.2.33")]
    )
    device = discovery.resolve_name(core, "steamdeck", timeout_s=0.01)
    assert device is not None
    assert device.name == "SteamDeck"
    assert device.address == "192.0.2.33"
    assert device.added_via == "mdns"
    assert device.last_seen is not None


def test_resolve_name_defaults_login(core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        discovery, "discover", lambda core, timeout_s: [_discovered("frame", login=None)]
    )
    device = discovery.resolve_name(core, "frame", timeout_s=0.01)
    assert device is not None
    assert device.login == constants.SSH_USER_DECK


def test_resolve_name_returns_none_when_absent(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery, "discover", lambda core, timeout_s: [])
    assert discovery.resolve_name(core, "ghost", timeout_s=0.01) is None


# --- resolve_device mDNS fallback (spec section 8 resolution order) -----------


def test_resolve_device_falls_back_to_mdns(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = Device(name="deck", address="192.0.2.10", login="deck", added_via="mdns")
    monkeypatch.setattr(discovery, "resolve_name", lambda core, name, **kw: found)
    resolved = resolve_device(core, "livingroom")
    assert resolved is found


def test_resolve_device_mdns_miss_still_raises_not_found(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery, "resolve_name", lambda core, name, **kw: None)
    with pytest.raises(DevkitError) as exc_info:
        resolve_device(core, "livingroom")
    assert exc_info.value.code == ErrorCode.DEVICE_NOT_FOUND


def test_resolve_device_mdns_error_propagates(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _blocked(core: DevkitCore, name: str, **kw: Any) -> None:
        raise DevkitError(ErrorCode.PERMISSION_LOCAL_NETWORK, "blocked")

    monkeypatch.setattr(discovery, "resolve_name", _blocked)
    with pytest.raises(DevkitError) as exc_info:
        resolve_device(core, "livingroom")
    assert exc_info.value.code == ErrorCode.PERMISSION_LOCAL_NETWORK


@pytest.mark.parametrize("selector", ["192.0.2.55", "deck.local"])
def test_resolve_device_ip_and_hostname_skip_mdns(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch, selector: str
) -> None:
    def _boom(core: DevkitCore, name: str, **kw: Any) -> None:
        raise AssertionError("mDNS fallback must not run for addresses/hostnames")

    monkeypatch.setattr(discovery, "resolve_name", _boom)
    resolved = resolve_device(core, selector)
    assert resolved.added_via == "explicit"


def test_resolve_device_registry_hit_skips_mdns(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    core.state.devices.upsert(
        Device(name="deck", address="192.0.2.10", login="deck", added_via="explicit")
    )
    monkeypatch.setattr(
        discovery,
        "resolve_name",
        lambda core, name, **kw: (_ for _ in ()).throw(AssertionError("no mDNS on registry hit")),
    )
    assert resolve_device(core, "deck").address == "192.0.2.10"


# --- CLI: devkit discover (spec section 10) -------------------------------------


def test_cli_discover_json(monkeypatch: pytest.MonkeyPatch, state_dir: Path) -> None:
    monkeypatch.setattr(
        discovery,
        "discover",
        lambda core, timeout_s: [_discovered("deck"), _discovered("frame", "192.0.2.20")],
    )
    result = runner.invoke(cli.app, ["discover"])
    assert result.exit_code == 0
    payload = jsonlib.loads(result.stdout)  # auto-JSON: CliRunner stdout is not a TTY
    assert [d["name"] for d in payload] == ["deck", "frame"]
    assert payload[0]["via"] == "mdns"


def test_cli_discover_human_on_tty(monkeypatch: pytest.MonkeyPatch, state_dir: Path) -> None:
    monkeypatch.setattr(common, "stdout_is_tty", lambda: True)
    monkeypatch.setattr(discovery, "discover", lambda core, timeout_s: [_discovered("deck")])
    result = runner.invoke(cli.app, ["discover", "--timeout", "0.1"])
    assert result.exit_code == 0
    assert "deck  192.0.2.10:32000  login=deck" in result.stdout
    assert not result.stdout.startswith("[")


def test_cli_discover_empty_human(monkeypatch: pytest.MonkeyPatch, state_dir: Path) -> None:
    monkeypatch.setattr(common, "stdout_is_tty", lambda: True)
    monkeypatch.setattr(discovery, "discover", lambda core, timeout_s: [])
    result = runner.invoke(cli.app, ["discover"])
    assert result.exit_code == 0
    assert "no devices discovered" in result.stdout


def test_cli_discover_permission_error_exit_6(
    monkeypatch: pytest.MonkeyPatch, state_dir: Path
) -> None:
    def _blocked(core: DevkitCore, timeout_s: float) -> list[DiscoveredDevice]:
        raise DevkitError(ErrorCode.PERMISSION_LOCAL_NETWORK, "blocked by TCC")

    monkeypatch.setattr(discovery, "discover", _blocked)
    result = runner.invoke(cli.app, ["discover"])
    assert result.exit_code == 6  # local permission problem (spec section 10)
    payload = jsonlib.loads(result.stdout)
    assert payload["code"] == "PERMISSION_LOCAL_NETWORK"
