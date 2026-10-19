"""End-to-end diagnostics pipeline tests (M3, spec sections 7-8, audit section 4).

Device contact is scripted through the fake-SSH harness; bulk pulls run a real
subprocess against a fake rsync binary selected with ``DEVKIT_RSYNC`` that
populates the staging dir with canned payloads. This exercises
``ctx.run_process``, per-kind artifact registration, journal ``artifact``
events, and the cancel/timeout cleanup hooks (spec section 7.4) exactly as
the CLI runs them.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import paramiko
import pytest
from fake_ssh import ScriptedResponse, make_paramiko_patch
from typer.testing import CliRunner

from steamos_devkit import cli, diagnostics
from steamos_devkit.artifacts import get_artifact, list_artifacts
from steamos_devkit.cli import common
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Device, JobState

FIXTURES = Path(__file__).parent.parent / "fixtures"

runner = CliRunner()

GET_STATUS = r"steamos-get-status --json"
FIND_SCREENSHOT_RE = r'find /tmp -maxdepth 1 -type f -name "gamescope\*\.png"'
FIND_CONFIG_RE = r'find /tmp -maxdepth 1 -type f -name "config_\*\.vdf"'
DUMP_CONFIG = r"steamos-dump-controller-config"
DECKARD_CAPTURE = r"deckard-capture --json"

PNG_REMOTE = "/tmp/gamescope_2026-10-19_12-00-00.png"
PNG_PAYLOAD = b"\x89PNG fake-integration-screenshot"
FRAME_REMOTE = "/tmp/deckard-frame.png"
GAMEID = "integ_game"

_STATUS_DECK = (GET_STATUS, (FIXTURES / "steamos-get-status.json").read_bytes(), b"", 0)
_STATUS_FRAME = (GET_STATUS, (FIXTURES / "steamos-get-status-deckard.json").read_bytes(), b"", 0)


@pytest.fixture
def core(state_dir: Path) -> DevkitCore:
    return DevkitCore(state_dir=state_dir)


@pytest.fixture
def paired_key(core: DevkitCore) -> Path:
    core.state.ensure_layout()
    key_path = core.state.key_path
    paramiko.RSAKey.generate(2048).write_private_key_file(str(key_path))
    return key_path


@pytest.fixture
def device(core: DevkitCore) -> Device:
    entry = Device(name="deck", address="192.0.2.10", login="deck", added_via="explicit")
    core.state.devices.upsert(entry)
    return entry


def _fake_rsync(bin_dir: Path, argv_log: Path, pull_body: str) -> Path:
    """A fake rsync: answers --version, logs argv (blocks split by ``---``),
    and runs ``pull_body`` for --info=progress2 pulls (the devkit-utils upload
    just exits 0).
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "rsync"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf 'rsync  version 3.5.1  protocol version 33\\n'\n"
        "  exit 0\n"
        "fi\n"
        f'printf \'%s\\n\' "$@" >> "{argv_log}"\n'
        f"printf -- '---\\n' >> \"{argv_log}\"\n"
        'case " $* " in\n'
        '  *" --info=progress2 "*)\n'
        f"{pull_body}"
        "    ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _populate_body() -> str:
    """Pull body: drop a canned payload into the staging dir per source kind,
    plus one progress2 record so journal byte progress is exercised."""
    return (
        '    for a in "$@"; do dest="$a"; done\n'
        '    mkdir -p "$dest"\n'
        "    printf '%s\\r' '      1,234 100%    1.20MB/s    0:00:00 (xfr#1, to-chk=0/1)'\n"
        '    case " $* " in\n'
        '      *"Steam/logs/"*)\n'
        "        printf 'log line one\\nlog line two\\n' > \"$dest/console-linux.txt\"\n"
        "        ;;\n"
        '      *"--include=steam-"*)\n'
        "        printf 'proton\\n' > \"$dest/steam-123.log\"\n"
        "        ;;\n"
        '      *"--include=mangoapp_"*)\n'
        "        printf 'csv\\n' > \"$dest/mangoapp_x.csv\"\n"
        "        ;;\n"
        '      *":/tmp/dumps/"*)\n'
        "        printf 'dump\\n' > \"$dest/minidump.dmp\"\n"
        "        ;;\n"
        '      *"--include=config_"*)\n'
        "        printf 'vdf\\n' > \"$dest/config_123.vdf\"\n"
        "        ;;\n"
        "    esac\n"
    )


