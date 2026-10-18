"""Unit tests for steamos_devkit.models (spec section 5)."""

from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar, get_args

import pytest
from pydantic import BaseModel, ValidationError

from steamos_devkit import constants
from steamos_devkit.constants import COMPAT_TOOL_ALIASES, RESERVED_GAMEIDS
from steamos_devkit.errors import ErrorCode
from steamos_devkit.models import (
    Artifact,
    Capabilities,
    DeployPreview,
    DeploySpec,
    Device,
    DeviceCapabilities,
    DeviceStatus,
    DiscoveredDevice,
    GameId,
    HostCapabilities,
    JobInfo,
    JobKind,
    JobProgress,
    JobState,
    LaunchResult,
    OperationCapability,
    Result,
    RsyncInfo,
    Title,
    TitleConfig,
    TitleDetail,
    ToolAvailability,
)

JOB_ID = "job_01ARZ3NDEKTSV4RRFFQ69G5FAV"
ART_ID = "art_01ARZ3NDEKTSV4RRFFQ69G5FAV"
NOW = datetime(2026, 10, 19, tzinfo=UTC)

_M = TypeVar("_M", bound=BaseModel)


def _roundtrip(model_class: type[_M], instance: _M) -> None:
    payload = instance.model_dump_json()
    assert model_class.model_validate_json(payload) == instance


def test_gameid_accepts_valid() -> None:
    for good in ("my.game_1", "_x", "A1", "a.b.c", "Game9"):
        # note: the spec section 4 regex requires >= 2 characters (upstream verbatim)
        Title(gameid=good, registered_with_steam=False)


def test_gameid_rejects_invalid() -> None:
    for bad in ("1abc", "has space", "with/slash", "with-dash", "", "../escape"):
        with pytest.raises(ValidationError):
            Title(gameid=bad, registered_with_steam=False)


def test_reserved_gameids_are_valid_identifiers() -> None:
    # reserved ids parse as GameId; deploy-time rejection is a core concern
    for gameid in RESERVED_GAMEIDS:
        Title(gameid=gameid, registered_with_steam=False)
    assert RESERVED_GAMEIDS == {"steam", "steamdeckard", "steamvr", "steamvrdeckard"}


def test_compat_tool_alias_literal_matches_constant() -> None:
    assert set(get_args(constants.CompatToolAlias)) == set(COMPAT_TOOL_ALIASES)
    assert COMPAT_TOOL_ALIASES == {
        "proton-stable",
        "proton-experimental",
        "SteamLinuxRuntime",
        "SteamLinuxRuntime_sniper",
        "lepton",
        "SteamLinuxRuntime_4-arm64",
        "SteamLinuxRuntime_4",
    }


def test_discovered_device_roundtrip() -> None:
    device = DiscoveredDevice(
        name="deck", address="192.0.2.10", login="deck", sshd=True, via="mdns"
    )
    assert device.port == constants.DEVICE_HTTP_PORT_DEFAULT
    assert str(device.address) == "192.0.2.10"
    _roundtrip(DiscoveredDevice, device)


def test_device_roundtrip() -> None:
    device = Device(
        name="deck",
        address="192.0.2.10",
        login="deck",
        family="deck",
        paired_at=NOW,
        added_via="mdns",
        last_seen=NOW,
    )
    _roundtrip(Device, device)


def test_device_status_roundtrip() -> None:
    status = DeviceStatus(
        hostname="steamdeck",
        os_name="SteamOS",
        os_version="3.7.13",
        is_deckard=False,
        session_status="gamescope",
        steam_status="OS",
        steam_osclient_branch="steamdeck_stable",
        steam_osclient_version=1760000000,
        cef_debugging_enabled=False,
        renderdoc_layer_enabled=False,
        renderdoc_replay_server_running=False,
        user_password_is_set=True,
        steam_launch_flags={"PROTON_LOG": "1"},
        raw={"extra": "preserved"},
    )
    _roundtrip(DeviceStatus, status)


