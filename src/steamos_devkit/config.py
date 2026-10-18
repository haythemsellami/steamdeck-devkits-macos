"""State root, settings, device registry, per-title configs, migration (spec section 3).

All state files are JSON (UTF-8), one object per file, written atomically.
The legacy pickle is parsed read-only, only behind an explicit flag — pickle
loading is code execution and never happens automatically.
"""

from __future__ import annotations

import json
import logging
import os
import pickle  # legacy-GUI import only, inside migrate_legacy(); never automatic
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import appdirs
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from steamos_devkit import constants
from steamos_devkit.constants import GAMEID_REGEX
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Device, FilterSpec, GameId, TitleConfig

logger = logging.getLogger(__name__)

SCHEMA_VERSION: int = 1


class Settings(BaseModel):
    """App-level preferences (replaces upstream's pickle); extra keys pass through."""

    model_config = ConfigDict(extra="allow")

    schema_version: int = SCHEMA_VERSION


class TitleState(BaseModel):
    """Per-title deploy config stored at ``titles/<gameid>.json`` (spec section 3)."""

    gameid: GameId
    source_dir: Path | None = None
    filters: FilterSpec = Field(default_factory=FilterSpec)
    config: TitleConfig = Field(default_factory=TitleConfig)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MigrationReport(BaseModel):
    """Outcome of a legacy-state import; sources are never modified."""

    sources_found: list[Path] = Field(default_factory=list)
    keys_imported: bool = False
    devices_imported: list[str] = Field(default_factory=list)
    last_device: str | None = None
    pickle_loaded: bool = False
    skipped: list[str] = Field(default_factory=list)


def platform_default_state_dir() -> Path:
    """Platform-conventional state root via appdirs (spec section 3).

    macOS: ``~/Library/Application Support/steamos-devkit``; Linux keeps the
    upstream path ``~/.config/steamos-devkit`` so dual-use machines stay
    compatible.
    """
    return Path(appdirs.user_config_dir(constants.APP_NAME))


def resolve_state_dir(explicit: Path | None = None, *, default: Path | None = None) -> Path:
    """Resolution order: explicit argument, ``DEVKIT_STATE_DIR``, platform default."""
    if explicit is not None:
        return explicit.expanduser()
    env = os.environ.get(constants.STATE_DIR_ENV_VAR)
    if env:
        return Path(env).expanduser()
    if default is not None:
        return default
    return platform_default_state_dir()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, model: BaseModel) -> None:
    _atomic_write(path, model.model_dump_json(indent=2) + "\n")


def _read_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise DevkitError(
            ErrorCode.STATE_CORRUPT,
            f"cannot read state file {path}: {e}",
            details={"path": str(path)},
        ) from e


