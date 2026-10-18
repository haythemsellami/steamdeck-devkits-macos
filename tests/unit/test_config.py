"""Unit tests for steamos_devkit.config (spec section 3)."""

import json
import pickle
import stat
from pathlib import Path

import pytest

from steamos_devkit import constants
from steamos_devkit.config import (
    SCHEMA_VERSION,
    DeviceRegistry,
    Settings,
    StateStore,
    TitleState,
    platform_default_state_dir,
    resolve_state_dir,
)
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Device, TitleConfig


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state")


def test_resolve_state_dir_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "explicit"
    env = tmp_path / "env"
    monkeypatch.setenv(constants.STATE_DIR_ENV_VAR, str(env))
    assert resolve_state_dir(explicit) == explicit
    assert resolve_state_dir() == env
    monkeypatch.delenv(constants.STATE_DIR_ENV_VAR)
    fallback = tmp_path / "adapter-default"
    assert resolve_state_dir(default=fallback) == fallback
    assert resolve_state_dir() == platform_default_state_dir()


def test_ensure_layout_creates_spec_directories(store: StateStore) -> None:
    store.ensure_layout()
    for name in ("keys", "titles", "jobs", "artifacts", "logs"):
        assert (store.root / name).is_dir()
    mode = stat.S_IMODE(store.keys_dir.stat().st_mode)
    assert mode == 0o700


def test_settings_roundtrip_and_atomic_write(store: StateStore) -> None:
    store.ensure_layout()
    assert store.load_settings() == Settings()
    settings = Settings()
    settings.__pydantic_extra__["theme"] = "dark"
    store.save_settings(settings)
    assert store.load_settings() == settings
    # no temp files left behind by the atomic write
    assert list(store.root.glob("*.tmp")) == []


def test_settings_corrupt_raises_state_corrupt(store: StateStore) -> None:
    store.ensure_layout()
    store.settings_path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(DevkitError) as exc_info:
        store.load_settings()
    assert exc_info.value.code == ErrorCode.STATE_CORRUPT


def _device(name: str = "deck", address: str = "192.0.2.10") -> Device:
    return Device(name=name, address=address, login="deck", added_via="explicit")


