"""Unit tests for titles.py — remote command construction, sidecars, launch parsing.

All device contact goes through the fake-SSH harness; ``ensure_utils`` rsync
runs against a fake rsync binary (version probe + exit 0) via DEVKIT_RSYNC.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import paramiko
import pytest
from fake_ssh import FakeSSHClient, make_paramiko_patch
from typer.testing import CliRunner

from steamos_devkit import cli
from steamos_devkit.config import TitleState
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Device, TitleConfig
from steamos_devkit.titles import (
    configure_title,
    delete_title,
    deploy,
    inspect_title,
    launch_title,
    list_titles,
    register_title,
)

runner = CliRunner()

GAMEID = "hello_triangle"
LIST_GAMES_JSON = json.dumps([{"gameid": GAMEID}, {"gameid": "spacegame_dev"}])


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
def core(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> DevkitCore:
    monkeypatch.setenv("DEVKIT_RSYNC", str(_fake_rsync(state_dir / "bin")))
    core = DevkitCore(state_dir=state_dir)
    core.state.ensure_layout()
    paramiko.RSAKey.generate(2048).write_private_key_file(str(core.state.key_path))
    core.state.devices.upsert(
        Device(name="deck", address="192.0.2.10", login="deck", added_via="explicit")
    )
    return core


@pytest.fixture
def ssh(core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> FakeSSHClient:
    return make_paramiko_patch(
        monkeypatch,
        commands=[
            (r"steamos-list-games", LIST_GAMES_JSON, b"", 0),
            # absent sidecars, answered the way GNU cat answers (L3)
            (r"^cat ", b"", b"cat: /home/deck/x: No such file or directory", 1),
        ],
    )


class TestListTitles:
    def test_lists_titles_with_exact_command(self, core: DevkitCore, ssh: FakeSSHClient) -> None:
        titles = list_titles(core, "deck")
        assert [t.gameid for t in titles] == [GAMEID, "spacegame_dev"]
        assert all(t.registered_with_steam for t in titles)
        assert "python3 ~/devkit-utils/steamos-list-games" in ssh.commands_run

    def test_malformed_payload_raises(
        self, core: DevkitCore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        make_paramiko_patch(
            monkeypatch, commands=[(r"steamos-list-games", b'{"not": "a list"}', b"", 0)]
        )
        with pytest.raises(DevkitError) as exc_info:
            list_titles(core, "deck")
        assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED

    def test_malformed_entry_raises(
        self, core: DevkitCore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        make_paramiko_patch(
            monkeypatch, commands=[(r"steamos-list-games", b'[{"nope": 1}]', b"", 0)]
        )
        with pytest.raises(DevkitError) as exc_info:
            list_titles(core, "deck")
        assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED


class TestInspectTitle:
    def test_reads_all_sidecars(self, core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
        ssh = make_paramiko_patch(
            monkeypatch,
            commands=[
                (r"cat .*-argv\.json", b'["run.sh", "-v", "--fullscreen"]', b"", 0),
                (r"cat .*-env\.json", b'{"STEAM_COMPAT": "1"}', b"", 0),
                (
                    r"cat .*-settings\.json",
                    b'{"steam_play": "1", "compat_tool": "proton-stable"}',
                    b"",
                    0,
                ),
            ],
        )
        detail = inspect_title(core, "deck", GAMEID)
        assert detail.config.entrypoint == "run.sh"
        assert detail.config.args == ["-v", "--fullscreen"]
        assert detail.config.env == {"STEAM_COMPAT": "1"}
        assert detail.config.runtime == "proton-stable"
        # the raw compat_tool key is preserved in settings for lossless round-trips
        assert detail.config.settings["compat_tool"] == "proton-stable"
        assert detail.remote_dir == "~/devkit-game/hello_triangle"
        assert f"cat ~/devkit-game/{GAMEID}-argv.json" in ssh.commands_run
        assert f"cat ~/devkit-game/{GAMEID}-env.json" in ssh.commands_run
        assert f"cat ~/devkit-game/{GAMEID}-settings.json" in ssh.commands_run

    def test_missing_sidecars_give_defaults(self, core: DevkitCore, ssh: FakeSSHClient) -> None:
        detail = inspect_title(core, "deck", GAMEID)  # every cat hits the 127 default
        assert detail.config == TitleConfig()

    def test_malformed_sidecar_raises(
        self, core: DevkitCore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        make_paramiko_patch(monkeypatch, commands=[(r"cat .*-argv\.json", b"[not json", b"", 0)])
        with pytest.raises(DevkitError) as exc_info:
            inspect_title(core, "deck", GAMEID)
        assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED

    def test_invalid_gameid_rejected_before_any_command(
        self, core: DevkitCore, ssh: FakeSSHClient
    ) -> None:
        with pytest.raises(DevkitError) as exc_info:
            inspect_title(core, "deck", "bad; rm -rf /")
        assert exc_info.value.code == ErrorCode.VALIDATION
        assert not any("cat" in c for c in ssh.commands_run)


class TestConfigureTitle:
    def test_writes_sidecars_and_persists_state(self, core: DevkitCore, ssh: FakeSSHClient) -> None:
        config = TitleConfig(
            entrypoint="bin/run.sh",
            args=["-v"],
            env={"A": "B"},
            runtime="proton-experimental",
            settings={"steam_play": "1"},
        )
        result = configure_title(core, "deck", GAMEID, config)
        assert result.ok
        fs = ssh.filesystem
        assert json.loads(fs[f"/home/deck/devkit-game/{GAMEID}-argv.json"]) == ["bin/run.sh", "-v"]
        assert json.loads(fs[f"/home/deck/devkit-game/{GAMEID}-env.json"]) == {"A": "B"}
        settings = json.loads(fs[f"/home/deck/devkit-game/{GAMEID}-settings.json"])
        assert settings == {"steam_play": "1", "compat_tool": "proton-experimental"}
        stored = core.state.load_title(GAMEID)
        assert stored is not None and stored.config.entrypoint == "bin/run.sh"

    @pytest.mark.parametrize("bad", ["/abs/path", "../escape", "a/../../b", "  "])
    def test_entrypoint_validation(self, core: DevkitCore, ssh: FakeSSHClient, bad: str) -> None:
        with pytest.raises(DevkitError) as exc_info:
            configure_title(core, "deck", GAMEID, TitleConfig(entrypoint=bad))
        assert exc_info.value.code == ErrorCode.VALIDATION

    def test_relative_entrypoint_ok(self, core: DevkitCore, ssh: FakeSSHClient) -> None:
        result = configure_title(core, "deck", GAMEID, TitleConfig(entrypoint="a/b/c.sh"))
        assert result.ok

    def test_env_only_configure_preserves_device_config(self, core: DevkitCore, ssh) -> None:
        # The device already has argv/settings sidecars; setting only env must
        # not touch them (device-helper semantics: save_argv(None)/empty env skip).
        result = configure_title(core, "deck", GAMEID, TitleConfig(env={"NEW": "1"}))
        assert result.ok
        fs = ssh.filesystem
        assert json.loads(fs[f"/home/deck/devkit-game/{GAMEID}-env.json"]) == {"NEW": "1"}
        assert f"/home/deck/devkit-game/{GAMEID}-argv.json" not in fs
        assert f"/home/deck/devkit-game/{GAMEID}-settings.json" not in fs

    def test_empty_configure_writes_nothing(self, core: DevkitCore, ssh: FakeSSHClient) -> None:
        result = configure_title(core, "deck", GAMEID, TitleConfig())
        assert result.ok
        assert ssh.filesystem == {}

    def test_settings_merge_over_device(
        self, core: DevkitCore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ssh = make_paramiko_patch(
            monkeypatch,
            commands=[
                (
                    rf"cat ~/devkit-game/{GAMEID}-settings\.json",
                    b'{"steam_play": "1", "keep": "x"}\n',
                    b"",
                    0,
                )
            ],
        )
        configure_title(
            core,
            "deck",
            GAMEID,
            TitleConfig(settings={"steam_play": "0"}, runtime="proton-stable"),
        )
        merged = json.loads(ssh.filesystem[f"/home/deck/devkit-game/{GAMEID}-settings.json"])
        assert merged == {"steam_play": "0", "keep": "x", "compat_tool": "proton-stable"}

    def test_args_only_preserves_device_entrypoint(
        self, core: DevkitCore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ssh = make_paramiko_patch(
            monkeypatch,
            commands=[
                (
                    rf"cat ~/devkit-game/{GAMEID}-argv\.json",
                    b'["bin/keep.sh", "--old"]\n',
                    b"",
                    0,
                )
            ],
        )
        configure_title(core, "deck", GAMEID, TitleConfig(args=["--new"]))
        argv = json.loads(ssh.filesystem[f"/home/deck/devkit-game/{GAMEID}-argv.json"])
        assert argv == ["bin/keep.sh", "--new"]

    def test_entrypoint_replaces_whole_argv(
        self, core: DevkitCore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ssh = make_paramiko_patch(
            monkeypatch,
            commands=[
                (rf"cat ~/devkit-game/{GAMEID}-argv\.json", b'["bin/old.sh", "--old"]\n', b"", 0)
            ],
        )
        configure_title(core, "deck", GAMEID, TitleConfig(entrypoint="bin/new.sh"))
        argv = json.loads(ssh.filesystem[f"/home/deck/devkit-game/{GAMEID}-argv.json"])
        assert argv == ["bin/new.sh"]


class TestRegisterTitle:
    def _register_command(self, ssh: FakeSSHClient) -> str:
        command = next(c for c in ssh.commands_run if "create-shortcut" in c)
        return command

    def test_parms_json_shape(self, core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
        ssh = make_paramiko_patch(
            monkeypatch, commands=[(r"create-shortcut", b'{"success": "registered"}', b"", 0)]
        )
        core.state.save_title(
            TitleState(gameid=GAMEID, config=TitleConfig(entrypoint="run.sh", args=["-v"]))
        )
        result = register_title(core, "deck", GAMEID)
        assert result.ok and result.registration_complete is True
        command = self._register_command(ssh)
        assert command.startswith("python3 ~/devkit-utils/steam-client-create-shortcut --parms ")
        parms = json.loads(shlex.split(command)[3])
        assert parms["gameid"] == GAMEID
        assert parms["directory"] == f"/home/deck/devkit-game/{GAMEID}"
        assert parms["argv"] == ["run.sh", "-v"]
        # required by the device-side script even when unset (upstream :1331)
        assert parms["force_appid"] == ""
        assert parms["lepton_args"] == ""
        assert parms["settings"]["compat_tool"] == ""

    def test_no_stored_config_sends_null_argv(
        self, core: DevkitCore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ssh = make_paramiko_patch(
            monkeypatch, commands=[(r"create-shortcut", b'{"success": "ok"}', b"", 0)]
        )
        register_title(core, "deck", GAMEID)
        parms = json.loads(shlex.split(self._register_command(ssh))[3])
        assert parms["argv"] is None  # device keeps its existing argv sidecar

    def test_device_error_raises(self, core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
        make_paramiko_patch(
            monkeypatch,
            commands=[
                (r"create-shortcut", b'{"error": "timeout - Steam client did not respond"}', b"", 0)
            ],
        )
        with pytest.raises(DevkitError) as exc_info:
            register_title(core, "deck", GAMEID)
        assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
        assert "timeout" in exc_info.value.message

    def test_nonzero_exit_raises(self, core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
        make_paramiko_patch(monkeypatch, commands=[(r"create-shortcut", b"", b"boom", 1)])
        with pytest.raises(DevkitError) as exc_info:
            register_title(core, "deck", GAMEID)
        assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
        assert exc_info.value.remote_exit_code == 1


class TestLaunchTitle:
    def test_success_on_stdout(self, core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
        ssh = make_paramiko_patch(monkeypatch, commands=[(r"run-game", b"success\n", b"", 0)])
        result = launch_title(core, "deck", GAMEID)
        assert result.launched is True
        assert result.readiness == "unknown"
        assert f"run-game gameid={GAMEID}" in next(c for c in ssh.commands_run if "run-game" in c)

    def test_success_logged_on_stderr(
        self, core: DevkitCore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        make_paramiko_patch(monkeypatch, commands=[(r"run-game", b"response-body", b"success", 0)])
        assert launch_title(core, "deck", GAMEID).launched is True

    def test_timeout_exit_zero_is_not_launched(
        self, core: DevkitCore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the device helper's timeout path exits 0 without a success marker
        make_paramiko_patch(monkeypatch, commands=[(r"run-game", b"", b"timeout", 0)])
        assert launch_title(core, "deck", GAMEID).launched is False

    def test_error_exit_is_not_launched(
        self, core: DevkitCore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        make_paramiko_patch(monkeypatch, commands=[(r"run-game", b"failed", b"failed", 255)])
        result = launch_title(core, "deck", GAMEID)
        assert result.launched is False
        assert "failed" in result.device_response


class TestDeleteTitle:
    def test_delete_command(self, core: DevkitCore, monkeypatch: pytest.MonkeyPatch) -> None:
        ssh = make_paramiko_patch(monkeypatch, commands=[(r"steamos-delete", b"deleted\n", b"", 0)])
        result = delete_title(core, "deck", GAMEID, confirm=True)
        assert result.ok
        assert f"python3 ~/devkit-utils/steamos-delete --delete-title {GAMEID}" in ssh.commands_run
        # local title config is device-agnostic state and survives remote deletion
        assert result.message == "deleted"


class TestDeployValidation:
    def test_reserved_gameid_rejected(self, core: DevkitCore, tmp_path: Path) -> None:
        from steamos_devkit.models import DeploySpec

        spec = DeploySpec(device="deck", gameid="steam", source_dir=tmp_path)
        with pytest.raises(DevkitError) as exc_info:
            deploy(core, "deck", spec)
        assert exc_info.value.code == ErrorCode.UNSUPPORTED

    def test_missing_source_dir_rejected(self, core: DevkitCore, tmp_path: Path) -> None:
        from steamos_devkit.models import DeploySpec

        spec = DeploySpec(device="deck", gameid="mygame", source_dir=tmp_path / "nope")
        with pytest.raises(DevkitError) as exc_info:
            deploy(core, "deck", spec)
        assert exc_info.value.code == ErrorCode.VALIDATION

    def test_unknown_device_rejected(self, core: DevkitCore, tmp_path: Path) -> None:
        from steamos_devkit.models import DeploySpec

        spec = DeploySpec(device="nowhere", gameid="mygame", source_dir=tmp_path)
        with pytest.raises(DevkitError) as exc_info:
            deploy(core, "nowhere", spec)
        assert exc_info.value.code == ErrorCode.DEVICE_NOT_FOUND


def _prepare_cli_state(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Key + registered device in the CLI's state dir (same shape as `core`)."""
    monkeypatch.setenv("DEVKIT_RSYNC", str(_fake_rsync(state_dir / "bin")))
    core = DevkitCore(state_dir=state_dir)
    core.state.ensure_layout()
    paramiko.RSAKey.generate(2048).write_private_key_file(str(core.state.key_path))
    core.state.devices.upsert(
        Device(name="deck", address="192.0.2.10", login="deck", added_via="explicit")
    )


