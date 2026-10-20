"""Typed data models (spec section 5).

All models serialize to JSON; field names are snake_case; timestamps are
ISO-8601 UTC. Job ids are ``job_<ulid>`` and artifact ids ``art_<ulid>``
(ULID via ulid-py).
"""

import warnings
from datetime import datetime
from enum import StrEnum
from ipaddress import IPv4Address
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from steamos_devkit.constants import (
    DEVICE_HTTP_PORT_DEFAULT,
    GAMEID_REGEX,
    CompatToolAlias,
)
from steamos_devkit.errors import ErrorCode

DeviceSelector = str
"""Registry name, IPv4 literal, or DNS hostname; the CLI defaults to last_device."""

GameId = Annotated[str, StringConstraints(pattern=GAMEID_REGEX)]
"""Validated title identifier (spec section 4). Reserved gameids
(:data:`steamos_devkit.constants.RESERVED_GAMEIDS`) are valid identifiers but
are rejected at deploy time with UNSUPPORTED."""

ScreenshotMode = Literal[1, 2, 3, 4]
"""Gamescope screenshot modes (spec section 4)."""

LogKind = Literal["steam", "proton", "dumps", "frametimes"]

_ULID = r"[0-9A-HJKMNP-TV-Z]{26}"  # Crockford base32, as produced by ulid-py
_JOB_ID = rf"^job_{_ULID}$"
_ARTIFACT_ID = rf"^art_{_ULID}$"
_SHA256 = r"^[0-9a-f]{64}$"


class DiscoveredDevice(BaseModel):
    """A device found via mDNS or added explicitly, before registration."""

    name: str
    address: IPv4Address
    port: int = DEVICE_HTTP_PORT_DEFAULT
    login: str | None = None
    sshd: bool | None = None
    via: Literal["mdns", "explicit"]


class Device(BaseModel):
    """Registry entry for a paired or explicitly added device."""

    name: str
    address: str
    port: int = DEVICE_HTTP_PORT_DEFAULT
    login: str
    family: Literal["deck", "frame", "unknown"] = "unknown"
    paired_at: datetime | None = None
    added_via: Literal["mdns", "explicit"]
    last_seen: datetime | None = None


class DeviceStatus(BaseModel):
    """Parsed ``steamos-get-status --json`` output (audit section 4)."""

    hostname: str
    os_name: str
    os_version: str
    is_deckard: bool
    session_status: str
    steam_status: Literal["NOT_RUNNING", "ERROR", "OS", "OS_DEV", "SIDELOADED", "UNKNOWN"]
    steam_osclient_branch: str | None = None
    steam_osclient_version: int | str | None = None
    cef_debugging_enabled: bool
    renderdoc_layer_enabled: bool
    renderdoc_replay_server_running: bool
    user_password_is_set: bool
    steam_default_args: list[str] = Field(default_factory=list)
    steam_current_args: list[str] = Field(default_factory=list)
    steam_launch_flags: dict[str, str] = Field(default_factory=dict)
    raw: dict[str, object] = Field(default_factory=dict)
    """Unparsed extras from the device response, preserved verbatim."""

    @field_validator("steam_status", mode="before")
    @classmethod
    def _strip_enum_prefix(cls, v: object) -> object:
        # Device sends str(enum), e.g. "SteamStatus.OS" (steamos-get-status:409).
        if isinstance(v, str) and "." in v:
            return v.rsplit(".", 1)[1]
        return v


class Title(BaseModel):
    """A deployed devkit title, from ``steamos-list-games`` plus sidecars."""

    gameid: GameId
    # None: registration is not knowable from the device helpers — they list
    # deployed directories; Steam registration lives behind the Steam pipe's
    # list-shortcuts (devkit_utils/resolve.py), which they never call.
    registered_with_steam: bool | None = None
    size_bytes: int | None = None
    modified: datetime | None = None


class TitleConfig(BaseModel):
    """Launch configuration for a title (spec section 5)."""

    entrypoint: str | None = None  # argv[0], relative to the game dir
    args: list[str] = Field(default_factory=list)  # appended to argv
    env: dict[str, str] = Field(default_factory=dict)
    runtime: CompatToolAlias | None = None
    settings: dict[str, str] = Field(default_factory=dict)  # steam_play, gdbserver, user keys...


