"""Unit tests for profiling operations (spec section 8; audit section 4).

Covers the exact remote command strings for the single-command toggles
(MangoHud inversion, RenderDoc layer/server, Proton logging) and the CLI
plumbing of the profiling commands. The gpu_trace/rgp job flows live in
tests/integration/test_profiling_pipeline.py.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import paramiko
import pytest
from fake_ssh import make_paramiko_patch
from typer.testing import CliRunner

from steamos_devkit import cli, profiling
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Device, JobInfo, JobKind, JobState, Result

runner = CliRunner()

ENV_DIR = "$XDG_RUNTIME_DIR/steam/env"


@pytest.fixture
def core(state_dir: Path) -> DevkitCore:
    return DevkitCore(state_dir=state_dir)


@pytest.fixture
def paired_key(core: DevkitCore) -> Path:
    core.state.ensure_layout()
    key_path = core.state.key_path
    paramiko.RSAKey.generate(2048).write_private_key_file(str(key_path))
    return key_path


def _registry_device(core: DevkitCore, name: str = "deck") -> Device:
    device = Device(name=name, address="192.0.2.10", login="deck", added_via="explicit")
    core.state.devices.upsert(device)
    return device


def _all_ok(monkeypatch: pytest.MonkeyPatch):
    return make_paramiko_patch(monkeypatch, commands=[(r".", b"", b"", 0)])


# --- mangohudctl ---------------------------------------------------------------


def test_perf_overlay_enable_inverts_no_display(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    fake = _all_ok(monkeypatch)
    result = profiling.set_perf_overlay(core, "deck", True)
    assert result.ok
    assert fake.commands_run == ["mangohudctl set no_display false"]


def test_perf_overlay_disable(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    fake = _all_ok(monkeypatch)
    result = profiling.set_perf_overlay(core, "deck", False)
    assert result.ok
    assert "disabled" in (result.message or "")
    assert fake.commands_run == ["mangohudctl set no_display true"]


def test_frametime_logging_on(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    fake = _all_ok(monkeypatch)
    result = profiling.set_frametime_logging(core, "deck", True)
    assert result.ok
    assert fake.commands_run == ["mangohudctl set log_session true"]


def test_frametime_logging_off_points_at_logs(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    fake = _all_ok(monkeypatch)
    result = profiling.set_frametime_logging(core, "deck", False)
    assert result.ok
    assert "frametimes" in (result.message or "")
    assert fake.commands_run == ["mangohudctl set log_session false"]


def test_mangohudctl_failure_raises(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    make_paramiko_patch(monkeypatch, commands=[(r"mangohudctl", b"", b"boom\n", 1)])
    with pytest.raises(DevkitError) as exc_info:
        profiling.set_perf_overlay(core, "deck", True)
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    assert exc_info.value.remote_exit_code == 1


# --- renderdoc layer -----------------------------------------------------------


def test_renderdoc_layer_enable_expands_xdg_remotely(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    fake = _all_ok(monkeypatch)
    result = profiling.set_renderdoc_layer(core, "deck", True)
    assert result.ok
    # $XDG_RUNTIME_DIR must reach the device unexpanded (single command string)
    assert fake.commands_run == [
        f"mkdir -p {ENV_DIR} && echo 1 > {ENV_DIR}/ENABLE_VULKAN_RENDERDOC_CAPTURE"
    ]


def test_renderdoc_layer_disable(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    fake = _all_ok(monkeypatch)
    result = profiling.set_renderdoc_layer(core, "deck", False)
    assert result.ok
    assert fake.commands_run == [f"rm -f {ENV_DIR}/ENABLE_VULKAN_RENDERDOC_CAPTURE"]


# --- renderdoc server ----------------------------------------------------------

_START_CMD = (
    "systemd-run --user --unit=devkit-renderdoc-replay --collect "
    "env RENDERDOC_TEMP=/home/deck renderdoccmd remoteserver"
)
_STOP_CMD = "systemctl --user stop devkit-renderdoc-replay 2>/dev/null; killall -9 renderdoccmd"


def test_renderdoc_server_start_verifies_pgrep(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    fake = make_paramiko_patch(
        monkeypatch,
        commands=[
            (r"systemd-run", b"", b"", 0),
            (r"pgrep -x renderdoccmd", b"4321\n", b"", 0),
        ],
    )
    result = profiling.renderdoc_server(core, "deck", "start")
    assert result.ok
    assert fake.commands_run == [_START_CMD, "sleep 2; pgrep -x renderdoccmd"]


def test_renderdoc_server_start_not_staying_up_fails(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    make_paramiko_patch(
        monkeypatch,
        commands=[
            (r"systemd-run", b"", b"", 0),
            (r"pgrep -x renderdoccmd", b"", b"", 1),
        ],
    )
    with pytest.raises(DevkitError) as exc_info:
        profiling.renderdoc_server(core, "deck", "start")
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED


def test_renderdoc_server_stop_tolerates_nonzero(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    fake = make_paramiko_patch(monkeypatch, commands=[(r"systemctl", b"", b"", 1)])
    result = profiling.renderdoc_server(core, "deck", "stop")
    assert result.ok
    assert fake.commands_run == [_STOP_CMD]


def test_renderdoc_server_status_running(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    make_paramiko_patch(monkeypatch, commands=[(r"pgrep", b"4321\n", b"", 0)])
    result = profiling.renderdoc_server(core, "deck", "status")
    assert result.ok
    assert "is running" in (result.message or "")


def test_renderdoc_server_status_not_running(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    make_paramiko_patch(monkeypatch, commands=[(r"pgrep", b"", b"", 1)])
    result = profiling.renderdoc_server(core, "deck", "status")
    assert result.ok
    assert "is not running" in (result.message or "")


def test_renderdoc_server_rejects_unknown_action(core: DevkitCore) -> None:
    _registry_device(core)
    with pytest.raises(DevkitError) as exc_info:
        profiling.renderdoc_server(core, "deck", "restart")  # type: ignore[arg-type]
    assert exc_info.value.code == ErrorCode.VALIDATION


# --- proton logging ------------------------------------------------------------


def test_proton_log_on_removes_legacy_winedebug_first(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    fake = _all_ok(monkeypatch)
    result = profiling.set_proton_logging(core, "deck", "on")
    assert result.ok
    assert fake.commands_run == [
        f"mkdir -p {ENV_DIR}",
        f"rm -f {ENV_DIR}/WINEDEBUG",
        f"echo 1 > {ENV_DIR}/PROTON_LOG",
    ]


def test_proton_log_on_with_winedebug(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    fake = _all_ok(monkeypatch)
    result = profiling.set_proton_logging(core, "deck", "on", winedebug="+timestamp")
    assert result.ok
    # no legacy WINEDEBUG removal when a value is given
    assert fake.commands_run == [
        f"mkdir -p {ENV_DIR}",
        f"echo 1 > {ENV_DIR}/PROTON_LOG",
        f"echo +timestamp > {ENV_DIR}/WINEDEBUG",
    ]


def test_proton_log_winedebug_value_is_quoted(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    fake = _all_ok(monkeypatch)
    profiling.set_proton_logging(core, "deck", "on", winedebug="foo bar")
    assert fake.commands_run[-1] == f"echo 'foo bar' > {ENV_DIR}/WINEDEBUG"


def test_proton_log_off(
    core: DevkitCore, paired_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_device(core)
    fake = _all_ok(monkeypatch)
    result = profiling.set_proton_logging(core, "deck", "off")
    assert result.ok
    assert fake.commands_run == [
        f"mkdir -p {ENV_DIR}",
        f"rm -f {ENV_DIR}/PROTON_LOG",
        f"rm -f {ENV_DIR}/WINEDEBUG",
    ]


def test_proton_log_rejects_unknown_mode(core: DevkitCore) -> None:
    _registry_device(core)
    with pytest.raises(DevkitError) as exc_info:
        profiling.set_proton_logging(core, "deck", "verbose")  # type: ignore[arg-type]
    assert exc_info.value.code == ErrorCode.VALIDATION


# --- CLI plumbing --------------------------------------------------------------


def _job_info(kind: JobKind = JobKind.GPU_TRACE) -> JobInfo:
    return JobInfo(
        id="job_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        kind=kind,
        state=JobState.SUCCEEDED,
        device="deck",
        created_at=datetime.now(UTC),
        message="done",
        artifacts=["art_01ARZ3NDEKTSV4RRFFQ69G5FAV"],
    )


class _StubJob:
    def __init__(self, info: JobInfo | None = None, error: DevkitError | None = None) -> None:
        self.job_id = "job_01ARZ3NDEKTSV4RRFFQ69G5FAV"
        self._info = info
        self._error = error

    def wait(self) -> JobInfo:
        if self._error is not None:
            raise self._error
        assert self._info is not None
        return self._info


def test_cli_gpu_trace_waits_and_reports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(
        profiling,
        "gpu_trace",
        lambda core, sel, timeout_s=300: calls.append((sel, timeout_s)) or _StubJob(_job_info()),
    )
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path), "gpu-trace", "deck"])
    assert result.exit_code == 0
    assert calls == [("deck", 300)]
    payload = json.loads(result.stdout)
    assert payload["state"] == "succeeded"
    assert payload["artifacts"] == ["art_01ARZ3NDEKTSV4RRFFQ69G5FAV"]


def test_cli_gpu_trace_no_wait_prints_job_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(profiling, "gpu_trace", lambda core, sel, timeout_s=300: _StubJob())
    result = runner.invoke(
        cli.app, ["--state-dir", str(tmp_path), "gpu-trace", "deck", "--no-wait"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {"job_id": "job_01ARZ3NDEKTSV4RRFFQ69G5FAV", "status": "queued"}


def test_cli_rgp_failed_job_maps_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    error = DevkitError(ErrorCode.REMOTE_COMMAND_FAILED, "capture failed")
    monkeypatch.setattr(
        profiling, "rgp_capture", lambda core, sel, timeout_s=180: _StubJob(error=error)
    )
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path), "rgp", "deck"])
    assert result.exit_code == 7
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "REMOTE_COMMAND_FAILED"


def test_cli_perf_overlay_maps_on_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(
        profiling,
        "set_perf_overlay",
        lambda core, sel, enabled: calls.append(enabled) or Result.success(message="ok"),
    )
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path), "perf-overlay", "deck", "on"])
    assert result.exit_code == 0
    assert calls == [True]
    assert json.loads(result.stdout)["ok"] is True

    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path), "perf-overlay", "deck", "off"])
    assert result.exit_code == 0
    assert calls == [True, False]


def test_cli_perf_overlay_rejects_bad_state(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path), "perf-overlay", "deck", "maybe"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "VALIDATION"


def test_cli_renderdoc_layer_and_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    layer_calls = []
    server_calls = []
    monkeypatch.setattr(
        profiling,
        "set_renderdoc_layer",
        lambda core, sel, enabled: layer_calls.append(enabled) or Result.success(message="ok"),
    )
    monkeypatch.setattr(
        profiling,
        "renderdoc_server",
        lambda core, sel, action: server_calls.append(action) or Result.success(message="ok"),
    )
    result = runner.invoke(
        cli.app, ["--state-dir", str(tmp_path), "renderdoc", "layer", "deck", "on"]
    )
    assert result.exit_code == 0
    assert layer_calls == [True]
    result = runner.invoke(
        cli.app, ["--state-dir", str(tmp_path), "renderdoc", "server", "deck", "start"]
    )
    assert result.exit_code == 0
    assert server_calls == ["start"]


def test_cli_renderdoc_server_rejects_bad_action(tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app, ["--state-dir", str(tmp_path), "renderdoc", "server", "deck", "restart"]
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "VALIDATION"


def test_cli_proton_log_passes_winedebug(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(
        profiling,
        "set_proton_logging",
        lambda core, sel, mode, winedebug=None: (
            calls.append((mode, winedebug)) or Result.success(message="ok")
        ),
    )
    result = runner.invoke(
        cli.app,
        ["--state-dir", str(tmp_path), "proton-log", "deck", "on", "--winedebug", "+timestamp"],
    )
    assert result.exit_code == 0
    assert calls == [("on", "+timestamp")]


def test_cli_frametimes_maps_on_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(
        profiling,
        "set_frametime_logging",
        lambda core, sel, enabled: calls.append(enabled) or Result.success(message="ok"),
    )
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path), "frametimes", "deck", "on"])
    assert result.exit_code == 0
    assert calls == [True]


def test_cli_job_submit_error_maps_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unknown_device(core, sel, timeout_s=300):
        raise DevkitError(ErrorCode.DEVICE_NOT_FOUND, "unknown device")

    monkeypatch.setattr(profiling, "gpu_trace", unknown_device)
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path), "gpu-trace", "ghost"])
    assert result.exit_code == 3
    assert json.loads(result.stdout)["code"] == "DEVICE_NOT_FOUND"


def test_cli_rgp_submit_error_maps_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unknown_device(core, sel, timeout_s=180):
        raise DevkitError(ErrorCode.DEVICE_NOT_FOUND, "unknown device")

    monkeypatch.setattr(profiling, "rgp_capture", unknown_device)
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path), "rgp", "ghost", "--no-wait"])
    assert result.exit_code == 3


def test_cli_toggle_error_maps_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def unreachable(core, sel, enabled):
        raise DevkitError(ErrorCode.DEVICE_UNREACHABLE, "cannot reach")

    monkeypatch.setattr(profiling, "set_perf_overlay", unreachable)
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path), "perf-overlay", "deck", "on"])
    assert result.exit_code == 3
    assert json.loads(result.stdout)["code"] == "DEVICE_UNREACHABLE"