class TestTitlesCli:
    def test_titles_list_json(self, state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _prepare_cli_state(state_dir, monkeypatch)
        make_paramiko_patch(
            monkeypatch, commands=[(r"steamos-list-games", LIST_GAMES_JSON, b"", 0)]
        )
        result = runner.invoke(cli.app, ["--state-dir", str(state_dir), "titles", "list", "deck"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert [t["gameid"] for t in payload] == [GAMEID, "spacegame_dev"]

    def test_titles_list_human_on_tty(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from steamos_devkit.cli import common

        monkeypatch.setattr(common, "stdout_is_tty", lambda: True)
        _prepare_cli_state(state_dir, monkeypatch)
        make_paramiko_patch(
            monkeypatch, commands=[(r"steamos-list-games", LIST_GAMES_JSON, b"", 0)]
        )
        result = runner.invoke(cli.app, ["--state-dir", str(state_dir), "titles", "list", "deck"])
        assert result.exit_code == 0
        assert GAMEID in result.stdout
        assert not result.stdout.startswith("[")

    def test_titles_inspect_json(self, state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _prepare_cli_state(state_dir, monkeypatch)
        make_paramiko_patch(
            monkeypatch,
            commands=[
                (r"cat .*-argv\.json", b'["run.sh", "-v"]', b"", 0),
                (r"cat .*-settings\.json", b'{"compat_tool": "lepton"}', b"", 0),
                # the env sidecar is absent; GNU cat's real answer (L3)
                (r"^cat ", b"", b"cat: /home/deck/x: No such file or directory", 1),
            ],
        )
        result = runner.invoke(
            cli.app, ["--state-dir", str(state_dir), "titles", "inspect", "deck", GAMEID]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["config"]["entrypoint"] == "run.sh"
        assert payload["config"]["runtime"] == "lepton"

    def test_titles_configure_full(self, state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _prepare_cli_state(state_dir, monkeypatch)
        ssh = make_paramiko_patch(
            monkeypatch,
            commands=[
                # absent sidecars, answered the way GNU cat answers (L3)
                (r"^cat ", b"", b"cat: /home/deck/x: No such file or directory", 1)
            ],
        )
        result = runner.invoke(
            cli.app,
            [
                "--state-dir",
                str(state_dir),
                "titles",
                "configure",
                "deck",
                GAMEID,
                "--entrypoint",
                "run.sh",
                "--args",
                "-v --fullscreen",
                "--env",
                "A=B",
                "--runtime",
                "proton-stable",
                "--set",
                "steam_play=1",
            ],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["ok"] is True
        fs = ssh.filesystem
        assert json.loads(fs[f"/home/deck/devkit-game/{GAMEID}-argv.json"]) == [
            "run.sh",
            "-v",
            "--fullscreen",
        ]
        assert json.loads(fs[f"/home/deck/devkit-game/{GAMEID}-env.json"]) == {"A": "B"}
        stored = DevkitCore(state_dir=state_dir).state.load_title(GAMEID)
        assert stored is not None and stored.config.runtime == "proton-stable"

    def test_configure_bad_args_quoting(self, state_dir: Path) -> None:
        result = runner.invoke(
            cli.app,
            [
                "--state-dir",
                str(state_dir),
                "titles",
                "configure",
                "deck",
                GAMEID,
                "--args",
                '"unterminated',
            ],
        )
        assert result.exit_code == 2

    def test_titles_register_json(self, state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _prepare_cli_state(state_dir, monkeypatch)
        make_paramiko_patch(
            monkeypatch, commands=[(r"create-shortcut", b'{"success": "ok"}', b"", 0)]
        )
        result = runner.invoke(
            cli.app, ["--state-dir", str(state_dir), "titles", "register", "deck", GAMEID]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["registration_complete"] is True

    def test_titles_delete_with_confirm(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _prepare_cli_state(state_dir, monkeypatch)
        make_paramiko_patch(monkeypatch, commands=[(r"steamos-delete", b"deleted\n", b"", 0)])
        result = runner.invoke(
            cli.app,
            ["--state-dir", str(state_dir), "titles", "delete", "deck", GAMEID, "--confirm"],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["ok"] is True

    def test_launch_json(self, state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _prepare_cli_state(state_dir, monkeypatch)
        make_paramiko_patch(monkeypatch, commands=[(r"run-game", b"success\n", b"", 0)])
        result = runner.invoke(cli.app, ["--state-dir", str(state_dir), "launch", "deck", GAMEID])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["launched"] is True
        assert payload["readiness"] == "unknown"

    def test_launch_unconfirmed_exits_7(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _prepare_cli_state(state_dir, monkeypatch)
        make_paramiko_patch(monkeypatch, commands=[(r"run-game", b"", b"timeout", 0)])
        result = runner.invoke(cli.app, ["--state-dir", str(state_dir), "launch", "deck", GAMEID])
        assert result.exit_code == 7

    def test_delete_requires_confirm(self, state_dir: Path) -> None:
        result = runner.invoke(
            cli.app, ["--state-dir", str(state_dir), "titles", "delete", "deck", GAMEID]
        )
        assert result.exit_code == 2
        assert "--confirm" in result.stdout

    def test_configure_rejects_bad_env_pair(self, state_dir: Path) -> None:
        result = runner.invoke(
            cli.app,
            ["--state-dir", str(state_dir), "titles", "configure", "deck", GAMEID, "--env", "NOPE"],
        )
        assert result.exit_code == 2

    def test_configure_rejects_bad_runtime(self, state_dir: Path) -> None:
        result = runner.invoke(
            cli.app,
            [
                "--state-dir",
                str(state_dir),
                "titles",
                "configure",
                "deck",
                GAMEID,
                "--runtime",
                "nope",
            ],
        )
        assert result.exit_code == 2
        assert "proton-stable" in result.stdout


class TestJobsCli:
    def _seed_job(self, state_dir: Path) -> str:
        from steamos_devkit.jobs import JobStore
        from steamos_devkit.models import JobKind, JobState

        store = JobStore(state_dir / "jobs")
        info = store.create(kind=JobKind.DEPLOY, device="deck", gameid="mygame")
        store.start(info.id, 1234)
        store.finish(info.id, JobState.SUCCEEDED, message="deploy complete: upload complete")
        return info.id

    def test_jobs_list_and_status(self, state_dir: Path) -> None:
        job_id = self._seed_job(state_dir)
        result = runner.invoke(cli.app, ["--state-dir", str(state_dir), "jobs", "list"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)[0]["id"] == job_id
        result = runner.invoke(cli.app, ["--state-dir", str(state_dir), "jobs", "status", job_id])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["state"] == "succeeded"

    def test_jobs_list_filters(self, state_dir: Path) -> None:
        self._seed_job(state_dir)
        result = runner.invoke(
            cli.app, ["--state-dir", str(state_dir), "jobs", "list", "--state", "failed"]
        )
        assert result.exit_code == 0
        assert json.loads(result.stdout) == []
        result = runner.invoke(
            cli.app, ["--state-dir", str(state_dir), "jobs", "list", "--state", "bogus"]
        )
        assert result.exit_code == 2

    def test_jobs_status_unknown_id_exits_2(self, state_dir: Path) -> None:
        result = runner.invoke(
            cli.app,
            ["--state-dir", str(state_dir), "jobs", "status", "job_01ARZ3NDEKTSV4RRFFQ69G5FAV"],
        )
        assert result.exit_code == 2

    def test_jobs_wait_terminal_exits_0(self, state_dir: Path) -> None:
        job_id = self._seed_job(state_dir)
        result = runner.invoke(cli.app, ["--state-dir", str(state_dir), "jobs", "wait", job_id])
        assert result.exit_code == 0

    def test_jobs_cancel_terminal_is_noop(self, state_dir: Path) -> None:
        job_id = self._seed_job(state_dir)
        result = runner.invoke(cli.app, ["--state-dir", str(state_dir), "jobs", "cancel", job_id])
        assert result.exit_code == 0

    def test_jobs_cancel_running_is_unsupported(self, state_dir: Path) -> None:
        from steamos_devkit.jobs import JobStore
        from steamos_devkit.models import JobKind

        store = JobStore(state_dir / "jobs")
        info = store.create(kind=JobKind.DEPLOY, device="deck")
        store.start(info.id, 1234)
        result = runner.invoke(cli.app, ["--state-dir", str(state_dir), "jobs", "cancel", info.id])
        assert result.exit_code == 10  # UNSUPPORTED: no cross-process cancel substrate

    def test_jobs_list_human_render(self, state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from steamos_devkit.cli import common
        from steamos_devkit.jobs import JobStore
        from steamos_devkit.models import JobKind

        monkeypatch.setattr(common, "stdout_is_tty", lambda: True)
        full = self._seed_job(state_dir)
        bare = JobStore(state_dir / "jobs").create(kind=JobKind.DEPLOY)  # no device/gameid/message
        result = runner.invoke(cli.app, ["--state-dir", str(state_dir), "jobs", "list"])
        assert result.exit_code == 0
        assert full in result.stdout and bare.id in result.stdout
        assert "device=deck" in result.stdout
        assert "deploy complete" in result.stdout


class TestArtifactsCli:
    def test_artifacts_list_empty_and_get_unknown(self, state_dir: Path) -> None:
        result = runner.invoke(cli.app, ["--state-dir", str(state_dir), "artifacts", "list"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == []
        result = runner.invoke(
            cli.app,
            ["--state-dir", str(state_dir), "artifacts", "get", "art_01ARZ3NDEKTSV4RRFFQ69G5FAV"],
        )
        assert result.exit_code == 2

    def test_artifacts_list_and_get(self, state_dir: Path, tmp_path: Path) -> None:
        from steamos_devkit.artifacts import register_artifact
        from steamos_devkit.config import StateStore

        payload = tmp_path / "shot.png"
        payload.write_bytes(b"\x89PNG fake")
        artifact = register_artifact(
            StateStore(state_dir),
            job_id="job_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            device="deck",
            kind="screenshot",
            source=payload,
            remote_source="/tmp/gamescope.png",
            media_type="image/png",
        )
        result = runner.invoke(
            cli.app, ["--state-dir", str(state_dir), "artifacts", "list", "--kind", "screenshot"]
        )
        assert result.exit_code == 0
        assert json.loads(result.stdout)[0]["id"] == artifact.id
        result = runner.invoke(
            cli.app, ["--state-dir", str(state_dir), "artifacts", "get", artifact.id]
        )
        assert result.exit_code == 0
        assert json.loads(result.stdout)["path"] == str(artifact.path)
        result = runner.invoke(
            cli.app, ["--state-dir", str(state_dir), "artifacts", "list", "--kind", "bogus"]
        )
        assert result.exit_code == 2