def test_device_registry_crud(store: StateStore) -> None:
    registry = store.devices
    assert registry.list() == []
    assert registry.get("deck") is None

    registry.upsert(_device())
    registry.upsert(_device("frame", "192.0.2.20"))
    assert [d.name for d in registry.list()] == ["deck", "frame"]

    # upsert replaces by name, does not duplicate
    registry.upsert(_device("deck", "192.0.2.99"))
    assert len(registry.list()) == 2
    assert registry.get("deck").address == "192.0.2.99"  # type: ignore[union-attr]

    assert registry.remove("frame") is True
    assert registry.remove("frame") is False
    assert [d.name for d in registry.list()] == ["deck"]

    # on-disk format: schema_version + device list
    payload = json.loads((store.root / "devices.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["devices"][0]["name"] == "deck"


def test_device_registry_corrupt_raises_state_corrupt(store: StateStore) -> None:
    store.ensure_layout()
    (store.root / "devices.json").write_text('{"schema_version": 999}', encoding="utf-8")
    with pytest.raises(DevkitError) as exc_info:
        store.devices.list()
    assert exc_info.value.code == ErrorCode.STATE_CORRUPT


def test_title_config_store(store: StateStore) -> None:
    assert store.load_title("my.game") is None
    state = TitleState(
        gameid="my.game",
        source_dir=Path("/tmp/build"),
        config=TitleConfig(entrypoint="run.sh", args=["--fast"]),
    )
    store.save_title(state)
    loaded = store.load_title("my.game")
    assert loaded is not None
    assert loaded.config.entrypoint == "run.sh"
    assert loaded.config.args == ["--fast"]
    assert store.delete_title("my.game") is True
    assert store.delete_title("my.game") is False


def test_title_store_rejects_invalid_gameid(store: StateStore) -> None:
    with pytest.raises(DevkitError) as exc_info:
        store.load_title("../escape")
    assert exc_info.value.code == ErrorCode.VALIDATION


def test_last_device_marker(store: StateStore) -> None:
    assert store.last_device is None
    store.set_last_device("deck")
    assert store.last_device == "deck"
    store.clear_last_device()
    assert store.last_device is None
    store.clear_last_device()  # idempotent
    with pytest.raises(DevkitError) as exc_info:
        store.set_last_device("   ")
    assert exc_info.value.code == ErrorCode.VALIDATION


def _write_legacy_keys(legacy_dir: Path) -> None:
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "devkit_rsa").write_bytes(b"PRIVATE-KEY-BYTES")
    (legacy_dir / "devkit_rsa.pub").write_bytes(b"ssh-rsa AAAA devkit-client:test@host")


def test_migration_imports_legacy_keys(store: StateStore, tmp_path: Path) -> None:
    legacy_dir = tmp_path / "legacy"
    _write_legacy_keys(legacy_dir)
    report = store.migrate_legacy(legacy_dir=legacy_dir, pickle_path=tmp_path / "absent.pickle")
    assert report.keys_imported is True
    assert store.key_path.read_bytes() == b"PRIVATE-KEY-BYTES"
    assert stat.S_IMODE(store.key_path.stat().st_mode) == 0o400
    # sources are never deleted
    assert (legacy_dir / "devkit_rsa").exists()


def test_migration_never_overwrites_existing_keys(store: StateStore, tmp_path: Path) -> None:
    legacy_dir = tmp_path / "legacy"
    _write_legacy_keys(legacy_dir)
    store.ensure_layout()
    store.key_path.write_bytes(b"OURS")
    report = store.migrate_legacy(legacy_dir=legacy_dir, pickle_path=tmp_path / "absent.pickle")
    assert report.keys_imported is False
    assert store.key_path.read_bytes() == b"OURS"
    assert any("already exist" in s for s in report.skipped)


def test_migration_pickle_guard(store: StateStore, tmp_path: Path) -> None:
    pickle_path = tmp_path / "settings.pickle"
    pickle_path.write_bytes(pickle.dumps({"Devkit.AddedByIP": {"192.0.2.30"}}))
    report = store.migrate_legacy(legacy_dir=tmp_path / "no-keys", pickle_path=pickle_path)
    assert report.pickle_loaded is False
    assert report.devices_imported == []
    assert store.devices.list() == []
    assert any("not loaded" in s for s in report.skipped)
    assert pickle_path.exists()  # read-only


def test_migration_imports_devices_and_preferred(store: StateStore, tmp_path: Path) -> None:
    pickle_path = tmp_path / "settings.pickle"
    pickle_path.write_bytes(
        pickle.dumps(
            {
                "Devkit.AddedByIP": {"192.0.2.30", "192.0.2.31"},
                "DevkitsWindow.preferred_devkit_name": "192.0.2.31",
            }
        )
    )
    report = store.migrate_legacy(
        legacy_dir=tmp_path / "no-keys", pickle_path=pickle_path, load_pickle=True
    )
    assert report.pickle_loaded is True
    assert report.devices_imported == ["192.0.2.30", "192.0.2.31"]
    devices = {d.name: d for d in store.devices.list()}
    assert devices["192.0.2.30"].added_via == "explicit"
    assert devices["192.0.2.30"].login == constants.SSH_USER_DECK
    assert store.last_device == "192.0.2.31"
    assert report.last_device == "192.0.2.31"
    assert pickle_path.exists()


def test_migration_survives_corrupt_pickle(store: StateStore, tmp_path: Path) -> None:
    pickle_path = tmp_path / "settings.pickle"
    pickle_path.write_bytes(b"not a pickle at all")
    report = store.migrate_legacy(
        legacy_dir=tmp_path / "no-keys", pickle_path=pickle_path, load_pickle=True
    )
    assert report.pickle_loaded is False
    assert any("could not parse" in s for s in report.skipped)


def test_migration_reports_missing_sources(store: StateStore, tmp_path: Path) -> None:
    report = store.migrate_legacy(
        legacy_dir=tmp_path / "nothing", pickle_path=tmp_path / "nothing.pickle"
    )
    assert report.keys_imported is False
    assert report.devices_imported == []
    assert report.sources_found == []
    assert any("no legacy devkit key" in s for s in report.skipped)
    assert any("no legacy GUI settings" in s for s in report.skipped)


def test_registry_is_devices_json_only(store: StateStore) -> None:
    assert isinstance(store.devices, DeviceRegistry)
    store.devices.upsert(_device())
    again = StateStore(store.root)
    assert again.devices.get("deck") is not None
