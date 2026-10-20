"""Unit tests for the shared helpers promoted into ``cli.common`` (L1).

These pin the single implementations that replaced per-module duplicates:
``shift_optional_selector`` (was ``_gameid_or_shift``/``_shift`` in titles,
diagnostics, profiling, session), ``run_job`` (was ``_start_job`` in profiling
and diagnostics, plus deploy's inline twin), ``artifact_paths_human`` (was
diagnostics' ``_artifact_paths_human``), ``tail_text_bounded`` (was
diagnostics' ``_tail_text``), and ``emit_result`` (was ``_emit_result`` in
profiling/session).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import click
import pytest
import typer

from steamos_devkit.artifacts import EXCERPT_MAX_BYTES, EXCERPT_MAX_LINES, register_artifact
from steamos_devkit.cli import common
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import JobInfo, JobKind, JobState, Result

_JOB_ID = "job_01ARZ3NDEKTSV4RRFFQ69G5FAV"


def _ctx(state_dir: Path, *, json_mode: bool = True) -> typer.Context:
    ctx = typer.Context(click.Command("devkit-test"))
    ctx.obj = common.Options(json=json_mode, state_dir=state_dir, verbose=False)
    return ctx


def _job_info(*, artifacts: list[str] | None = None) -> JobInfo:
    return JobInfo(
        id=_JOB_ID,
        kind=JobKind.SCREENSHOT,
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
        self.wait_calls = 0

    def wait(self) -> JobInfo:
        self.wait_calls += 1
        if self._error is not None:
            raise self._error
        assert self._info is not None
        return self._info


class TestShiftOptionalSelector:
    def test_two_positionals_keep_device_and_value(self) -> None:
        assert common.shift_optional_selector("deck", "on", missing="m") == ("deck", "on")

    def test_one_positional_is_the_value(self) -> None:
        # device left to resolve_selector's last-device fallback
        assert common.shift_optional_selector("on", None, missing="m") == (None, "on")

    def test_no_positionals_is_validation_with_the_caller_message(self) -> None:
        with pytest.raises(DevkitError) as exc_info:
            common.shift_optional_selector(None, None, missing="missing gameid")
        assert exc_info.value.code == ErrorCode.VALIDATION
        assert exc_info.value.message == "missing gameid"


class TestRunJob:
    def test_no_wait_emits_queued_payload_without_waiting(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        job = _StubJob(error=DevkitError(ErrorCode.INTERNAL, "wait must not run"))
        assert common.run_job(_ctx(tmp_path), job, wait=False) is None
        assert job.wait_calls == 0
        assert json.loads(capsys.readouterr().out) == {"job_id": _JOB_ID, "status": "queued"}

    def test_wait_returns_terminal_info_and_emits_it(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        info = _job_info()
        assert common.run_job(_ctx(tmp_path), _StubJob(info), wait=True) is info
        assert json.loads(capsys.readouterr().out)["state"] == "succeeded"

    def test_wait_human_line(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        common.run_job(_ctx(tmp_path, json_mode=False), _StubJob(_job_info()), wait=True)
        assert f"job {_JOB_ID} succeeded: done" in capsys.readouterr().out

    def test_wait_error_dies_with_mapped_exit_code(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        error = DevkitError(ErrorCode.REMOTE_COMMAND_FAILED, "rsync exploded")
        with pytest.raises(typer.Exit) as exc_info:
            common.run_job(_ctx(tmp_path), _StubJob(error=error), wait=True)
        assert exc_info.value.exit_code == 7
        assert json.loads(capsys.readouterr().out)["code"] == "REMOTE_COMMAND_FAILED"


def test_artifact_paths_human(tmp_path: Path) -> None:
    core = DevkitCore(state_dir=tmp_path / "s")
    payload = tmp_path / "shot.png"
    payload.write_bytes(b"\x89PNG fake")
    artifact = register_artifact(
        core.state,
        job_id=_JOB_ID,
        device="deck",
        kind="screenshot",
        source=payload,
        remote_source="/tmp/gamescope.png",
    )
    lines = common.artifact_paths_human(core, _job_info(artifacts=[artifact.id]))
    assert lines == [f"artifact: {artifact.id} — {artifact.path}"]


class TestTailTextBounded:
    def test_small_file_returned_in_full(self, tmp_path: Path) -> None:
        path = tmp_path / "log.txt"
        path.write_text("a\nb\nc\n", encoding="utf-8")
        assert common.tail_text_bounded(path, 200) == "a\nb\nc"

    def test_keeps_only_the_last_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "log.txt"
        path.write_text("".join(f"line {i}\n" for i in range(10)), encoding="utf-8")
        assert common.tail_text_bounded(path, 3) == "line 7\nline 8\nline 9"

    def test_tail_lines_clamped_to_excerpt_max(self, tmp_path: Path) -> None:
        path = tmp_path / "log.txt"
        total = EXCERPT_MAX_LINES + 100
        path.write_text("".join(f"line {i}\n" for i in range(total)), encoding="utf-8")
        out = common.tail_text_bounded(path, EXCERPT_MAX_LINES + 1000)
        lines = out.splitlines()
        assert len(lines) == EXCERPT_MAX_LINES
        assert lines[0] == f"line {total - EXCERPT_MAX_LINES}"
        assert f"line {total - EXCERPT_MAX_LINES - 1}" not in lines

    def test_reads_only_the_tail_bytes_of_large_files(self, tmp_path: Path) -> None:
        path = tmp_path / "big.txt"
        path.write_bytes(b"HEADMARKER\n" + b"x" * EXCERPT_MAX_BYTES)
        out = common.tail_text_bounded(path, 10)
        assert "HEADMARKER" not in out
        assert out.strip("x") == ""

    def test_invalid_utf8_is_replaced_not_raised(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.txt"
        path.write_bytes(b"\xff\xfe\n")
        assert common.tail_text_bounded(path, 10) == "\ufffd\ufffd"


class TestEmitResult:
    def test_json_mode_emits_model_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        common.emit_result(_ctx(tmp_path), Result.success(message="ok done"))
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["message"] == "ok done"

    def test_human_mode_emits_the_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        common.emit_result(_ctx(tmp_path, json_mode=False), Result.success(message="ok done"))
        assert capsys.readouterr().out == "ok done\n"
