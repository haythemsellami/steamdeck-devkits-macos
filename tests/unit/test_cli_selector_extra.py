"""Selector standardization tests (wave-2 spec delta).

Every device-taking CLI command takes an OPTIONAL device positional (or
``--device/-d`` for deploy, where a leading optional positional would be
ambiguous) resolved through ``cli.common.resolve_selector``: explicit value
wins, otherwise the last-device marker, otherwise VALIDATION exit 2.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import paramiko
import pytest
import typer
from fake_ssh import make_paramiko_patch
from typer.testing import CliRunner

from steamos_devkit import cli, profiling, session
from steamos_devkit.cli import common
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Device, Result

runner = CliRunner()

GAMEID = "hello_triangle"
NO_DEVICE_MSG = "no device given and no last device; pass a device or run `devkit pair`"


def _ctx(state_dir: Path) -> typer.Context:
    ctx = typer.Context(click.Command("devkit-test"))
    ctx.obj = common.Options(json=True, state_dir=state_dir, verbose=False)
    return ctx


def _fake_rsync(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "rsync"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf 'rsync  version 3.5.1  protocol version 33\\n'\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@pytest.fixture
def cli_state(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A CLI state dir with a key and a registered device named ``deck``."""
    monkeypatch.setenv("DEVKIT_RSYNC", str(_fake_rsync(state_dir / "bin")))
    core = DevkitCore(state_dir=state_dir)
    core.state.ensure_layout()
    paramiko.RSAKey.generate(2048).write_private_key_file(str(core.state.key_path))
    core.state.devices.upsert(
        Device(name="deck", address="192.0.2.10", login="deck", added_via="explicit")
    )
    return state_dir


def _with_last_device(state_dir: Path) -> None:
    DevkitCore(state_dir=state_dir).state.set_last_device("deck")


class TestResolveSelector:
    def test_explicit_selector_wins(self, cli_state: Path) -> None:
        # last_device exists but the explicit value must win
        _with_last_device(cli_state)
        assert common.resolve_selector(_ctx(cli_state), "other") == "other"

    def test_falls_back_to_last_device(self, cli_state: Path) -> None:
        _with_last_device(cli_state)
        assert common.resolve_selector(_ctx(cli_state), None) == "deck"

    def test_no_selector_no_marker_is_validation(self, cli_state: Path) -> None:
        with pytest.raises(DevkitError) as exc_info:
            common.resolve_selector(_ctx(cli_state), None)
        assert exc_info.value.code == ErrorCode.VALIDATION
        assert exc_info.value.message == NO_DEVICE_MSG