class TitleDetail(Title):
    """A title together with its parsed on-device launch configuration."""

    config: TitleConfig = Field(default_factory=TitleConfig)
    remote_dir: str | None = None


class FilterSpec(BaseModel):
    """rsync filter selection for deploys."""

    mode: Literal["exclude", "include", "raw"] = "exclude"
    patterns: list[str] = Field(default_factory=list)


# pydantic 2.14 added BaseModel.register(); the spec-frozen field name
# DeploySpec.register (spec section 5) shadows it. The name is contractual, so
# the import-time shadow warning is suppressed exactly at this class.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message='Field name "register" in "DeploySpec" shadows an attribute',
        category=UserWarning,
    )

    class DeploySpec(BaseModel):
        """Inputs for one deploy (spec sections 5 and 8)."""

        device: DeviceSelector
        gameid: GameId
        source_dir: Path
        filters: FilterSpec = Field(default_factory=FilterSpec)
        clean: bool = False  # --delete --delete-excluded --delete-delay
        checksum: bool = False  # --checksum (mutually exclusive with update)
        update: bool = False  # --update
        compress: bool = True  # -z
        register: bool = True  # run steam-client-create-shortcut after upload
        launch: bool = False  # run-game after successful register
        config: TitleConfig | None = None  # applied before register

        @model_validator(mode="after")
        def _checksum_and_update_are_exclusive(self) -> "DeploySpec":
            if self.checksum and self.update:
                raise ValueError("checksum and update are mutually exclusive")
            return self


class DeployPreview(BaseModel):
    """Result of ``rsync -n --itemize-changes`` (never mutates the device)."""

    files_added: int
    files_changed: int
    files_deleted: int
    bytes_to_transfer: int
    sample_changes: list[str] = Field(default_factory=list)  # at most 200 itemized lines
    warnings: list[str] = Field(default_factory=list)


class Artifact(BaseModel):
    """A captured payload stored under the state root with a meta sidecar."""

    id: str = Field(pattern=_ARTIFACT_ID)
    job_id: str = Field(pattern=_JOB_ID)
    device: str
    kind: Literal[
        "logs",
        "proton_log",
        "screenshot",
        "controller_config",
        "gpu_trace",
        "rgp",
        "frametimes",
        "dumps",
    ]
    path: Path
    bytes: int
    sha256: str = Field(pattern=_SHA256)
    media_type: str | None = None
    remote_source: str
    created_at: datetime


class LaunchResult(BaseModel):
    """Outcome of a title launch (spec section 5)."""

    gameid: GameId
    launched: bool
    device_response: str = ""
    readiness: Literal["unknown"] = "unknown"  # no upstream readiness signal (audit section 7)


# --- Host adapter models (spec section 12) -----------------------------------

HostContext = Literal["cli", "bundled_app", "mcp_stdio"]
"""Process context detected by the host adapter (spec section 12)."""

RsyncFlavor = Literal["rsync", "openrsync", "unknown"]
RsyncSource = Literal["env", "bundled", "homebrew", "cygwin", "path", "none"]


class RsyncInfo(BaseModel):
    """Probe result for one rsync candidate (spec sections 9 and 12)."""

    path: Path | None = None
    flavor: RsyncFlavor | None = None
    version: str | None = None
    protocol_version: int | None = None
    source: RsyncSource = "none"
    ok: bool = False
    """True only for real rsync with protocol >= 30; openrsync never qualifies."""
    message: str | None = None


class ToolAvailability(BaseModel):
    """Aggregate host dependency probe (spec section 12)."""

    rsync: RsyncInfo
    ssh_path: Path | None = None
    ssh_version: str | None = None
    terminal: str | None = None
    """Terminal emulator used for interactive shells, e.g. "Terminal.app"."""
    filezilla: Path | None = None


# --- Capabilities (spec section 9) --------------------------------------------


class HostCapabilities(BaseModel):
    """Everything the host can do, built from probes (never hardcoded)."""

    platform: str
    arch: str
    context: HostContext
    python_version: str
    state_dir: Path
    state_dir_writable: bool
    rsync: RsyncInfo
    ssh_path: Path | None = None
    ssh_version: str | None = None
    terminal: str | None = None
    local_network: Literal["unknown", "likely_ok", "blocked"] = "unknown"
    """Local Network (TCC) is not programmatically readable; inferred at best."""


