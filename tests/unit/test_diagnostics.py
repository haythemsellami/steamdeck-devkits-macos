"""Unit tests for diagnostics operations (spec section 8; audit sections 4-5).

Command-string exactness (xprop mode mapping, the gamescope/config find
polls, dump-controller-config, rm cleanups) and Frame-vs-Deck branching are
verified against fake_ssh command records; rsync pull argv (per-kind filters
and remote sources) runs against a fake rsync binary selected with
``DEVKIT_RSYNC``, exactly like the deploy pipeline tests. Job-level cancel
cleanup lives in tests/integration/test_diagnostics_pipeline.py.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import paramiko
import pytest
from fake_ssh import ScriptedResponse, make_paramiko_patch
from typer.testing import CliRunner

from steamos_devkit import cli, diagnostics
from steamos_devkit.artifacts import get_artifact, register_artifact
from steamos_devkit.cli import common
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.jobs import JobContext
from steamos_devkit.models import Device, JobInfo, JobKind, JobState

runner = CliRunner()

GET_STATUS = r"steamos-get-status --json"
DECKARD_CAPTURE = r"deckard-capture --json"
XPROP = r"xprop -root -f GAMESCOPECTRL_DEBUG_REQUEST_SCREENSHOT"
FIND_SCREENSHOT_CMD = 'find /tmp -maxdepth 1 -type f -name "gamescope*.png"'
FIND_SCREENSHOT_RE = r'find /tmp -maxdepth 1 -type f -name "gamescope\*\.png"'
FIND_CONFIG_CMD = 'find /tmp -maxdepth 1 -type f -name "config_*.vdf"'
FIND_CONFIG_RE = r'find /tmp -maxdepth 1 -type f -name "config_\*\.vdf"'
DUMP_CONFIG = r"steamos-dump-controller-config"

PNG_REMOTE = "/tmp/gamescope_2026-10-19_12-00-00.png"
PNG_PAYLOAD = b"\x89PNG fake-screenshot"
FRAME_REMOTE = "/tmp/deckard-screenshot.png"

GAMEID = "unit_game"


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


def _fake_rsync(bin_dir: Path, argv_log: Path, pull_body: str = "") -> Path:
    """A fake rsync: answers --version, logs argv (one invocation per block,
    separated by ``---``), runs ``pull_body`` for --info=progress2 pulls only
    (the devkit-utils upload just exits 0).
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


def _rsync_invocations(argv_log: Path) -> list[list[str]]:
    text = argv_log.read_text(encoding="utf-8")
    return [block.splitlines() for block in text.split("---\n") if block.strip()]


def _pull_invocations(argv_log: Path) -> list[list[str]]:
    return [args for args in _rsync_invocations(argv_log) if "--info=progress2" in args]


def _use_fake_rsync(
    core: DevkitCore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pull_body: str = "",
) -> Path:
    argv_log = tmp_path / "rsync-argv.log"
    rsync = _fake_rsync(tmp_path / "bin", argv_log, pull_body)
    monkeypatch.setenv("DEVKIT_RSYNC", str(rsync))
    return argv_log


def _status_rule(fixture: str, fixture_file: Callable[[str], str]) -> tuple[str, bytes, bytes, int]:
    return (GET_STATUS, fixture_file(fixture).encode(), b"", 0)


def _deck_screenshot_rules(fixture_file: Callable[[str], str]) -> list:
    """The happy-path Deck commands: wipe, xprop trigger, find hit."""
    return [
        _status_rule("steamos-get-status.json", fixture_file),
        (r"rm -f /tmp/gamescope", b"", b"", 0),
        (XPROP, b"", b"", 0),
        (FIND_SCREENSHOT_RE, f"{PNG_REMOTE}\n".encode(), b"", 0),
    ]


def _fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    def fast(self: JobContext, seconds: float) -> None:
        self._cancel.wait(min(seconds, 0.01))
        self.check_cancelled()

    monkeypatch.setattr(JobContext, "sleep", fast)


# --- fetch_logs -----------------------------------------------------------------