def test_title_and_detail_roundtrip() -> None:
    title = Title(gameid="my.game", registered_with_steam=True, size_bytes=1024, modified=NOW)
    _roundtrip(Title, title)
    detail = TitleDetail(
        gameid="my.game",
        registered_with_steam=True,
        config=TitleConfig(entrypoint="run.sh", args=["-v"], env={"A": "B"}, runtime="lepton"),
        remote_dir="~/devkit-game/my.game",
    )
    _roundtrip(TitleDetail, detail)


def test_deploy_spec_checksum_and_update_are_exclusive() -> None:
    with pytest.raises(ValidationError, match="mutually exclusive"):
        DeploySpec(device="deck", gameid="g1", source_dir="/tmp", checksum=True, update=True)


def test_deploy_spec_roundtrip_defaults() -> None:
    spec = DeploySpec(device="deck", gameid="g1", source_dir=Path("/tmp/build"))
    assert spec.compress is True and spec.register is True and spec.launch is False
    assert spec.filters.mode == "exclude"
    _roundtrip(DeploySpec, spec)


def test_deploy_preview_roundtrip() -> None:
    preview = DeployPreview(
        files_added=1,
        files_changed=2,
        files_deleted=0,
        bytes_to_transfer=4096,
        sample_changes=[">f+++++++++ bin/game"],
    )
    _roundtrip(DeployPreview, preview)


def test_artifact_id_patterns_enforced() -> None:
    base = {
        "job_id": JOB_ID,
        "device": "deck",
        "kind": "screenshot",
        "path": Path("/tmp/a.png"),
        "bytes": 10,
        "sha256": "a" * 64,
        "remote_source": "/tmp/gamescope.png",
        "created_at": NOW,
    }
    artifact = Artifact(id=ART_ID, **base)  # type: ignore[arg-type]
    _roundtrip(Artifact, artifact)
    with pytest.raises(ValidationError):
        Artifact(id="not-an-artifact-id", **base)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Artifact(id=ART_ID, **{**base, "sha256": "nothex"})  # type: ignore[arg-type]


def test_launch_result_readiness_is_unknown() -> None:
    result = LaunchResult(gameid="g1", launched=True, device_response="ok")
    assert result.readiness == "unknown"
    _roundtrip(LaunchResult, result)


def test_result_envelope() -> None:
    ok = Result.success(message="done")
    assert ok.ok and ok.code is None
    fail = Result.failure(ErrorCode.DEVICE_NOT_FOUND, "nope")
    assert not fail.ok and fail.code == ErrorCode.DEVICE_NOT_FOUND
    _roundtrip(Result, ok)
    _roundtrip(Result, fail)


def test_job_models() -> None:
    assert JobState.QUEUED.is_terminal is False
    assert JobState.RUNNING.is_terminal is False
    for terminal in (
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.INTERRUPTED,
        JobState.TIMEOUT,
    ):
        assert terminal.is_terminal
    info = JobInfo(
        id=JOB_ID,
        kind=JobKind.DEPLOY,
        state=JobState.RUNNING,
        device="deck",
        gameid="g1",
        created_at=NOW,
        progress=JobProgress(phase="transferring", bytes_done=5, bytes_total=10, items_done=1),
    )
    _roundtrip(JobInfo, info)


def test_capabilities_roundtrip() -> None:
    caps = Capabilities(
        host=HostCapabilities(
            platform="darwin",
            arch="arm64",
            context="cli",
            python_version="3.12.12",
            state_dir=Path("/tmp/state"),
            state_dir_writable=True,
            rsync=RsyncInfo(flavor="rsync", version="3.5.1", protocol_version=35, ok=True),
            ssh_path=Path("/usr/bin/ssh"),
            terminal="Terminal.app",
            local_network="unknown",
        ),
        device=DeviceCapabilities(family="deck", has_gpu_trace=True),
        operations={"deploy": OperationCapability(status="supported_untested")},
    )
    _roundtrip(Capabilities, caps)


def test_tool_availability_roundtrip() -> None:
    tools = ToolAvailability(rsync=RsyncInfo(), terminal=None)
    assert tools.rsync.ok is False
    _roundtrip(ToolAvailability, tools)


def test_gameid_annotation_uses_spec_regex() -> None:
    metadata = GameId.__metadata__
    assert any(getattr(m, "pattern", None) == constants.GAMEID_REGEX for m in metadata)
