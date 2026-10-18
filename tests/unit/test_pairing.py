"""Unit tests for pairing: key management, properties fetch, register flow.

All HTTP goes through the ``pairing._urlopen`` seam — no real sockets.
"""

from __future__ import annotations

import io
import json
import os
import stat
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from steamos_devkit import constants, pairing
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Device

PROPERTIES = Path(__file__).resolve().parent.parent / "fixtures" / "properties.json"
DECLINE_BODY = Path(__file__).resolve().parent.parent / "fixtures" / "pairing-403-body.txt"


@pytest.fixture
def core(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> DevkitCore:
    monkeypatch.setattr("getpass.getuser", lambda: "testuser")
    monkeypatch.setattr("socket.gethostname", lambda: "testhost")
    return DevkitCore(state_dir=state_dir)


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://192.0.2.10:32000/register", code, "error", hdrs=None, fp=io.BytesIO(body)
    )


# --- ensure_key ---------------------------------------------------------------


def test_ensure_key_generates_rsa_keypair(core: DevkitCore) -> None:
    key_path = pairing.ensure_key(core)
    assert key_path == core.state.key_path
    assert key_path.exists()
    mode = stat.S_IMODE(os.stat(key_path).st_mode)
    assert mode == 0o400
    pub = core.state.pubkey_path.read_text(encoding="utf-8")
    assert pub.startswith("ssh-rsa ")
    assert pub.endswith(" devkit-client:testuser@testhost\n")


def test_ensure_key_is_stable_across_calls(core: DevkitCore) -> None:
    first = core.state.key_path
    pairing.ensure_key(core)
    original = first.read_bytes()
    pairing.ensure_key(core)
    assert first.read_bytes() == original  # generated once, then reused


def test_ensure_key_never_overwrites_existing_key(core: DevkitCore) -> None:
    core.state.ensure_layout()
    core.state.key_path.write_bytes(b"pre-existing key material")
    pairing.ensure_key(core)
    assert core.state.key_path.read_bytes() == b"pre-existing key material"


# --- fetch_properties -----------------------------------------------------------


def test_fetch_properties_parses_settings_and_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fake_urlopen(request: Any, timeout: float) -> bytes:
        seen.append(request.full_url)
        return PROPERTIES.read_bytes()

    monkeypatch.setattr(pairing, "_urlopen", fake_urlopen)
    props = pairing.fetch_properties("192.0.2.10", 32000, timeout=5)
    assert props["login"] == "deck"
    assert props["settings"] == {"sshd": "1"}
    assert props["sshd"] is True
    assert seen == ["http://192.0.2.10:32000/properties.json"]


def test_fetch_properties_legacy_login_name_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> bytes:
        if request.full_url.endswith("/properties.json"):
            raise _http_error(404, b"not found")
        if request.full_url.endswith("/login-name"):
            return b"steamos\n"
        raise AssertionError(request.full_url)

    monkeypatch.setattr(pairing, "_urlopen", fake_urlopen)
    props = pairing.fetch_properties("192.0.2.10", 32000, timeout=5)
    assert props == {"login": "steamos", "settings": {}, "sshd": None}


def test_fetch_properties_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> bytes:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(pairing, "_urlopen", fake_urlopen)
    with pytest.raises(DevkitError) as exc_info:
        pairing.fetch_properties("192.0.2.10", 32000, timeout=5)
    assert exc_info.value.code == ErrorCode.DEVICE_UNREACHABLE


def test_fetch_properties_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> bytes:
        raise TimeoutError("timed out")

    monkeypatch.setattr(pairing, "_urlopen", fake_urlopen)
    with pytest.raises(DevkitError) as exc_info:
        pairing.fetch_properties("192.0.2.10", 32000, timeout=5)
    assert exc_info.value.code == ErrorCode.TIMEOUT


def test_urlopen_uses_no_proxy_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("http_proxy", "http://proxy.example:8080")
    captured: dict[str, Any] = {}
    real_build_opener = urllib.request.build_opener

    def spy(*handlers: Any) -> Any:
        captured["handlers"] = handlers
        return real_build_opener(*handlers)

    monkeypatch.setattr(urllib.request, "build_opener", spy)
    pairing._no_proxy_opener()
    (handler,) = [
        h for h in captured["handlers"] if isinstance(h, urllib.request.ProxyHandler)
    ]
    assert handler.proxies == {}  # env proxies are stripped for LAN requests
    # and the resulting opener therefore registers no proxy scheme handlers
    opener = pairing._no_proxy_opener()
    assert not any(isinstance(h, urllib.request.ProxyHandler) for h in opener.handlers)


# --- pair ---------------------------------------------------------------------


class FakeHTTP:
    """Scriptable _urlopen replacement capturing requests."""

    def __init__(self, register_response: bytes | Exception = b"") -> None:
        self.register_response = register_response
        self.register_request: Any = None

    def __call__(self, request: Any, timeout: float) -> bytes:
        url = request.full_url
        if url.endswith("/properties.json"):
            return PROPERTIES.read_bytes()
        if url.endswith("/register"):
            self.register_request = request
            if isinstance(self.register_response, Exception):
                raise self.register_response
            return self.register_response
        raise AssertionError(url)