def test_fetch_logs_default_is_steam_only(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_file: Callable[[str], str],
) -> None:
    argv_log = _use_fake_rsync(core, monkeypatch, tmp_path)
    fake = make_paramiko_patch(
        monkeypatch, commands=[_status_rule("steamos-get-status.json", fixture_file)]
    )
    info = diagnostics.fetch_logs(core, "deck").wait()
    assert info.state == JobState.SUCCEEDED
    assert info.kind == JobKind.FETCH_LOGS
    assert "logs fetched: steam" in (info.message or "")
    pulls = _pull_invocations(argv_log)
    assert len(pulls) == 1
    # the dest staging dir is a tempdir; only the remote source is pinned
    assert "deck@192.0.2.10:/home/deck/.local/share/Steam/logs/" in pulls[0]
    # the status probe ran once to look for a steamvr logpath
    assert fake.commands_run == ["python3 ~/devkit-utils/steamos-get-status --json"]
    artifacts = [get_artifact(core.state, a) for a in info.artifacts]
    assert [a.kind for a in artifacts] == ["logs"]
    assert artifacts[0].remote_source == "/home/deck/.local/share/Steam/logs"


def test_fetch_logs_steamvr_logpath_rides_along_first(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_file: Callable[[str], str],
) -> None:
    argv_log = _use_fake_rsync(core, monkeypatch, tmp_path)
    status = json.loads(fixture_file("steamos-get-status.json"))
    status["steamvr_logpath"] = "/home/deck/.local/share/SteamVR/logs"
    make_paramiko_patch(monkeypatch, commands=[(GET_STATUS, json.dumps(status).encode(), b"", 0)])
    info = diagnostics.fetch_logs(core, "deck", kinds=["steam"]).wait()
    assert info.state == JobState.SUCCEEDED
    pulls = _pull_invocations(argv_log)
    assert len(pulls) == 1
    sources = [a for a in pulls[0] if a.startswith("deck@192.0.2.10:")]
    # upstream priority: steamvr first, then the steam logs path (:1422-1426)
    assert sources == [
        "deck@192.0.2.10:/home/deck/.local/share/SteamVR/logs/",
        "deck@192.0.2.10:/home/deck/.local/share/Steam/logs/",
    ]
    artifact = get_artifact(core.state, info.artifacts[0])
    assert artifact.remote_source == (
        "/home/deck/.local/share/SteamVR/logs /home/deck/.local/share/Steam/logs"
    )


def test_fetch_logs_all_kinds_filters_and_artifacts(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_file: Callable[[str], str],
) -> None:
    argv_log = _use_fake_rsync(core, monkeypatch, tmp_path)
    make_paramiko_patch(
        monkeypatch, commands=[_status_rule("steamos-get-status.json", fixture_file)]
    )
    info = diagnostics.fetch_logs(
        core, "deck", kinds=["steam", "proton", "dumps", "frametimes"]
    ).wait()
    assert info.state == JobState.SUCCEEDED
    pulls = _pull_invocations(argv_log)
    assert len(pulls) == 4
    steam, proton, dumps, frametimes = pulls
    assert "deck@192.0.2.10:/home/deck/.local/share/Steam/logs/" in steam
    assert "--include=steam-*.log" in proton
    assert "--exclude=*" in proton
    assert "deck@192.0.2.10:/home/deck/" in proton
    assert "deck@192.0.2.10:/tmp/dumps/" in dumps
    assert "--include=mangoapp_*.csv" in frametimes
    assert "deck@192.0.2.10:/home/deck/" in frametimes
    kinds = [get_artifact(core.state, a).kind for a in info.artifacts]
    assert kinds == ["logs", "proton_log", "dumps", "frametimes"]
    # every pull reports its own progress phase
    journal = (core.state.jobs_dir / f"{info.id}.jsonl").read_text(encoding="utf-8")
    for phase in ("pulling steam", "pulling proton", "pulling dumps", "pulling frametimes"):
        assert phase in journal


