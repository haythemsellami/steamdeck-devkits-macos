"""Unit tests for transfer.py — rsync argv matrix, preview parsing, upload wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from steamos_devkit.core import DevkitCore
from steamos_devkit.device import DeviceHandle
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import DeploySpec, Device, FilterSpec
from steamos_devkit.transfer import (
    MAX_SAMPLE_CHANGES,
    build_rsync_argv,
    parse_preview_output,
    preview_deploy,
    upload_tree,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"

RSYNC = Path("/opt/homebrew/bin/rsync")
SSH = Path("/usr/bin/ssh")
KEY = Path("/state/keys/devkit_rsa")


def _base_argv(**overrides: object) -> list[str]:
    kwargs: dict[str, object] = {
        "rsync_path": RSYNC,
        "ssh_path": SSH,
        "key_path": KEY,
        "sources": [Path("/tmp/build")],
        "dest": "deck@192.0.2.10:~/devkit-game/mygame/",
    }
    kwargs.update(overrides)
    return build_rsync_argv(**kwargs)  # type: ignore[arg-type]


class TestBuildRsyncArgv:
    def test_defaults_match_upstream(self) -> None:
        argv = _base_argv()
        assert argv[0] == str(RSYNC)
        assert argv[1] == "-av"
        assert "-z" in argv  # compress on by default (upstream default since v0.20260727.0)
        assert "--chmod=Du=rwx,Dgo=rx,Fu=rwx,Fog=rx" in argv
        rsh = argv[argv.index("-e") + 1]
        assert rsh == (
            f"{SSH} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o ConnectTimeout=10 -i {KEY}"
        )
        # folder-to-folder forcing, upstream style
        assert argv[-2] == "/tmp/build/"
        assert argv[-1] == "deck@192.0.2.10:~/devkit-game/mygame/"
        for absent in ("--delete", "--delete-excluded", "--delete-delay", "--update", "--checksum"):
            assert absent not in argv

    def test_no_compress_drops_z(self) -> None:
        assert "-z" not in _base_argv(compress=False)

    def test_clean_adds_delete_triple(self) -> None:
        argv = _base_argv(clean=True)
        assert "--delete" in argv
        assert "--delete-excluded" in argv
        assert "--delete-delay" in argv

    def test_update_and_checksum_xor(self) -> None:
        assert "--update" in _base_argv(update=True)
        assert "--checksum" not in _base_argv(update=True)
        assert "--checksum" in _base_argv(checksum=True)
        assert "--update" not in _base_argv(checksum=True)

    def test_checksum_and_update_raise_validation(self) -> None:
        with pytest.raises(DevkitError) as exc_info:
            _base_argv(checksum=True, update=True)
        assert exc_info.value.code == ErrorCode.VALIDATION

    def test_empty_sources_raise_validation(self) -> None:
        with pytest.raises(DevkitError) as exc_info:
            _base_argv(sources=[])
        assert exc_info.value.code == ErrorCode.VALIDATION

    def test_exclude_filter_mode(self) -> None:
        argv = _base_argv(filters=FilterSpec(mode="exclude", patterns=["*.tmp", "build/"]))
        assert "--exclude=*.tmp" in argv
        assert "--exclude=build/" in argv

    def test_include_filter_mode(self) -> None:
        argv = _base_argv(filters=FilterSpec(mode="include", patterns=["*.x86_64", "*.so"]))
        include_at = argv.index("--include=*.x86_64")
        assert argv[include_at + 1] == "--include=*.so"
        assert argv[include_at + 2] == "--include=*/"
        assert argv[include_at + 3] == "--exclude=*"
        assert argv[include_at + 4] == "--prune-empty-dirs"

    def test_raw_filter_mode_is_verbatim(self) -> None:
        raw = ["--exclude=.git/", "--compress-level=1"]
        argv = _base_argv(filters=FilterSpec(mode="raw", patterns=raw))
        assert raw[0] in argv and raw[1] in argv

    def test_no_filters_no_filter_args(self) -> None:
        argv = _base_argv(filters=FilterSpec())
        assert not any(a.startswith("--exclude") or a.startswith("--include") for a in argv)

    def test_extra_args_come_before_sources(self) -> None:
        argv = _base_argv(extra_args=["-n", "--itemize-changes", "--stats"])
        assert argv[-4:-2] == ["--itemize-changes", "--stats"]
        assert argv[-1].startswith("deck@")

    def test_paths_with_spaces_are_quoted_in_rsh(self) -> None:
        argv = _base_argv(ssh_path=Path("/opt/my tools/ssh"), key_path=Path("/tmp/my key"))
        rsh = argv[argv.index("-e") + 1]
        assert "'/opt/my tools/ssh'" in rsh
        assert "'/tmp/my key'" in rsh

    def test_source_trailing_slash_is_not_doubled(self) -> None:
        argv = _base_argv(sources=[Path("/tmp/build/")])
        assert argv[-2] == "/tmp/build/"


class TestParsePreviewOutput:
    def test_golden_fixture_counts(self, fixture_file) -> None:
        text = fixture_file("rsync-itemize-dry-run.txt")
        preview = parse_preview_output(text)
        # files only: dirs (cd+++++++++ scripts/, *deleting obsolete/) excluded
        assert preview.files_added == 2  # data/pak1.dat, scripts/run.sh
        assert preview.files_changed == 2  # README.txt, config/default.cfg
        assert preview.files_deleted == 2  # junk.dat, savegame.sav
        assert len(preview.sample_changes) == 8  # dirs included in the sample
        assert any("scripts/" in line for line in preview.sample_changes)
        # fixture carries no --stats block
        assert preview.bytes_to_transfer == 0
        assert preview.warnings

    def test_stats_block_sets_bytes(self, fixture_file) -> None:
        text = fixture_file("rsync-itemize-dry-run.txt") + (
            "\nNumber of files: 15\nTotal transferred file size: 44,654,742 bytes\n"
        )
        preview = parse_preview_output(text)
        assert preview.bytes_to_transfer == 44_654_742
        assert preview.warnings == []

    def test_sample_is_capped_with_warning(self) -> None:
        lines = [f">f+++++++++ file{i:04d}.bin" for i in range(MAX_SAMPLE_CHANGES + 50)]
        preview = parse_preview_output("\n".join(lines))
        assert len(preview.sample_changes) == MAX_SAMPLE_CHANGES
        assert preview.files_added == MAX_SAMPLE_CHANGES + 50
        assert any("truncated" in w for w in preview.warnings)

    def test_empty_output(self) -> None:
        preview = parse_preview_output("sending incremental file list\n\n")
        assert preview.files_added == 0
        assert preview.sample_changes == []


class _FakeCtx:
    """JobContext stand-in for upload_tree: replays records through on_output."""

    def __init__(self, records: list[str], exit_code: int = 0) -> None:
        self.records = records
        self.exit_code = exit_code
        self.progress_events: list[dict[str, object]] = []
        self.argv_seen: list[str] = []

    def progress(self, phase: str, **kwargs: object) -> None:
        self.progress_events.append({"phase": phase, **kwargs})

    def run_process(self, argv, on_output=None, **_kwargs) -> int:
        self.argv_seen = [str(a) for a in argv]
        for record in self.records:
            if on_output is not None:
                on_output(record)
        return self.exit_code


def _fake_rsync(directory: Path, body: str = "exit 0") -> Path:
    """A fake rsync good enough for the adapter's --version probe."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "rsync"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf 'rsync  version 3.5.1  protocol version 33\\n'\n"
        "  exit 0\n"
        "fi\n"
        f"{body}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@pytest.fixture