def _use_fake_rsync(
    core: DevkitCore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pull_body: str
) -> Path:
    argv_log = tmp_path / "rsync-argv.log"
    rsync = _fake_rsync(tmp_path / "bin", argv_log, pull_body)
    monkeypatch.setenv("DEVKIT_RSYNC", str(rsync))
    return argv_log


def _journal_events(core: DevkitCore, job_id: str) -> list[dict[str, object]]:
    path = core.state.jobs_dir / f"{job_id}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestFetchLogsPipeline:
    def test_full_pipeline_registers_one_artifact_per_kind(
        self,
        core: DevkitCore,
        paired_key: Path,
        device: Device,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _use_fake_rsync(core, monkeypatch, tmp_path, _populate_body())
        make_paramiko_patch(monkeypatch, commands=[_STATUS_DECK])
        job = diagnostics.fetch_logs(core, "deck", kinds=["steam", "proton", "dumps", "frametimes"])
        info = job.wait()
        assert info.state == JobState.SUCCEEDED

        # one artifact per kind, with real payload bytes
        assert len(info.artifacts) == 4
        artifacts = [get_artifact(core.state, a) for a in info.artifacts]
        by_kind = {a.kind: a for a in artifacts}
        assert set(by_kind) == {"logs", "proton_log", "dumps", "frametimes"}
        assert (
            (by_kind["logs"].path / "console-linux.txt")
            .read_text(encoding="utf-8")
            .startswith("log line one")
        )
        assert (by_kind["proton_log"].path / "steam-123.log").is_file()
        assert (by_kind["dumps"].path / "minidump.dmp").is_file()
        assert (by_kind["frametimes"].path / "mangoapp_x.csv").is_file()
        assert all(a.device == "deck" and a.job_id == job.job_id for a in by_kind.values())

        # journal carries one artifact event per kind and per-kind pull phases
        events = _journal_events(core, job.job_id)
        artifact_events = [e for e in events if e.get("t") == "artifact"]
        assert [e["artifact_id"] for e in artifact_events] == info.artifacts
        phases = [e.get("phase") for e in events if e.get("t") == "progress"]
        assert phases[0] == "preparing"
        for kind in ("steam", "proton", "dumps", "frametimes"):
            assert f"pulling {kind}" in phases
        # the fake rsync's progress2 record landed as byte progress
        pulled = [e for e in events if e.get("phase") == "pulling steam"]
        assert pulled[-1]["bytes_done"] == 1234

    def test_partial_failure_keeps_successful_kinds(
        self,
        core: DevkitCore,
        paired_key: Path,
        device: Device,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        body = (
            'case " $* " in\n'
            '  *":/tmp/dumps/"*)\n'
            "    printf 'rsync: link_stat \"/tmp/dumps\" failed: No such file or directory (2)'\n"
            "    exit 23\n"
            "    ;;\n"
            "esac\n"
        ) + _populate_body()
        _use_fake_rsync(core, monkeypatch, tmp_path, body)
        make_paramiko_patch(monkeypatch, commands=[_STATUS_DECK])
        info = diagnostics.fetch_logs(core, "deck", kinds=["steam", "dumps"]).wait()
        assert info.state == JobState.SUCCEEDED
        assert "skipped: dumps (rsync exit 23)" in (info.message or "")
        kinds = [get_artifact(core.state, a).kind for a in info.artifacts]
        assert kinds == ["logs"]

    def test_cancel_mid_pull_journals_cancelled_without_artifacts(
        self,
        core: DevkitCore,
        paired_key: Path,
        device: Device,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        marker = tmp_path / "rsync-started"
        body = f'    touch "{marker}"\n    sleep 60\n'
        _use_fake_rsync(core, monkeypatch, tmp_path, body)
        make_paramiko_patch(monkeypatch, commands=[_STATUS_DECK])
        job = diagnostics.fetch_logs(core, "deck", kinds=["steam"])
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "fake rsync never started"
        job.cancel()
        with pytest.raises(DevkitError) as exc_info:
            job.wait()
        assert exc_info.value.code == ErrorCode.CANCELLED
        info = job.info()
        assert info.state == JobState.CANCELLED
        # spec section 7.4: local partial staging removed, no artifact registered
        assert info.artifacts == []
        assert list_artifacts(core.state) == []
        events = _journal_events(core, job.job_id)
        assert events[-1]["t"] == "cancelled"
        # a fresh process (recovery sweep) must not disturb a terminal journal
        second = DevkitCore(state_dir=core.state.root)
        assert second.job_runner.recovered == []

    def test_cli_logs_end_to_end_with_excerpt(
        self,
        core: DevkitCore,
        paired_key: Path,
        device: Device,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _use_fake_rsync(core, monkeypatch, tmp_path, _populate_body())
        make_paramiko_patch(monkeypatch, commands=[_STATUS_DECK])
        monkeypatch.setattr(common, "stdout_is_tty", lambda: True)
        result = runner.invoke(
            cli.app, ["--state-dir", str(core.state.root), "logs", "deck", "--kind", "steam"]
        )
        assert result.exit_code == 0, result.output
        assert "succeeded" in result.output
        assert "--- console-linux.txt (last 200 lines) ---" in result.output
        assert "log line one" in result.output
        # the artifact is listed and retrievable via the CLI (M3 exit check)
        monkeypatch.setattr(common, "stdout_is_tty", lambda: False)  # back to JSON mode
        result = runner.invoke(
            cli.app, ["--state-dir", str(core.state.root), "artifacts", "list", "--kind", "logs"]
        )
        payload = json.loads(result.stdout)
        assert len(payload) == 1
        assert payload[0]["kind"] == "logs"
        result = runner.invoke(
            cli.app, ["--state-dir", str(core.state.root), "artifacts", "get", payload[0]["id"]]
        )
        assert result.exit_code == 0
        assert json.loads(result.stdout)["path"].endswith("payload")


class TestScreenshotPipeline:
    def test_deck_screenshot_registers_png_artifact(
        self,
        core: DevkitCore,
        paired_key: Path,
        device: Device,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _use_fake_rsync(core, monkeypatch, tmp_path, "")
        fake = make_paramiko_patch(
            monkeypatch,
            commands=[
                _STATUS_DECK,
                (r"rm -f /tmp/gamescope", b"", b"", 0),
                (r"xprop", b"", b"", 0),
                ScriptedResponse(FIND_SCREENSHOT_RE, b"", b"", 0, max_uses=1),
                ScriptedResponse(FIND_SCREENSHOT_RE, f"{PNG_REMOTE}\n".encode(), b"", 0),
            ],
            filesystem={PNG_REMOTE: PNG_PAYLOAD},
        )
        job = diagnostics.screenshot(core, "deck", mode=3)
        info = job.wait()
        assert info.state == JobState.SUCCEEDED
        artifact = get_artifact(core.state, info.artifacts[0])
        assert artifact.kind == "screenshot"
        assert artifact.media_type == "image/png"
        assert artifact.path.read_bytes() == PNG_PAYLOAD
        events = _journal_events(core, job.job_id)
        assert [e.get("t") for e in events][-1] == "succeeded"
        assert any(e.get("t") == "artifact" for e in events)
        phases = [e.get("phase") for e in events if e.get("t") == "progress"]
        assert phases == ["preparing", "capturing", "downloading"]
        # the device copy is removed after a successful pull
        assert f"rm -f {PNG_REMOTE}" in fake.commands_run

    def test_frame_screenshot_pipeline(
        self,
        core: DevkitCore,
        paired_key: Path,
        device: Device,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _use_fake_rsync(core, monkeypatch, tmp_path, "")
        capture_json = json.dumps({"success": True, "output": FRAME_REMOTE})
        fake = make_paramiko_patch(
            monkeypatch,
            commands=[
                _STATUS_FRAME,
                (DECKARD_CAPTURE, capture_json.encode(), b"", 0),
            ],
            filesystem={FRAME_REMOTE: PNG_PAYLOAD},
        )
        info = diagnostics.screenshot(core, "deck").wait()
        assert info.state == JobState.SUCCEEDED
        assert not any("xprop" in c for c in fake.commands_run)
        assert f"rm {FRAME_REMOTE}" in fake.commands_run
        artifact = get_artifact(core.state, info.artifacts[0])
        assert artifact.kind == "screenshot"
        assert artifact.path.read_bytes() == PNG_PAYLOAD

    def test_cancel_during_poll_wipes_gamescope_files(
        self,
        core: DevkitCore,
        paired_key: Path,
        device: Device,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _use_fake_rsync(core, monkeypatch, tmp_path, "")
        fake = make_paramiko_patch(
            monkeypatch,
            commands=[
                _STATUS_DECK,
                (r"rm -f /tmp/gamescope", b"", b"", 0),
                (r"xprop", b"", b"", 0),
                # find never matches: unscripted -> exit 127 -> treated as absent
            ],
        )
        job = diagnostics.screenshot(core, "deck")
        find_cmd = 'find /tmp -maxdepth 1 -type f -name "gamescope*.png"'
        deadline = time.monotonic() + 10
        while find_cmd not in fake.commands_run:
            assert time.monotonic() < deadline, "job never reached the find poll"
            time.sleep(0.01)
        job.cancel()
        with pytest.raises(DevkitError) as exc_info:
            job.wait()
        assert exc_info.value.code == ErrorCode.CANCELLED
        assert job.info().state == JobState.CANCELLED
        # spec section 7.4: initial wipe plus the cleanup hook's wipe
        assert fake.commands_run.count("rm -f /tmp/gamescope*.png") == 2
        assert job.info().artifacts == []


class TestControllerConfigPipeline:
    def test_full_pipeline_registers_vdf_artifact(
        self,
        core: DevkitCore,
        paired_key: Path,
        device: Device,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _use_fake_rsync(core, monkeypatch, tmp_path, _populate_body())
        fake = make_paramiko_patch(
            monkeypatch,
            commands=[
                (DUMP_CONFIG, b'{"success": "config written"}', b"", 0),
                (FIND_CONFIG_RE, b"/tmp/config_123.vdf\n", b"", 0),
            ],
        )
        job = diagnostics.controller_config(core, "deck", GAMEID)
        info = job.wait()
        assert info.state == JobState.SUCCEEDED
        assert info.gameid == GAMEID
        artifact = get_artifact(core.state, info.artifacts[0])
        assert artifact.kind == "controller_config"
        assert (artifact.path / "config_123.vdf").is_file()
        events = _journal_events(core, job.job_id)
        assert any(e.get("t") == "artifact" for e in events)
        phases = [e.get("phase") for e in events if e.get("t") == "progress"]
        # the rsync progress2 records add further "pulling" events
        assert phases[:4] == ["preparing", "dumping", "waiting", "pulling"]
        assert fake.commands_run[:2] == [
            "rm -f /tmp/config_*.tmp",
            f"python3 ~/devkit-utils/steamos-dump-controller-config --gameid {GAMEID}",
        ]

    def test_cancel_during_pull_runs_cleanup_hook(
        self,
        core: DevkitCore,
        paired_key: Path,
        device: Device,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        marker = tmp_path / "rsync-started"
        body = f'    touch "{marker}"\n    sleep 60\n'
        _use_fake_rsync(core, monkeypatch, tmp_path, body)
        fake = make_paramiko_patch(
            monkeypatch,
            commands=[
                (DUMP_CONFIG, b'{"success": "ok"}', b"", 0),
                (FIND_CONFIG_RE, b"/tmp/config_123.vdf\n", b"", 0),
            ],
        )
        job = diagnostics.controller_config(core, "deck", GAMEID)
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "fake rsync never started"
        job.cancel()
        with pytest.raises(DevkitError) as exc_info:
            job.wait()
        assert exc_info.value.code == ErrorCode.CANCELLED
        assert job.info().artifacts == []
        assert list_artifacts(core.state) == []
        # spec section 7.4: temp and dump files wiped remotely
        assert "rm -f /tmp/config_*.tmp /tmp/config_*.vdf" in fake.commands_run

    def test_cli_controller_config_end_to_end(
        self,
        core: DevkitCore,
        paired_key: Path,
        device: Device,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _use_fake_rsync(core, monkeypatch, tmp_path, _populate_body())
        make_paramiko_patch(
            monkeypatch,
            commands=[
                (DUMP_CONFIG, b'{"success": "ok"}', b"", 0),
                (FIND_CONFIG_RE, b"/tmp/config_123.vdf\n", b"", 0),
            ],
        )
        result = runner.invoke(
            cli.app, ["--state-dir", str(core.state.root), "controller-config", "deck", GAMEID]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["state"] == "succeeded"
        assert len(payload["artifacts"]) == 1