def test_fetch_logs_proton_only_skips_status_probe(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    argv_log = _use_fake_rsync(core, monkeypatch, tmp_path)
    fake = make_paramiko_patch(monkeypatch, commands=[])
    info = diagnostics.fetch_logs(core, "deck", kinds=["proton"]).wait()
    assert info.state == JobState.SUCCEEDED
    # no devkit-utils upload and no status probe: pure rsync pull
    assert fake.commands_run == []
    assert len(_rsync_invocations(argv_log)) == 1  # the pull only


def test_fetch_logs_missing_remote_dir_is_tolerated(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_file: Callable[[str], str],
) -> None:
    body = (
        'case " $* " in\n'
        '  *":/tmp/dumps/"*)\n'
        "    printf 'rsync: link_stat \"/tmp/dumps\" failed: No such file or directory (2)'\n"
        "    exit 23\n"
        "    ;;\n"
        "esac\n"
    )
    _use_fake_rsync(core, monkeypatch, tmp_path, body)
    make_paramiko_patch(
        monkeypatch, commands=[_status_rule("steamos-get-status.json", fixture_file)]
    )
    info = diagnostics.fetch_logs(core, "deck", kinds=["steam", "dumps"]).wait()
    assert info.state == JobState.SUCCEEDED
    assert "skipped: dumps (rsync exit 23)" in (info.message or "")
    kinds = [get_artifact(core.state, a).kind for a in info.artifacts]
    assert kinds == ["logs"]


def test_fetch_logs_all_kinds_fail_fails_job(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_file: Callable[[str], str],
) -> None:
    body = "printf 'rsync: link_stat failed: No such file or directory (2)'\nexit 23\n"
    _use_fake_rsync(core, monkeypatch, tmp_path, body)
    make_paramiko_patch(
        monkeypatch, commands=[_status_rule("steamos-get-status.json", fixture_file)]
    )
    job = diagnostics.fetch_logs(core, "deck", kinds=["steam", "dumps"])
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    assert "every requested log pull failed" in exc_info.value.message
    info = job.info()
    assert info.state == JobState.FAILED
    assert info.artifacts == []


def test_fetch_logs_status_probe_failure_is_a_note(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _use_fake_rsync(core, monkeypatch, tmp_path)
    make_paramiko_patch(monkeypatch, commands=[(GET_STATUS, b"", b"boom", 1)])
    info = diagnostics.fetch_logs(core, "deck", kinds=["steam"]).wait()
    assert info.state == JobState.SUCCEEDED
    assert "status probe failed" in (info.message or "")


def test_fetch_logs_rejects_unknown_kind(core: DevkitCore, device: Device) -> None:
    with pytest.raises(DevkitError) as exc_info:
        diagnostics.fetch_logs(core, "deck", kinds=["bogus"])  # type: ignore[list-item]
    assert exc_info.value.code == ErrorCode.VALIDATION


def test_fetch_logs_requires_pairing_key(core: DevkitCore, device: Device) -> None:
    with pytest.raises(DevkitError) as exc_info:
        diagnostics.fetch_logs(core, "deck", kinds=["proton"])
    assert exc_info.value.code == ErrorCode.PAIRING_FAILED


# --- screenshot -----------------------------------------------------------------


@pytest.mark.parametrize("mode", [1, 2, 3, 4])
def test_screenshot_deck_mode_mapping(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_file: Callable[[str], str],
    mode: int,
) -> None:
    _use_fake_rsync(core, monkeypatch, tmp_path)
    fake = make_paramiko_patch(
        monkeypatch,
        commands=_deck_screenshot_rules(fixture_file),
        filesystem={PNG_REMOTE: PNG_PAYLOAD},
    )
    info = diagnostics.screenshot(core, "deck", mode=mode).wait()  # type: ignore[arg-type]
    assert info.state == JobState.SUCCEEDED
    assert info.kind == JobKind.SCREENSHOT
    assert fake.commands_run == [
        "python3 ~/devkit-utils/steamos-get-status --json",
        "rm -f /tmp/gamescope*.png",
        "DISPLAY=:0 xprop -root -f GAMESCOPECTRL_DEBUG_REQUEST_SCREENSHOT 32c "
        f"-set GAMESCOPECTRL_DEBUG_REQUEST_SCREENSHOT {mode}",
        FIND_SCREENSHOT_CMD,
        f"rm -f {PNG_REMOTE}",
    ]
    artifact = get_artifact(core.state, info.artifacts[0])
    assert artifact.kind == "screenshot"
    assert artifact.media_type == "image/png"
    assert artifact.remote_source == PNG_REMOTE
    assert artifact.path.read_bytes() == PNG_PAYLOAD


def test_screenshot_deck_polls_until_file_appears(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_file: Callable[[str], str],
) -> None:
    _fast_sleep(monkeypatch)
    _use_fake_rsync(core, monkeypatch, tmp_path)
    find_cmd = FIND_SCREENSHOT_CMD
    fake = make_paramiko_patch(
        monkeypatch,
        commands=[
            _status_rule("steamos-get-status.json", fixture_file),
            (r"rm -f /tmp/gamescope", b"", b"", 0),
            (XPROP, b"", b"", 0),
            ScriptedResponse(FIND_SCREENSHOT_RE, b"", b"", 0, max_uses=2),
            ScriptedResponse(FIND_SCREENSHOT_RE, f"{PNG_REMOTE}\n".encode(), b"", 0),
        ],
        filesystem={PNG_REMOTE: PNG_PAYLOAD},
    )
    info = diagnostics.screenshot(core, "deck").wait()
    assert info.state == JobState.SUCCEEDED
    assert fake.commands_run.count(find_cmd) == 3


def test_screenshot_deck_timeout_when_nothing_appears(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_file: Callable[[str], str],
) -> None:
    _fast_sleep(monkeypatch)
    monkeypatch.setattr(diagnostics, "_SCREENSHOT_APPEAR_TIMEOUT_S", 0.05)
    _use_fake_rsync(core, monkeypatch, tmp_path)
    make_paramiko_patch(
        monkeypatch,
        commands=[
            _status_rule("steamos-get-status.json", fixture_file),
            (r"rm -f /tmp/gamescope", b"", b"", 0),
            (XPROP, b"", b"", 0),
            # find never matches: unscripted -> exit 127 -> treated as absent
        ],
    )
    job = diagnostics.screenshot(core, "deck")
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    assert "timeout waiting for gamescope" in exc_info.value.message
    assert job.info().artifacts == []


def test_screenshot_frame_branch(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_file: Callable[[str], str],
) -> None:
    _use_fake_rsync(core, monkeypatch, tmp_path)
    capture_json = json.dumps({"success": True, "output": FRAME_REMOTE})
    fake = make_paramiko_patch(
        monkeypatch,
        commands=[
            _status_rule("steamos-get-status-deckard.json", fixture_file),
            (DECKARD_CAPTURE, capture_json.encode(), b"", 0),
        ],
        filesystem={FRAME_REMOTE: PNG_PAYLOAD},
    )
    info = diagnostics.screenshot(core, "deck").wait()
    assert info.state == JobState.SUCCEEDED
    # Frame: deckard-capture, never the gamescope xprop trigger
    assert fake.commands_run == [
        "python3 ~/devkit-utils/steamos-get-status --json",
        "python3 ~/devkit-utils/deckard-capture --json",
        f"rm {FRAME_REMOTE}",
    ]
    artifact = get_artifact(core.state, info.artifacts[0])
    assert artifact.kind == "screenshot"
    assert artifact.remote_source == FRAME_REMOTE
    assert artifact.path.read_bytes() == PNG_PAYLOAD


def test_screenshot_frame_error_exit_with_json(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_file: Callable[[str], str],
) -> None:
    _use_fake_rsync(core, monkeypatch, tmp_path)
    make_paramiko_patch(
        monkeypatch,
        commands=[
            _status_rule("steamos-get-status-deckard.json", fixture_file),
            (DECKARD_CAPTURE, b'{"error": "compositor busy"}', b"", 1),
        ],
    )
    job = diagnostics.screenshot(core, "deck")
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    assert "compositor busy" in exc_info.value.message


def test_screenshot_frame_unsuccessful_payload(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_file: Callable[[str], str],
) -> None:
    _use_fake_rsync(core, monkeypatch, tmp_path)
    make_paramiko_patch(
        monkeypatch,
        commands=[
            _status_rule("steamos-get-status-deckard.json", fixture_file),
            (DECKARD_CAPTURE, b'{"success": false, "error": "no window"}', b"", 0),
        ],
    )
    job = diagnostics.screenshot(core, "deck")
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert "no window" in exc_info.value.message


def test_screenshot_frame_unparseable_payload(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_file: Callable[[str], str],
) -> None:
    _use_fake_rsync(core, monkeypatch, tmp_path)
    make_paramiko_patch(
        monkeypatch,
        commands=[
            _status_rule("steamos-get-status-deckard.json", fixture_file),
            (DECKARD_CAPTURE, b"not json", b"", 0),
        ],
    )
    job = diagnostics.screenshot(core, "deck")
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert "failed to parse deckard-capture output" in exc_info.value.message


def test_screenshot_rejects_bad_mode(core: DevkitCore, device: Device) -> None:
    with pytest.raises(DevkitError) as exc_info:
        diagnostics.screenshot(core, "deck", mode=9)  # type: ignore[arg-type]
    assert exc_info.value.code == ErrorCode.VALIDATION


# --- controller_config ------------------------------------------------------------


def _controller_rsync_body() -> str:
    # the pull must actually drop a config_*.vdf into the staging dir (last
    # argv element) for the job to succeed
    return (
        '    for a in "$@"; do dest="$a"; done\n'
        '    mkdir -p "$dest"\n'
        "    printf 'vdf\\n' > \"$dest/config_123.vdf\"\n"
    )


def test_controller_config_command_sequence(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    argv_log = _use_fake_rsync(core, monkeypatch, tmp_path, _controller_rsync_body())
    fake = make_paramiko_patch(
        monkeypatch,
        commands=[
            (DUMP_CONFIG, b'{"success": "config dumped"}', b"", 0),
            (FIND_CONFIG_RE, b"/tmp/config_123.vdf\n", b"", 0),
        ],
    )
    info = diagnostics.controller_config(core, "deck", GAMEID).wait()
    assert info.state == JobState.SUCCEEDED
    assert info.kind == JobKind.CONTROLLER_CONFIG
    assert info.gameid == GAMEID
    assert "config dumped" in (info.message or "")
    assert fake.commands_run == [
        "rm -f /tmp/config_*.tmp",
        f"python3 ~/devkit-utils/steamos-dump-controller-config --gameid {GAMEID}",
        FIND_CONFIG_CMD,
    ]
    pulls = _pull_invocations(argv_log)
    assert len(pulls) == 1
    assert "--include=config_*.vdf" in pulls[0]
    assert "--exclude=*" in pulls[0]
    assert "deck@192.0.2.10:/tmp/" in pulls[0]
    artifact = get_artifact(core.state, info.artifacts[0])
    assert artifact.kind == "controller_config"
    assert artifact.remote_source == "/tmp/config_*.vdf"


def test_controller_config_device_error_fails_before_pull(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    argv_log = _use_fake_rsync(core, monkeypatch, tmp_path, _controller_rsync_body())
    make_paramiko_patch(
        monkeypatch,
        commands=[(DUMP_CONFIG, b'{"error": "no such game"}', b"", 0)],
    )
    job = diagnostics.controller_config(core, "deck", GAMEID)
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    assert "no such game" in exc_info.value.message
    assert _pull_invocations(argv_log) == []


def test_controller_config_non_json_dump_fails(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _use_fake_rsync(core, monkeypatch, tmp_path)
    make_paramiko_patch(monkeypatch, commands=[(DUMP_CONFIG, b"garbage", b"", 0)])
    job = diagnostics.controller_config(core, "deck", GAMEID)
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert "did not return JSON" in exc_info.value.message


def test_controller_config_timeout_waiting_for_vdf(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _fast_sleep(monkeypatch)
    monkeypatch.setattr(diagnostics, "_CONFIG_APPEAR_TIMEOUT_S", 0.05)
    _use_fake_rsync(core, monkeypatch, tmp_path)
    make_paramiko_patch(
        monkeypatch,
        commands=[(DUMP_CONFIG, b'{"success": "ok"}', b"", 0)],
    )
    job = diagnostics.controller_config(core, "deck", GAMEID)
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert "timeout waiting for /tmp/config_*.vdf" in exc_info.value.message


def test_controller_config_empty_pull_fails(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # rsync exits 0 but the device produced no config_*.vdf
    _use_fake_rsync(core, monkeypatch, tmp_path)
    make_paramiko_patch(
        monkeypatch,
        commands=[
            (DUMP_CONFIG, b'{"success": "ok"}', b"", 0),
            (FIND_CONFIG_RE, b"/tmp/config_123.vdf\n", b"", 0),
        ],
    )
    job = diagnostics.controller_config(core, "deck", GAMEID)
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert "no config_*.vdf" in exc_info.value.message


def test_controller_config_rsync_failure_fails_job(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _use_fake_rsync(core, monkeypatch, tmp_path, "exit 23\n")
    make_paramiko_patch(
        monkeypatch,
        commands=[
            (DUMP_CONFIG, b'{"success": "ok"}', b"", 0),
            (FIND_CONFIG_RE, b"/tmp/config_123.vdf\n", b"", 0),
        ],
    )
    job = diagnostics.controller_config(core, "deck", GAMEID)
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    # wait() rebuilds the error from the journal; the exit code survives in
    # the message, not remote_exit_code
    assert "rsync exited 23" in exc_info.value.message


def test_controller_config_rejects_bad_gameid(core: DevkitCore, device: Device) -> None:
    with pytest.raises(DevkitError) as exc_info:
        diagnostics.controller_config(core, "deck", "1bad")
    assert exc_info.value.code == ErrorCode.VALIDATION


# --- CLI plumbing -----------------------------------------------------------------

_JOB_ID = "job_01ARZ3NDEKTSV4RRFFQ69G5FAV"


def _job_info(kind: JobKind, artifacts: list[str] | None = None) -> JobInfo:
    return JobInfo(
        id=_JOB_ID,
        kind=kind,
        state=JobState.SUCCEEDED,
        device="deck",
        created_at=datetime.now(UTC),
        message="done",
        artifacts=artifacts or [],
    )


class _StubJob:
    def __init__(self, info: JobInfo | None = None, error: DevkitError | None = None) -> None:
        self.job_id = _JOB_ID
        self._info = info
        self._error = error

    def wait(self) -> JobInfo:
        if self._error is not None:
            raise self._error
        assert self._info is not None
        return self._info


def _register_log_artifact(core: DevkitCore, tmp_path: Path, lines: int = 10) -> str:
    staging = tmp_path / "logs-payload"
    staging.mkdir()
    (staging / "console-linux.txt").write_text(
        "".join(f"log line {i}\n" for i in range(lines)), encoding="utf-8"
    )
    artifact = register_artifact(
        core.state,
        job_id=_JOB_ID,
        device="deck",
        kind="logs",
        source=staging,
        remote_source="/home/deck/.local/share/Steam/logs",
    )
    return artifact.id


def test_cli_logs_wait_prints_bounded_excerpt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(common, "stdout_is_tty", lambda: True)

    def fake_fetch_logs(core, sel, kinds=None, timeout_s=600):
        artifact_id = _register_log_artifact(core, tmp_path)
        return _StubJob(_job_info(JobKind.FETCH_LOGS, [artifact_id]))

    monkeypatch.setattr(diagnostics, "fetch_logs", fake_fetch_logs)
    result = runner.invoke(
        cli.app, ["--state-dir", str(tmp_path / "s"), "logs", "deck", "--tail", "3"]
    )
    assert result.exit_code == 0, result.output
    assert "--- console-linux.txt (last 3 lines) ---" in result.stdout
    assert "log line 7" in result.stdout
    assert "log line 9" in result.stdout
    assert "log line 0" not in result.stdout  # bounded tail


def test_cli_logs_no_wait_prints_job_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(
        diagnostics,
        "fetch_logs",
        lambda core, sel, kinds=None, timeout_s=600: (
            calls.append((sel, kinds)) or _StubJob(_job_info(JobKind.FETCH_LOGS))
        ),
    )
    result = runner.invoke(
        cli.app, ["--state-dir", str(tmp_path), "logs", "deck", "--kind", "proton", "--no-wait"]
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"job_id": _JOB_ID, "status": "queued"}
    assert calls == [("deck", ["proton"])]


def test_cli_logs_rejects_unknown_kind(tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app, ["--state-dir", str(tmp_path), "logs", "deck", "--kind", "bogus"]
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "VALIDATION"


def test_cli_logs_failed_job_maps_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    error = DevkitError(ErrorCode.REMOTE_COMMAND_FAILED, "rsync exploded")
    monkeypatch.setattr(
        diagnostics,
        "fetch_logs",
        lambda core, sel, kinds=None, timeout_s=600: _StubJob(error=error),
    )
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path), "logs", "deck"])
    assert result.exit_code == 7
    assert json.loads(result.stdout)["code"] == "REMOTE_COMMAND_FAILED"


def test_cli_screenshot_lists_artifact_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(common, "stdout_is_tty", lambda: True)

    def fake_screenshot(core, sel, mode=3, timeout_s=60):
        png = tmp_path / "shot.png"
        png.write_bytes(PNG_PAYLOAD)
        artifact = register_artifact(
            core.state,
            job_id=_JOB_ID,
            device="deck",
            kind="screenshot",
            source=png,
            remote_source="/tmp/gamescope_x.png",
            media_type="image/png",
        )
        return _StubJob(_job_info(JobKind.SCREENSHOT, [artifact.id]))

    monkeypatch.setattr(diagnostics, "screenshot", fake_screenshot)
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path / "s"), "screenshot", "deck"])
    assert result.exit_code == 0, result.output
    assert "artifact: art_" in result.stdout
    assert "shot.png" in result.stdout


def test_cli_screenshot_rejects_bad_mode(tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app, ["--state-dir", str(tmp_path), "screenshot", "deck", "--mode", "9"]
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "VALIDATION"


def test_cli_screenshot_submit_error_maps_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unknown_device(core, sel, mode=3, timeout_s=60):
        raise DevkitError(ErrorCode.DEVICE_NOT_FOUND, "unknown device")

    monkeypatch.setattr(diagnostics, "screenshot", unknown_device)
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path), "screenshot", "ghost"])
    assert result.exit_code == 3
    assert json.loads(result.stdout)["code"] == "DEVICE_NOT_FOUND"


def test_cli_controller_config_passes_gameid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = []
    monkeypatch.setattr(
        diagnostics,
        "controller_config",
        lambda core, sel, gameid, timeout_s=60: (
            calls.append((sel, gameid)) or _StubJob(_job_info(JobKind.CONTROLLER_CONFIG))
        ),
    )
    result = runner.invoke(
        cli.app,
        ["--state-dir", str(tmp_path), "controller-config", "deck", GAMEID, "--no-wait"],
    )
    assert result.exit_code == 0
    assert calls == [("deck", GAMEID)]
    assert json.loads(result.stdout) == {"job_id": _JOB_ID, "status": "queued"}


def test_cli_logs_uses_last_device_when_sel_omitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = []
    monkeypatch.setattr(
        diagnostics,
        "fetch_logs",
        lambda core, sel, kinds=None, timeout_s=600: (
            calls.append(sel) or _StubJob(_job_info(JobKind.FETCH_LOGS))
        ),
    )
    state = tmp_path / "s"
    state.mkdir()
    (state / "last_device").write_text("deck\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["--state-dir", str(state), "logs", "--no-wait"])
    assert result.exit_code == 0
    assert calls == ["deck"]


def test_cli_logs_without_sel_or_last_device_fails(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path), "logs", "--no-wait"])
    assert result.exit_code == 2
    assert "no device given" in json.loads(result.stdout)["message"]