def core(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> DevkitCore:
    monkeypatch.setenv("DEVKIT_RSYNC", str(_fake_rsync(state_dir / "bin")))
    core = DevkitCore(state_dir=state_dir)
    core.state.devices.upsert(
        Device(name="deck", address="192.0.2.10", login="deck", added_via="explicit")
    )
    return core


@pytest.fixture
def handle(core: DevkitCore) -> DeviceHandle:
    device = core.state.devices.get("deck")
    assert device is not None
    return DeviceHandle(core, device)


def _spec(tmp_path: Path, **overrides: object) -> DeploySpec:
    source = tmp_path / "build"
    source.mkdir(exist_ok=True)
    kwargs: dict[str, object] = {"device": "deck", "gameid": "mygame", "source_dir": source}
    kwargs.update(overrides)
    return DeploySpec(**kwargs)  # type: ignore[arg-type]


class TestUploadTree:
    def test_progress_records_wire_through(
        self, handle: DeviceHandle, tmp_path: Path, fixture_file
    ) -> None:
        from steamos_devkit.jobs.progress import split_records

        records = split_records(fixture_file("rsync-progress2-success.txt"))
        ctx = _FakeCtx(records)
        upload_tree(ctx, handle, _spec(tmp_path), "/home/deck/devkit-game/mygame")  # type: ignore[arg-type]
        assert ctx.argv_seen[1] == "-av"
        assert "--info=progress2" in ctx.argv_seen
        assert "--partial" in ctx.argv_seen
        assert ctx.argv_seen[-1] == "deck@192.0.2.10:/home/deck/devkit-game/mygame/"
        progress = [e for e in ctx.progress_events if e["phase"] == "transferring"]
        assert progress, "no progress events parsed from golden capture"
        last = progress[-1]
        assert last["bytes_done"] == 44_654_666
        assert last["bytes_total"] == 44_654_666
        assert last["items_done"] == 7
        for event in progress:
            assert event["bytes_total"] is None or event["bytes_total"] >= event["bytes_done"]

    def test_progress_never_exceeds_100_percent(self, handle: DeviceHandle, tmp_path: Path) -> None:
        # a >100% estimate (checksum re-sends) must clamp bytes_total >= bytes_done
        ctx = _FakeCtx(["     1,000 150%    1.00kB/s    0:00:00 (xfr#1, to-chk=0/2)"])
        upload_tree(ctx, handle, _spec(tmp_path), "/home/deck/devkit-game/mygame")  # type: ignore[arg-type]
        (event,) = ctx.progress_events
        assert event["bytes_total"] == 1000
        assert event["bytes_done"] == 1000

    def test_nonzero_exit_raises_with_tail(self, handle: DeviceHandle, tmp_path: Path) -> None:
        ctx = _FakeCtx(["some/file.txt", "rsync error: some files vanished"], exit_code=23)
        with pytest.raises(DevkitError) as exc_info:
            upload_tree(ctx, handle, _spec(tmp_path), "/home/deck/devkit-game/mygame")  # type: ignore[arg-type]
        assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
        assert exc_info.value.remote_exit_code == 23
        assert "vanished" in exc_info.value.details["output_tail"]

    def test_clean_and_filters_flow_into_argv(self, handle: DeviceHandle, tmp_path: Path) -> None:
        ctx = _FakeCtx([])
        spec = _spec(tmp_path, clean=True, filters=FilterSpec(mode="exclude", patterns=["*.log"]))
        upload_tree(ctx, handle, spec, "/home/deck/devkit-game/mygame")  # type: ignore[arg-type]
        assert "--delete" in ctx.argv_seen
        assert "--exclude=*.log" in ctx.argv_seen


class TestPreviewDeploy:
    def test_preview_runs_dry_run_without_device_mutation(
        self,
        core: DevkitCore,
        tmp_path: Path,
        fixture_file,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        core.state.ensure_layout()
        core.state.key_path.touch()  # preview only checks the key exists
        argv_log = tmp_path / "argv.log"
        fake = _fake_rsync(
            tmp_path / "bin",
            body=(
                "printf '%s\\n' \"$@\" > " + str(argv_log) + "\n"
                "cat " + str(FIXTURES / "rsync-itemize-dry-run.txt") + "\n"
                "printf 'Total transferred file size: 44,654,742 bytes\\n'\n"
                "exit 0"
            ),
        )
        monkeypatch.setenv("DEVKIT_RSYNC", str(fake))
        preview = preview_deploy(core, "deck", _spec(tmp_path))
        assert preview.files_added == 2
        assert preview.bytes_to_transfer == 44_654_742
        argv = argv_log.read_text(encoding="utf-8").splitlines()
        assert "-n" in argv
        assert "--itemize-changes" in argv
        assert "--stats" in argv
        assert argv[-1] == "deck@192.0.2.10:~/devkit-game/mygame/"

    def test_preview_requires_key(self, core: DevkitCore, tmp_path: Path) -> None:
        core.state.ensure_layout()
        with pytest.raises(DevkitError) as exc_info:
            preview_deploy(core, "deck", _spec(tmp_path))
        assert exc_info.value.code == ErrorCode.PAIRING_FAILED

    def test_preview_rejects_reserved_gameid(self, core: DevkitCore, tmp_path: Path) -> None:
        with pytest.raises(DevkitError) as exc_info:
            preview_deploy(core, "deck", _spec(tmp_path, gameid="steam"))
        assert exc_info.value.code == ErrorCode.UNSUPPORTED

    def test_preview_rejects_missing_source(self, core: DevkitCore, tmp_path: Path) -> None:
        with pytest.raises(DevkitError) as exc_info:
            preview_deploy(core, "deck", _spec(tmp_path, source_dir=tmp_path / "missing"))
        assert exc_info.value.code == ErrorCode.VALIDATION

    def test_preview_rsync_failure_maps_remote_failed(
        self, core: DevkitCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        core.state.ensure_layout()
        core.state.key_path.touch()
        fake = _fake_rsync(tmp_path / "bin", body="echo 'rsync: connection failed' >&2\nexit 12")
        monkeypatch.setenv("DEVKIT_RSYNC", str(fake))
        with pytest.raises(DevkitError) as exc_info:
            preview_deploy(core, "deck", _spec(tmp_path))
        assert exc_info.value.code == ErrorCode.REMOTE_COMMAND_FAILED
        assert exc_info.value.remote_exit_code == 12
        assert "connection failed" in exc_info.value.details["stderr_tail"]


def test_deploy_spec_validation_via_models(tmp_path: Path) -> None:
    # pydantic owns the checksum+update invariant at construction time
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DeploySpec(
            device="deck",
            gameid="mygame",
            source_dir=tmp_path,
            checksum=True,
            update=True,
        )


def test_launch_result_defaults() -> None:
    from steamos_devkit.models import LaunchResult

    result = LaunchResult(gameid="g1", launched=False)
    assert result.device_response == ""
    assert result.readiness == "unknown"
    assert json.loads(result.model_dump_json())["readiness"] == "unknown"