class DeviceRegistry:
    """CRUD over ``devices.json`` (spec section 3)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def list(self) -> list[Device]:
        return self._load()

    def get(self, name: str) -> Device | None:
        for device in self._load():
            if device.name == name:
                return device
        return None

    def upsert(self, device: Device) -> None:
        devices = [d for d in self._load() if d.name != device.name]
        devices.append(device)
        self._save(devices)

    def remove(self, name: str) -> bool:
        devices = self._load()
        kept = [d for d in devices if d.name != name]
        if len(kept) == len(devices):
            return False
        self._save(kept)
        return True

    def _load(self) -> list[Device]:
        if not self._path.exists():
            return []
        data = _read_json_file(self._path)
        try:
            if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("unknown devices.json schema")
            return [Device.model_validate(d) for d in data["devices"]]
        except (ValueError, KeyError, TypeError, ValidationError) as e:
            raise DevkitError(
                ErrorCode.STATE_CORRUPT,
                f"invalid device registry {self._path}: {e}",
                details={"path": str(self._path)},
            ) from e

    def _save(self, devices: list[Device]) -> None:
        payload = {"schema_version": SCHEMA_VERSION, "devices": [d.model_dump(mode="json") for d in devices]}
        _atomic_write(self._path, json.dumps(payload, indent=2) + "\n")


class StateStore:
    """Rooted view over the whole state layout (spec section 3)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    # -- layout ---------------------------------------------------------------

    @property
    def keys_dir(self) -> Path:
        return self.root / constants.KEYS_DIRNAME

    @property
    def titles_dir(self) -> Path:
        return self.root / constants.TITLES_DIRNAME

    @property
    def jobs_dir(self) -> Path:
        return self.root / constants.JOBS_DIRNAME

    @property
    def artifacts_dir(self) -> Path:
        return self.root / constants.ARTIFACTS_DIRNAME

    @property
    def logs_dir(self) -> Path:
        return self.root / constants.LOGS_DIRNAME

    @property
    def key_path(self) -> Path:
        return self.keys_dir / constants.KEY_FILENAME

    @property
    def pubkey_path(self) -> Path:
        return self.keys_dir / constants.PUBKEY_FILENAME

    def ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in (self.keys_dir, self.titles_dir, self.jobs_dir, self.artifacts_dir, self.logs_dir):
            directory.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(self.keys_dir, 0o700)

    # -- settings ---------------------------------------------------------------

    @property
    def settings_path(self) -> Path:
        return self.root / constants.SETTINGS_FILENAME

    def load_settings(self) -> Settings:
        if not self.settings_path.exists():
            return Settings()
        data = _read_json_file(self.settings_path)
        try:
            return Settings.model_validate(data)
        except ValidationError as e:
            raise DevkitError(
                ErrorCode.STATE_CORRUPT,
                f"invalid settings {self.settings_path}: {e}",
                details={"path": str(self.settings_path)},
            ) from e

    def save_settings(self, settings: Settings) -> None:
        _atomic_write_json(self.settings_path, settings)

    # -- device registry ---------------------------------------------------------

    @property
    def devices(self) -> DeviceRegistry:
        return DeviceRegistry(self.root / constants.DEVICES_FILENAME)

    # -- per-title config ----------------------------------------------------------

    def _title_path(self, gameid: str) -> Path:
        if not re.fullmatch(GAMEID_REGEX, gameid):
            raise DevkitError(ErrorCode.VALIDATION, f"invalid gameid: {gameid!r}")
        return self.titles_dir / f"{gameid}.json"

    def load_title(self, gameid: str) -> TitleState | None:
        path = self._title_path(gameid)
        if not path.exists():
            return None
        data = _read_json_file(path)
        try:
            return TitleState.model_validate(data)
        except ValidationError as e:
            raise DevkitError(
                ErrorCode.STATE_CORRUPT,
                f"invalid title config {path}: {e}",
                details={"path": str(path)},
            ) from e

    def save_title(self, state: TitleState) -> None:
        state.updated_at = datetime.now(UTC)
        _atomic_write_json(self._title_path(state.gameid), state)

    def delete_title(self, gameid: str) -> bool:
        try:
            self._title_path(gameid).unlink()
        except FileNotFoundError:
            return False
        return True

    # -- last-device marker ---------------------------------------------------------

    @property
    def last_device_path(self) -> Path:
        return self.root / constants.LAST_DEVICE_FILENAME

    @property
    def last_device(self) -> str | None:
        try:
            name = self.last_device_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError as e:
            raise DevkitError(
                ErrorCode.STATE_CORRUPT,
                f"cannot read {self.last_device_path}: {e}",
                details={"path": str(self.last_device_path)},
            ) from e
        return name or None

    def set_last_device(self, name: str) -> None:
        if not name.strip():
            raise DevkitError(ErrorCode.VALIDATION, "last_device name must be non-empty")
        _atomic_write(self.last_device_path, name.strip() + "\n")

    def clear_last_device(self) -> None:
        try:
            self.last_device_path.unlink()
        except FileNotFoundError:
            pass

    # -- legacy migration ---------------------------------------------------------

    def migrate_legacy(
        self,
        *,
        load_pickle: bool = False,
        legacy_dir: Path | None = None,
        pickle_path: Path | None = None,
    ) -> MigrationReport:
        """One-way import of upstream state (spec section 3).

        Reads upstream's config dir (``~/.config/steamos-devkit/`` on Linux; on
        macOS the appdirs dir is shared with our state root, so keys may sit at
        its top level) and — only with ``load_pickle=True`` — the legacy GUI
        pickle. Sources are never deleted or modified; existing state of ours
        is never overwritten.
        """
        report = MigrationReport()
        candidates: list[Path] = []
        for candidate in (
            legacy_dir if legacy_dir is not None else platform_default_state_dir(),
            self.root,
        ):
            if candidate not in candidates:
                candidates.append(candidate)

        self._migrate_keys(candidates, report)
        self._migrate_pickle(
            pickle_path if pickle_path is not None else Path(constants.LEGACY_GUI_PICKLE).expanduser(),
            load_pickle=load_pickle,
            report=report,
        )
        return report

    def _migrate_keys(self, candidates: list[Path], report: MigrationReport) -> None:
        if self.key_path.exists():
            report.skipped.append(f"keys already exist at {self.key_path}")
            return
        for directory in candidates:
            legacy_key = directory / constants.KEY_FILENAME
            legacy_pub = directory / constants.PUBKEY_FILENAME
            if not legacy_key.exists() or legacy_key == self.key_path:
                continue
            report.sources_found.append(legacy_key)
            self.ensure_layout()
            for src, dst in ((legacy_key, self.key_path), (legacy_pub, self.pubkey_path)):
                if src.exists():
                    dst.write_bytes(src.read_bytes())
                    if sys.platform != "win32":
                        os.chmod(dst, 0o400)
            report.keys_imported = True
            logger.info("imported legacy devkit key from %s", legacy_key)
            return
        report.skipped.append("no legacy devkit key found")

    def _migrate_pickle(self, path: Path, *, load_pickle: bool, report: MigrationReport) -> None:
        if not path.exists():
            report.skipped.append(f"no legacy GUI settings at {path}")
            return
        report.sources_found.append(path)
        if not load_pickle:
            report.skipped.append(
                f"legacy GUI pickle {path} present but not loaded "
                "(pass load_pickle=True / use the migrate command)"
            )
            return
        # The only pickle load in the codebase: explicit, read-only, guarded.
        try:
            with path.open("rb") as f:
                data: Any = pickle.load(f)
        except Exception as e:
            report.skipped.append(f"could not parse legacy pickle {path}: {e}")
            return
        report.pickle_loaded = True
        if not isinstance(data, dict):
            report.skipped.append(f"legacy pickle {path} did not contain a settings dict")
            return

        added = data.get(constants.LEGACY_PICKLE_ADDED_BY_IP_KEY, set())
        if not isinstance(added, set | frozenset):
            report.skipped.append(f"{constants.LEGACY_PICKLE_ADDED_BY_IP_KEY} is not an IP set")
            added = set()
        registry = self.devices
        for ip in sorted(str(a) for a in added):
            if registry.get(ip) is not None:
                report.skipped.append(f"device {ip} already registered")
                continue
            registry.upsert(
                Device(
                    name=ip,
                    address=ip,
                    login=constants.SSH_USER_DECK,
                    added_via="explicit",
                )
            )
            report.devices_imported.append(ip)

        preferred = data.get(constants.LEGACY_PICKLE_PREFERRED_DEVICE_KEY)
        if isinstance(preferred, str) and preferred:
            known = {d.name for d in registry.list()}
            if preferred in known and self.last_device is None:
                self.set_last_device(preferred)
                report.last_device = preferred
            else:
                report.skipped.append(f"preferred device {preferred!r} not in registry or already set")