def test_cli_controller_config_single_positional_is_gameid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = []
    monkeypatch.setattr(
        diagnostics,
        "controller_config",
        lambda core, sel, gameid, timeout_s=60: (
            calls.append((sel, gameid)) or _StubJob(_job_info(JobKind.CONTROLLER_CONFIG))
        ),
    )
    state = tmp_path / "s"
    state.mkdir()
    (state / "last_device").write_text("deck\n", encoding="utf-8")
    result = runner.invoke(
        cli.app, ["--state-dir", str(state), "controller-config", GAMEID, "--no-wait"]
    )
    assert result.exit_code == 0
    assert calls == [("deck", GAMEID)]


def test_screenshot_non_object_status_fails(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _use_fake_rsync(core, monkeypatch, tmp_path)
    make_paramiko_patch(monkeypatch, commands=[(GET_STATUS, b"[]", b"", 0)])
    job = diagnostics.screenshot(core, "deck")
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
    assert "non-object JSON payload" in exc_info.value.message


def test_screenshot_frame_missing_output_path(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_file: Callable[[str], str],
) -> None:
    _use_fake_rsync(core, monkeypatch, tmp_path)
    make_paramiko_patch(
        monkeypatch,
        commands=[
            _status_rule("steamos-get-status-deckard.json", fixture_file),
            (DECKARD_CAPTURE, b'{"success": true}', b"", 0),
        ],
    )
    job = diagnostics.screenshot(core, "deck")
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert "no output path" in exc_info.value.message


def test_controller_config_non_dict_dump_fails(
    core: DevkitCore,
    paired_key: Path,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _use_fake_rsync(core, monkeypatch, tmp_path)
    make_paramiko_patch(monkeypatch, commands=[(DUMP_CONFIG, b'["ok"]', b"", 0)])
    job = diagnostics.controller_config(core, "deck", GAMEID)
    with pytest.raises(DevkitError) as exc_info:
        job.wait()
    assert "unexpected steamos-dump-controller-config response" in exc_info.value.message


def test_cli_logs_finds_frame_primary_log_nested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(common, "stdout_is_tty", lambda: True)

    def fake_fetch_logs(core, sel, kinds=None, timeout_s=600):
        staging = tmp_path / "frame-payload"
        nested = staging / "SteamVR" / "logs"
        nested.mkdir(parents=True)
        (nested / "steam_output.log").write_text("frame log body\n", encoding="utf-8")
        artifact = register_artifact(
            core.state,
            job_id=_JOB_ID,
            device="deck",
            kind="logs",
            source=staging,
            remote_source="/home/steamos/.local/share/Steam/logs",
        )
        return _StubJob(_job_info(JobKind.FETCH_LOGS, [artifact.id]))

    monkeypatch.setattr(diagnostics, "fetch_logs", fake_fetch_logs)
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path / "s"), "logs", "deck"])
    assert result.exit_code == 0, result.output
    assert "--- steam_output.log (last 200 lines) ---" in result.stdout
    assert "frame log body" in result.stdout


def test_cli_logs_without_primary_log_notes_absence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(common, "stdout_is_tty", lambda: True)

    def fake_fetch_logs(core, sel, kinds=None, timeout_s=600):
        staging = tmp_path / "empty-payload"
        staging.mkdir()
        (staging / "README.txt").write_text("unrelated\n", encoding="utf-8")
        artifact = register_artifact(
            core.state,
            job_id=_JOB_ID,
            device="deck",
            kind="logs",
            source=staging,
            remote_source="/home/deck/.local/share/Steam/logs",
        )
        return _StubJob(_job_info(JobKind.FETCH_LOGS, [artifact.id]))

    monkeypatch.setattr(diagnostics, "fetch_logs", fake_fetch_logs)
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path / "s"), "logs", "deck"])
    assert result.exit_code == 0, result.output
    assert "no console-linux.txt / steam_output.log" in result.stdout


def test_cli_controller_config_missing_gameid_fails(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path), "controller-config"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "VALIDATION"