class DeviceCapabilities(BaseModel):
    """Device feature probes from steamos-get-status plus feature checks."""

    family: Literal["deck", "frame", "unknown"] = "unknown"
    os_version: str | None = None
    steam_branch: str | None = None
    steam_version: int | None = None
    has_gpu_trace: bool | None = None
    gamescope_version: str | None = None  # screenshot modes need gamescope >= 3.13
    renderdoc_layer_enabled: bool | None = None
    renderdoc_replay_server_running: bool | None = None
    cef_debugging_enabled: bool | None = None
    has_mangohudctl: bool | None = None
    session_select: Literal["holo-session-select", "steamos-session-select", "unknown"] = "unknown"


class OperationCapability(BaseModel):
    """Support status of one CLI command / MCP tool (spec section 9)."""

    status: Literal[
        "supported_tested",
        "supported_untested",
        "dependency_required",
        "capture_export_only",
        "unavailable_host",
        "unavailable_device",
        "unsupported_upstream",
    ]
    reason: str | None = None
    dependency: str | None = None


class Capabilities(BaseModel):
    """Host plus optional device capabilities, and the per-operation matrix."""

    host: HostCapabilities
    device: DeviceCapabilities | None = None
    operations: dict[str, OperationCapability] = Field(default_factory=dict)


# --- Result envelope (spec section 5) -----------------------------------------


class Result(BaseModel):
    """Envelope for every core call that does not start a job.

    ``code`` is None exactly when ``ok`` is true. Deploy distinguishes upload,
    registration, and launch per the overview.
    """

    ok: bool
    code: ErrorCode | None = None
    device: Device | None = None
    message: str | None = None
    upload_complete: bool | None = None
    registration_complete: bool | None = None
    launch: LaunchResult | None = None

    @classmethod
    def success(cls, *, device: Device | None = None, message: str | None = None) -> "Result":
        return cls(ok=True, device=device, message=message)

    @classmethod
    def failure(
        cls,
        code: ErrorCode,
        message: str,
        *,
        device: Device | None = None,
    ) -> "Result":
        return cls(ok=False, code=code, device=device, message=message)


# --- Jobs (spec section 7) -----------------------------------------------------


class JobKind(StrEnum):
    """Long-running operation kinds (spec section 7.4)."""

    DEPLOY = "deploy"
    FETCH_LOGS = "fetch_logs"
    SCREENSHOT = "screenshot"
    CONTROLLER_CONFIG = "controller_config"
    GPU_TRACE = "gpu_trace"
    RGP = "rgp"


class JobState(StrEnum):
    """queued -> running -> one of the terminal states (spec section 7.1)."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    TIMEOUT = "timeout"

    @property
    def is_terminal(self) -> bool:
        return self in {
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.INTERRUPTED,
            JobState.TIMEOUT,
        }


class JobProgress(BaseModel):
    """Progress snapshot from a journal ``progress`` event (spec section 7.1)."""

    phase: str
    bytes_done: int | None = None
    bytes_total: int | None = None
    items_done: int | None = None
    rate_bps: float | None = None


class JobInfo(BaseModel):
    """Public view of a job, derived from its journal (spec section 7.1)."""

    id: str = Field(pattern=_JOB_ID)
    kind: JobKind
    state: JobState
    device: str | None = None
    gameid: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    timeout_s: int | None = None
    progress: JobProgress | None = None
    code: ErrorCode | None = None
    message: str | None = None
    artifacts: list[str] = Field(default_factory=list)  # artifact ids


# --- Diagnostics ---------------------------------------------------------------


class SetupStep(BaseModel):
    """One onboarding step outcome (`devkit setup`)."""

    name: str
    status: Literal["ok", "skipped", "warning", "error"]
    summary: str


class SetupReport(BaseModel):
    """Result of `devkit setup` — ok when no step errored."""

    ok: bool
    steps: list[SetupStep] = Field(default_factory=list)


class DoctorCheck(BaseModel):
    """One doctor() finding."""

    name: str
    status: Literal["ok", "warning", "error", "unknown"]
    summary: str
    details: dict[str, str] = Field(default_factory=dict)


class DoctorReport(BaseModel):
    """Full host report from DevkitCore.doctor() (spec section 15, M0)."""

    ok: bool  # no check in error state
    context: HostContext
    platform: str
    arch: str
    python_version: str
    state_dir: Path
    tools: ToolAvailability
    checks: list[DoctorCheck] = Field(default_factory=list)