class TestTitlesSelectorFallback:
    def test_titles_list_uses_last_device(
        self, cli_state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_last_device(cli_state)
        make_paramiko_patch(
            monkeypatch,
            commands=[(r"steamos-list-games", json.dumps([{"gameid": GAMEID}]), b"", 0)],
        )
        result = runner.invoke(cli.app, ["--state-dir", str(cli_state), "titles", "list"])
        assert result.exit_code == 0, result.output
        assert [t["gameid"] for t in json.loads(result.stdout)] == [GAMEID]

    def test_titles_list_without_selector_or_marker_exits_2(self, cli_state: Path) -> None:
        result = runner.invoke(cli.app, ["--state-dir", str(cli_state), "titles", "list"])
        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        assert payload["code"] == "VALIDATION"
        assert payload["message"] == NO_DEVICE_MSG

    def test_launch_gameid_only_uses_last_device(
        self, cli_state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_last_device(cli_state)
        ssh = make_paramiko_patch(monkeypatch, commands=[(r"run-game", b"success\n", b"", 0)])
        result = runner.invoke(cli.app, ["--state-dir", str(cli_state), "launch", GAMEID])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["launched"] is True
        assert any("run-game" in c for c in ssh.commands_run)

    def test_launch_without_gameid_exits_2(self, cli_state: Path) -> None:
        _with_last_device(cli_state)
        result = runner.invoke(cli.app, ["--state-dir", str(cli_state), "launch"])
        assert result.exit_code == 2
        assert json.loads(result.stdout)["message"] == "missing gameid"

    def test_titles_inspect_gameid_only_uses_last_device(
        self, cli_state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_last_device(cli_state)
        make_paramiko_patch(monkeypatch, commands=[(r"cat .*-argv\.json", b'["run.sh"]', b"", 0)])
        result = runner.invoke(
            cli.app, ["--state-dir", str(cli_state), "titles", "inspect", GAMEID]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["config"]["entrypoint"] == "run.sh"


class TestDeploySelectorOption:
    def test_deploy_without_device_or_marker_exits_2(self, cli_state: Path) -> None:
        result = runner.invoke(cli.app, ["--state-dir", str(cli_state), "deploy", GAMEID, "/tmp"])
        assert result.exit_code == 2
        assert json.loads(result.stdout)["message"] == NO_DEVICE_MSG

    def test_deploy_uses_last_device_when_option_omitted(self, cli_state: Path) -> None:
        _with_last_device(cli_state)
        # the marker resolves to the registry device; a missing source dir then
        # fails validation — proving the selector fell back before spec checks
        result = runner.invoke(
            cli.app, ["--state-dir", str(cli_state), "deploy", GAMEID, "/nonexistent-src"]
        )
        assert result.exit_code == 2  # VALIDATION (source dir), not "no device given"
        assert "no device given" not in json.loads(result.stdout)["message"]

    def test_deploy_device_option_overrides_last_device(self, cli_state: Path) -> None:
        _with_last_device(cli_state)
        # --device 192.0.2.99 is a transient selector: resolution succeeds, then
        # the source-dir validation fails — the marker (deck) was NOT used
        result = runner.invoke(
            cli.app,
            [
                "--state-dir",
                str(cli_state),
                "deploy",
                GAMEID,
                "/nonexistent-src",
                "-d",
                "192.0.2.99",
            ],
        )
        assert result.exit_code == 2
        assert "no device given" not in json.loads(result.stdout)["message"]


class TestProfilingSelectorFallback:
    def test_gpu_trace_uses_last_device(
        self, cli_state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_last_device(cli_state)
        calls = []

        class _Job:
            job_id = "job_01ARZ3NDEKTSV4RRFFQ69G5FAV"

            def wait(self):  # pragma: no cover - trivial stub
                raise DevkitError(ErrorCode.CANCELLED, "stop after submit")

        monkeypatch.setattr(
            profiling, "gpu_trace", lambda core, sel, timeout_s=300: calls.append(sel) or _Job()
        )
        result = runner.invoke(cli.app, ["--state-dir", str(cli_state), "gpu-trace", "--no-wait"])
        assert result.exit_code == 0, result.output
        assert calls == ["deck"]

    def test_perf_overlay_state_only_uses_last_device(
        self, cli_state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_last_device(cli_state)
        calls = []
        monkeypatch.setattr(
            profiling,
            "set_perf_overlay",
            lambda core, sel, enabled: calls.append((sel, enabled)) or Result.success(message="ok"),
        )
        result = runner.invoke(cli.app, ["--state-dir", str(cli_state), "perf-overlay", "on"])
        assert result.exit_code == 0, result.output
        assert calls == [("deck", True)]

    def test_perf_overlay_without_state_exits_2(self, cli_state: Path) -> None:
        _with_last_device(cli_state)
        result = runner.invoke(cli.app, ["--state-dir", str(cli_state), "perf-overlay"])
        assert result.exit_code == 2
        assert json.loads(result.stdout)["message"] == "missing on|off argument"

    def test_renderdoc_server_action_only_uses_last_device(
        self, cli_state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_last_device(cli_state)
        calls = []
        monkeypatch.setattr(
            profiling,
            "renderdoc_server",
            lambda core, sel, action: calls.append((sel, action)) or Result.success(message="ok"),
        )
        result = runner.invoke(
            cli.app, ["--state-dir", str(cli_state), "renderdoc", "server", "status"]
        )
        assert result.exit_code == 0, result.output
        assert calls == [("deck", "status")]


class TestSessionSelectorFallback:
    def test_session_only_uses_last_device(
        self, cli_state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_last_device(cli_state)
        calls = []
        monkeypatch.setattr(
            session,
            "set_session",
            lambda core, sel, which: calls.append((sel, which)) or Result.success(message="ok"),
        )
        result = runner.invoke(cli.app, ["--state-dir", str(cli_state), "session", "gamescope"])
        assert result.exit_code == 0, result.output
        assert calls == [("deck", "gamescope")]

    def test_steam_mode_only_uses_last_device(
        self, cli_state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_last_device(cli_state)
        calls = []
        monkeypatch.setattr(
            session,
            "set_steam_mode",
            lambda core, sel, mode, gameid=None, args=None, gdbserver=False: (
                calls.append((sel, mode)) or Result.success(message="ok")
            ),
        )
        result = runner.invoke(cli.app, ["--state-dir", str(cli_state), "steam-mode", "os"])
        assert result.exit_code == 0, result.output
        assert calls == [("deck", "OS")]

    def test_reload_steam_uses_last_device(
        self, cli_state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_last_device(cli_state)
        calls = []
        monkeypatch.setattr(
            session,
            "reload_steam",
            lambda core, sel: calls.append(sel) or Result.success(message="ok"),
        )
        result = runner.invoke(cli.app, ["--state-dir", str(cli_state), "reload-steam"])
        assert result.exit_code == 0, result.output
        assert calls == ["deck"]

    def test_session_without_value_exits_2(self, cli_state: Path) -> None:
        _with_last_device(cli_state)
        result = runner.invoke(cli.app, ["--state-dir", str(cli_state), "session"])
        assert result.exit_code == 2
        assert (
            "missing gamescope|plasma-x11|plasma-wayland argument"
            in json.loads(result.stdout)["message"]
        )
