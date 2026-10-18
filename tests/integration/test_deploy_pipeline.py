"""End-to-end deploy pipeline tests (M2, spec sections 7-8, audit section 3).

Device contact is scripted through the fake-SSH harness; bulk transfer runs a
real subprocess against a fake rsync binary selected with ``DEVKIT_RSYNC`` —
this exercises ``ctx.run_process``, the progress2 record pump, cancellation
process-group kills, and journal phases exactly as the CLI runs them.
"""

from __future__ import annotations

import json
import shlex
import time
from pathlib import Path

import paramiko
import pytest
from fake_ssh import make_paramiko_patch
from typer.testing import CliRunner

from steamos_devkit import cli
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.jobs import JobStore
from steamos_devkit.models import DeploySpec, Device, JobKind, JobProgress, JobState, TitleConfig
from steamos_devkit.titles import deploy

FIXTURES = Path(__file__).parent.parent / "fixtures"
GAMEID = "integ_game"
PREPARE_JSON = json.dumps({"user": "deck", "directory": f"/home/deck/devkit-game/{GAMEID}"})

runner = CliRunner()


def _fake_rsync(bin_dir: Path, argv_log: Path, transfer_body: str, *, gate: bool = True) -> Path:
    """A fake rsync: answers --version, logs argv, runs transfer_body.

    With ``gate`` (default) the body runs only for the progress2 deploy
    invocation — the devkit-utils upload just exits 0. Without it the body
    runs on every non-version invocation (used for the preview dry-run).
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    if gate:
        body_block = f'case " $* " in\n  *" --info=progress2 "*)\n{transfer_body}\n    ;;\nesac\n'
    else:
        body_block = f"{transfer_body}\n"
    path = bin_dir / "rsync"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf 'rsync  version 3.5.1  protocol version 33\\n'\n"
        "  exit 0\n"
        "fi\n"
        f'printf \'%s\\n\' "$@" >> "{argv_log}"\n'
        f"{body_block}"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _golden_transfer(fixture: str = "rsync-progress2-success.txt") -> str:
    return f'    cat "{FIXTURES / fixture}"'


def _make_core(state_dir: Path, monkeypatch: pytest.MonkeyPatch, rsync: Path) -> DevkitCore:
    monkeypatch.setenv("DEVKIT_RSYNC", str(rsync))
    core = DevkitCore(state_dir=state_dir)
    core.state.ensure_layout()
    paramiko.RSAKey.generate(2048).write_private_key_file(str(core.state.key_path))
    core.state.devices.upsert(
        Device(name="deck", address="192.0.2.10", login="deck", added_via="explicit")
    )
    return core


def _source_tree(tmp_path: Path) -> Path:
    source = tmp_path / "build"
    source.mkdir(exist_ok=True)
    (source / "game.sh").write_text("#!/bin/sh\necho hello\n", encoding="utf-8")
    return source


def _spec(source: Path, **overrides: object) -> DeploySpec:
    kwargs: dict[str, object] = {"device": "deck", "gameid": GAMEID, "source_dir": source}
    kwargs.update(overrides)
    return DeploySpec(**kwargs)  # type: ignore[arg-type]


def _journal_events(core: DevkitCore, job_id: str) -> list[dict[str, object]]:
    path = core.state.jobs_dir / f"{job_id}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _progress_events(core: DevkitCore, job_id: str) -> list[dict[str, object]]:
    return [e for e in _journal_events(core, job_id) if e.get("t") == "progress"]


class TestDeployPipeline:
    def test_full_pipeline_register_and_launch(
        self, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        argv_log = tmp_path / "rsync-argv.log"
        rsync = _fake_rsync(tmp_path / "bin", argv_log, _golden_transfer())
        core = _make_core(state_dir, monkeypatch, rsync)
        ssh = make_paramiko_patch(
            monkeypatch,
            commands=[
                (r"steamos-prepare-upload", PREPARE_JSON, b"", 0),
                (r"create-shortcut", b'{"success": "registered with steam"}', b"", 0),
                (r"run-game", b"success\n", b"", 0),
            ],
        )
        handle = deploy(core, "deck", _spec(_source_tree(tmp_path), launch=True))
        info = handle.wait()
        assert info.state == JobState.SUCCEEDED
        assert info.message is not None
        assert info.message.startswith("deploy complete")
        assert "upload complete" in info.message
        assert "registered with Steam" in info.message
        assert "launch requested" in info.message

        # device helper ordering: prepare < register < launch
        commands = ssh.commands_run
        prepare_at = next(i for i, c in enumerate(commands) if "prepare-upload" in c)
        register_at = next(i for i, c in enumerate(commands) if "create-shortcut" in c)
        launch_at = next(i for i, c in enumerate(commands) if "run-game" in c)
        assert prepare_at < register_at < launch_at
        assert f"--gameid {GAMEID}" in commands[prepare_at]

        # the real rsync subprocess got the deploy argv with resume+progress flags
        argv_lines = argv_log.read_text(encoding="utf-8").splitlines()
        assert "--info=progress2" in argv_lines
        assert "--partial" in argv_lines
        assert f"deck@192.0.2.10:/home/deck/devkit-game/{GAMEID}/" in argv_lines

        # journal phases and progress2 wiring
        phases = [e.get("phase") for e in _progress_events(core, handle.job_id)]
        assert phases[0] == "preparing"
        assert "transferring" in phases and "registering" in phases and "launching" in phases
        transferring = [
            e for e in _progress_events(core, handle.job_id) if e.get("phase") == "transferring"
        ]
        assert transferring[-1]["bytes_done"] == 44_654_666
        assert transferring[-1]["items_done"] == 7
        assert transferring[-1]["bytes_total"] == transferring[-1]["bytes_done"]

        # deploy remembers the source tree locally for the title
        stored = core.state.load_title(GAMEID)
        assert stored is not None
        assert stored.source_dir == _source_tree(tmp_path)

    def test_config_applied_before_register(
        self, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rsync = _fake_rsync(tmp_path / "bin", tmp_path / "argv.log", _golden_transfer())
        core = _make_core(state_dir, monkeypatch, rsync)
        ssh = make_paramiko_patch(
            monkeypatch,
            commands=[
                (r"steamos-prepare-upload", PREPARE_JSON, b"", 0),
                (r"create-shortcut", b'{"success": "ok"}', b"", 0),
            ],
        )
        config = TitleConfig(entrypoint="bin/run.sh", args=["-v"], runtime="proton-stable")
        handle = deploy(core, "deck", _spec(_source_tree(tmp_path), config=config))
        assert handle.wait().state == JobState.SUCCEEDED
        fs = ssh.filesystem
        assert json.loads(fs[f"/home/deck/devkit-game/{GAMEID}-argv.json"]) == ["bin/run.sh", "-v"]
        settings = json.loads(fs[f"/home/deck/devkit-game/{GAMEID}-settings.json"])
        assert settings["compat_tool"] == "proton-stable"
        register_cmd = next(c for c in ssh.commands_run if "create-shortcut" in c)
        parms = json.loads(shlex.split(register_cmd)[3])
        assert parms["argv"] == ["bin/run.sh", "-v"]
        assert parms["settings"]["compat_tool"] == "proton-stable"
        stored = core.state.load_title(GAMEID)
        assert stored is not None and stored.config.entrypoint == "bin/run.sh"

    def test_launch_false_skips_run_game(
        self, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rsync = _fake_rsync(tmp_path / "bin", tmp_path / "argv.log", _golden_transfer())
        core = _make_core(state_dir, monkeypatch, rsync)
        ssh = make_paramiko_patch(
            monkeypatch,
            commands=[
                (r"steamos-prepare-upload", PREPARE_JSON, b"", 0),
                (r"create-shortcut", b'{"success": "ok"}', b"", 0),
            ],
        )
        handle = deploy(core, "deck", _spec(_source_tree(tmp_path)))  # launch=False default
        info = handle.wait()
        assert info.state == JobState.SUCCEEDED
        assert not any("run-game" in c for c in ssh.commands_run)
        phases = [e.get("phase") for e in _progress_events(core, handle.job_id)]
        assert "launching" not in phases

    def test_no_register_upload_only(
        self, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rsync = _fake_rsync(tmp_path / "bin", tmp_path / "argv.log", _golden_transfer())
        core = _make_core(state_dir, monkeypatch, rsync)
        ssh = make_paramiko_patch(
            monkeypatch, commands=[(r"steamos-prepare-upload", PREPARE_JSON, b"", 0)]
        )
        handle = deploy(core, "deck", _spec(_source_tree(tmp_path), register=False))
        assert handle.wait().state == JobState.SUCCEEDED
        assert not any("create-shortcut" in c for c in ssh.commands_run)

    def test_registration_failure_fails_job_after_complete_upload(
        self, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rsync = _fake_rsync(tmp_path / "bin", tmp_path / "argv.log", _golden_transfer())
        core = _make_core(state_dir, monkeypatch, rsync)
        ssh = make_paramiko_patch(
            monkeypatch,
            commands=[
                (r"steamos-prepare-upload", PREPARE_JSON, b"", 0),
                (r"create-shortcut", b"", b"boom: steam pipe unavailable", 1),
            ],
        )
        handle = deploy(core, "deck", _spec(_source_tree(tmp_path), launch=True))
        with pytest.raises(DevkitError) as exc_info:
            handle.wait()
        assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
        info = handle.info()
        assert info.state == JobState.FAILED
        assert info.code == ErrorCode.REMOTE_COMMAND_FAILED
        # the upload itself completed: full golden progress landed first
        transferring = [
            e for e in _progress_events(core, handle.job_id) if e.get("phase") == "transferring"
        ]
        assert transferring[-1]["bytes_done"] == 44_654_666
        # launch was never attempted
        assert not any("run-game" in c for c in ssh.commands_run)
        # `jobs wait` on the failed job exits 7 (spec section 10)
        result = runner.invoke(
            cli.app, ["--state-dir", str(state_dir), "jobs", "wait", handle.job_id]
        )
        assert result.exit_code == 7

    def test_cancel_mid_transfer(
        self, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marker = tmp_path / "rsync-started"
        argv_log = tmp_path / "argv.log"
        rsync = _fake_rsync(tmp_path / "bin", argv_log, f'    touch "{marker}"\n    sleep 60')
        core = _make_core(state_dir, monkeypatch, rsync)
        ssh = make_paramiko_patch(
            monkeypatch, commands=[(r"steamos-prepare-upload", PREPARE_JSON, b"", 0)]
        )
        handle = deploy(core, "deck", _spec(_source_tree(tmp_path)))
        deadline = time.time() + 10
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "fake rsync never started"
        handle.cancel()
        with pytest.raises(DevkitError) as exc_info:
            handle.wait()
        assert exc_info.value.code == ErrorCode.CANCELLED
        assert handle.info().state == JobState.CANCELLED
        # registration never ran; the rsync argv carried --partial so the
        # remote partial file stays resumable (spec section 7.4)
        assert not any("create-shortcut" in c for c in ssh.commands_run)
        assert "--partial" in argv_log.read_text(encoding="utf-8").splitlines()
        # a fresh process (recovery sweep) must not disturb a terminal journal
        second = DevkitCore(state_dir=state_dir)
        assert second.job_runner.recovered == []

    def test_interrupted_journal_recovery(
        self, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # simulate process death mid-upload: a journal left at a progress event
        store = JobStore(state_dir / "jobs")
        stale = store.create(kind=JobKind.DEPLOY, device="deck", gameid=GAMEID)
        store.start(stale.id, 4242)
        store.progress(stale.id, JobProgress(phase="transferring", bytes_done=100, bytes_total=200))
        core = _make_core(
            state_dir, monkeypatch, _fake_rsync(tmp_path / "bin", tmp_path / "argv.log", "")
        )
        assert core.job_runner.recovered == [stale.id]
        assert core.job_store.load(stale.id).state == JobState.INTERRUPTED


class TestDeployCli:
    def test_cli_deploy_waits_for_success(
        self, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rsync = _fake_rsync(tmp_path / "bin", tmp_path / "argv.log", _golden_transfer())
        _make_core(state_dir, monkeypatch, rsync)
        make_paramiko_patch(
            monkeypatch,
            commands=[
                (r"steamos-prepare-upload", PREPARE_JSON, b"", 0),
                (r"create-shortcut", b'{"success": "ok"}', b"", 0),
            ],
        )
        result = runner.invoke(
            cli.app,
            ["--state-dir", str(state_dir), "deploy", "deck", GAMEID, str(_source_tree(tmp_path))],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["state"] == "succeeded"
        assert "deploy complete" in payload["message"]

    def test_cli_deploy_no_wait_then_jobs_wait(
        self, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rsync = _fake_rsync(tmp_path / "bin", tmp_path / "argv.log", _golden_transfer())
        _make_core(state_dir, monkeypatch, rsync)
        make_paramiko_patch(
            monkeypatch,
            commands=[
                (r"steamos-prepare-upload", PREPARE_JSON, b"", 0),
                (r"create-shortcut", b'{"success": "ok"}', b"", 0),
            ],
        )
        result = runner.invoke(
            cli.app,
            [
                "--state-dir",
                str(state_dir),
                "deploy",
                "deck",
                GAMEID,
                str(_source_tree(tmp_path)),
                "--no-wait",
            ],
        )
        assert result.exit_code == 0, result.output
        job_id = json.loads(result.stdout)["job_id"]
        result = runner.invoke(cli.app, ["--state-dir", str(state_dir), "jobs", "wait", job_id])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["state"] == "succeeded"

    def test_cli_deploy_bad_filter_mode_exits_2(
        self, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_core(state_dir, monkeypatch, _fake_rsync(tmp_path / "bin", tmp_path / "a.log", ""))
        result = runner.invoke(
            cli.app,
            [
                "--state-dir",
                str(state_dir),
                "deploy",
                "deck",
                GAMEID,
                str(_source_tree(tmp_path)),
                "--filter-mode",
                "bogus",
            ],
        )
        assert result.exit_code == 2

    def test_cli_deploy_missing_source_exits_2(
        self, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_core(state_dir, monkeypatch, _fake_rsync(tmp_path / "bin", tmp_path / "a.log", ""))
        result = runner.invoke(
            cli.app,
            ["--state-dir", str(state_dir), "deploy", "deck", GAMEID, str(tmp_path / "missing")],
        )
        assert result.exit_code == 2

    def test_cli_deploy_reserved_gameid_exits_10(
        self, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_core(state_dir, monkeypatch, _fake_rsync(tmp_path / "bin", tmp_path / "a.log", ""))
        result = runner.invoke(
            cli.app,
            ["--state-dir", str(state_dir), "deploy", "deck", "steam", str(_source_tree(tmp_path))],
        )
        assert result.exit_code == 10  # UNSUPPORTED

    def test_cli_preview(
        self, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rsync = _fake_rsync(
            tmp_path / "bin",
            tmp_path / "argv.log",
            '    cat "' + str(FIXTURES / "rsync-itemize-dry-run.txt") + '"\n'
            "    printf 'Total transferred file size: 44,654,742 bytes\\n'",
            gate=False,
        )
        _make_core(state_dir, monkeypatch, rsync)
        # preview never touches paramiko; only rsync -n over (faked) ssh
        result = runner.invoke(
            cli.app,
            [
                "--state-dir",
                str(state_dir),
                "deploy",
                "deck",
                GAMEID,
                str(_source_tree(tmp_path)),
                "--preview",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["files_added"] == 2
        assert payload["bytes_to_transfer"] == 44_654_742

    def test_cli_deploy_bad_gameid_exits_2(
        self, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_core(state_dir, monkeypatch, _fake_rsync(tmp_path / "bin", tmp_path / "a.log", ""))
        result = runner.invoke(
            cli.app,
            ["--state-dir", str(state_dir), "deploy", "deck", "1bad", str(_source_tree(tmp_path))],
        )
        assert result.exit_code == 2  # pydantic DeploySpec rejection maps to VALIDATION

    def test_cli_preview_without_stats_reports_warning(
        self, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rsync = _fake_rsync(
            tmp_path / "bin",
            tmp_path / "argv.log",
            '    cat "' + str(FIXTURES / "rsync-itemize-dry-run.txt") + '"',
            gate=False,
        )
        _make_core(state_dir, monkeypatch, rsync)
        result = runner.invoke(
            cli.app,
            [
                "--state-dir",
                str(state_dir),
                "deploy",
                "deck",
                GAMEID,
                str(_source_tree(tmp_path)),
                "--preview",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["bytes_to_transfer"] == 0
        assert payload["warnings"]