def test_pair_success_registers_and_marks_last_device(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = FakeHTTP()
    monkeypatch.setattr(pairing, "_urlopen", http)
    result = pairing.pair(core, "192.0.2.99", timeout_s=30)
    assert result.ok is True
    assert result.message == "paired"
    device = result.device
    assert device is not None
    assert device.name == "192.0.2.99"
    assert device.address == "192.0.2.99"
    assert device.login == "deck"  # from properties.json
    assert device.paired_at is not None
    assert device.last_seen is not None
    assert device.added_via == "explicit"
    # registry + last-device marker
    assert core.state.devices.get("192.0.2.99") is not None
    assert core.state.last_device == "192.0.2.99"
    # POST body: pubkey line + magic phrase, text/plain (upstream :1109-1121)
    request = http.register_request
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "text/plain"
    body = request.data.decode("ascii")
    pub = core.state.pubkey_path.read_text(encoding="utf-8").strip("\n")
    assert body == f"{pub} {pairing.MAGIC_PHRASE}\n"
    assert body.startswith("ssh-rsa ")
    # the phrase travels in the request body only — never in the result
    assert pairing.MAGIC_PHRASE not in result.model_dump_json()


def test_pair_rekeys_only_once(core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pairing, "_urlopen", FakeHTTP())
    pairing.pair(core, "192.0.2.99")
    first_key = core.state.key_path.read_bytes()
    pairing.pair(core, "192.0.2.100")
    assert core.state.key_path.read_bytes() == first_key
    assert core.state.last_device == "192.0.2.100"


def test_pair_declined_403_parses_mixed_body(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = FakeHTTP(register_response=_http_error(403, DECLINE_BODY.read_bytes()))
    monkeypatch.setattr(pairing, "_urlopen", http)
    result = pairing.pair(core, "192.0.2.10")
    assert result.ok is False
    assert result.code == ErrorCode.PAIRING_DECLINED
    assert result.message == "Pairing request was declined on the device."
    assert core.state.devices.list() == []


def test_pair_declined_403_without_json_line(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = FakeHTTP(register_response=_http_error(403, b"ssh-approve-key: nope\nplain text\n"))
    monkeypatch.setattr(pairing, "_urlopen", http)
    result = pairing.pair(core, "192.0.2.10")
    assert result.code == ErrorCode.PAIRING_DECLINED
    assert result.message == "pairing was declined on the device"


def test_pair_http_error_other_than_403(core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHTTP(register_response=_http_error(500, b"boom"))
    monkeypatch.setattr(pairing, "_urlopen", http)
    result = pairing.pair(core, "192.0.2.10")
    assert result.code == ErrorCode.PAIRING_FAILED
    assert "HTTP 500" in (result.message or "")


def test_pair_register_timeout(core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHTTP(register_response=TimeoutError("timed out"))
    monkeypatch.setattr(pairing, "_urlopen", http)
    result = pairing.pair(core, "192.0.2.10", timeout_s=120)
    assert result.code == ErrorCode.PAIRING_TIMEOUT
    assert "120" in (result.message or "")


def test_pair_register_urlerror_timeout(core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHTTP(register_response=urllib.error.URLError(TimeoutError("timed out")))
    monkeypatch.setattr(pairing, "_urlopen", http)
    result = pairing.pair(core, "192.0.2.10")
    assert result.code == ErrorCode.PAIRING_TIMEOUT


def test_pair_register_unreachable(core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHTTP(register_response=urllib.error.URLError("connection refused"))
    monkeypatch.setattr(pairing, "_urlopen", http)
    result = pairing.pair(core, "192.0.2.10")
    assert result.code == ErrorCode.DEVICE_UNREACHABLE


def test_pair_properties_timeout_maps_to_pairing_timeout(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_urlopen(request: Any, timeout: float) -> bytes:
        raise TimeoutError("timed out")

    monkeypatch.setattr(pairing, "_urlopen", fake_urlopen)
    result = pairing.pair(core, "192.0.2.10")
    assert result.code == ErrorCode.PAIRING_TIMEOUT


def test_pair_uses_registry_device_name_and_port(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    core.state.devices.upsert(
        Device(name="deck", address="192.0.2.10", port=32001, login="deck", added_via="mdns")
    )
    http = FakeHTTP()
    monkeypatch.setattr(pairing, "_urlopen", http)
    result = pairing.pair(core, "deck")
    assert result.ok is True
    assert result.device is not None
    assert result.device.name == "deck"
    assert result.device.port == 32001
    assert http.register_request.full_url == "http://192.0.2.10:32001/register"


def test_pair_unknown_device_raises(core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("steamos_devkit.discovery.resolve_name", lambda core, name, **kw: None)
    with pytest.raises(DevkitError) as exc_info:
        pairing.pair(core, "livingroom")
    assert exc_info.value.code == ErrorCode.DEVICE_NOT_FOUND


def test_parse_decline_reason_directly() -> None:
    body = DECLINE_BODY.read_bytes()
    assert pairing._parse_decline_reason(body) == "Pairing request was declined on the device."
    assert pairing._parse_decline_reason(b'{"other": 1}\n') == "pairing was declined on the device"
    assert pairing._parse_decline_reason(b"") == "pairing was declined on the device"
