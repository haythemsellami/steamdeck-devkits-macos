"""Unit tests for the devkit CLI (spec section 10): doctor, version, exit codes."""

import json
from pathlib import Path

import pytest
import typer
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from steamos_devkit import cli, constants
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import DoctorCheck, DoctorReport

runner = CliRunner()

RSYNC_GOOD = "rsync  version 3.5.1  protocol version 35\\n"
OPENRSYNC = "openrsync: protocol version 29\\n"


def _fake_rsync(directory: Path, output: str) -> Path:
    directory.mkdir(exist_ok=True)
    path = directory / "rsync"
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_version_json() -> None:
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)  # auto-JSON: CliRunner stdout is not a TTY
    assert payload["version"]
    assert payload["upstream_commit"] == "a00ceb7d91ea44a0c3e714a91a06417d6e5cdb33"


def test_version_human_on_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_stdout_is_tty", lambda: True)
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.startswith("devkit ")
    assert "upstream steamos-devkit a00ceb7" in result.stdout


def test_doctor_human_on_tty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_stdout_is_tty", lambda: True)
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync(tmp_path, RSYNC_GOOD)))
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path / "s"), "doctor"])
    assert result.exit_code == 0
    assert "[     ok] rsync:" in result.stdout
    assert "result: ok" in result.stdout
    assert not result.stdout.startswith("{")  # human mode is not JSON


def test_die_json_mode(capsys: pytest.CaptureFixture[str]) -> None:
    err = DevkitError(ErrorCode.DEVICE_NOT_FOUND, "no such device")
    with pytest.raises(typer.Exit) as exc_info:
        cli._die(err, json_mode=True)
    assert exc_info.value.exit_code == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "ok": False,
        "code": "DEVICE_NOT_FOUND",
        "device": None,
        "message": "no such device",
        "upload_complete": None,
        "registration_complete": None,
        "launch": None,
    }


def test_die_human_mode(capsys: pytest.CaptureFixture[str]) -> None:
    err = DevkitError(ErrorCode.UNSUPPORTED, "stop-title does not exist upstream")
    with pytest.raises(typer.Exit) as exc_info:
        cli._die(err, json_mode=False)
    assert exc_info.value.exit_code == 10
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[UNSUPPORTED]" in captured.err


def test_main_catches_devkit_error(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    mocker.patch.object(
        cli, "app", side_effect=DevkitError(ErrorCode.OPENRSYNC_DETECTED, "openrsync found")
    )
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 5
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "OPENRSYNC_DETECTED"


def test_main_runs_app(mocker: MockerFixture) -> None:
    app_mock = mocker.patch.object(cli, "app")
    cli.main()
    app_mock.assert_called_once_with()


def test_doctor_json_green(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync(tmp_path, RSYNC_GOOD)))
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path / "s"), "doctor"])
    assert result.exit_code == 0
    report = DoctorReport.model_validate(json.loads(result.stdout))
    assert report.ok is True
    assert report.state_dir == tmp_path / "s"


def test_doctor_openrsync_exits_5(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(constants.RSYNC_ENV_VAR, str(_fake_rsync(tmp_path, OPENRSYNC)))
    result = runner.invoke(cli.app, ["--state-dir", str(tmp_path / "s"), "--json", "doctor"])
    assert result.exit_code == 5  # dependency problem (spec section 10)
    report = DoctorReport.model_validate(json.loads(result.stdout))
    assert report.ok is False
    assert report.tools.rsync.flavor == "openrsync"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (None, 0),
        (ErrorCode.VALIDATION, 2),
        (ErrorCode.DEVICE_NOT_FOUND, 3),
        (ErrorCode.DEVICE_UNREACHABLE, 3),
        (ErrorCode.AMBIGUOUS_DEVICE, 3),
        (ErrorCode.PAIRING_DECLINED, 4),
        (ErrorCode.PAIRING_FAILED, 4),
        (ErrorCode.DEPENDENCY_MISSING, 5),
        (ErrorCode.OPENRSYNC_DETECTED, 5),
        (ErrorCode.PERMISSION_LOCAL_NETWORK, 6),
        (ErrorCode.PERMISSION_KEY_FILE, 6),
        (ErrorCode.REMOTE_COMMAND_FAILED, 7),
        (ErrorCode.TIMEOUT, 8),
        (ErrorCode.PAIRING_TIMEOUT, 8),
        (ErrorCode.CANCELLED, 9),
        (ErrorCode.UNSUPPORTED, 10),
        (ErrorCode.JOB_NOT_FOUND, 2),
        (ErrorCode.ARTIFACT_NOT_FOUND, 2),
        (ErrorCode.STATE_CORRUPT, 1),
    ],
)
def test_exit_code_table(code: ErrorCode | None, expected: int) -> None:
    assert cli.exit_code_for(code) == expected
    if code is not None:
        assert cli.exit_code_for_error(DevkitError(code, "m")) == expected


def test_render_doctor_human_output(tmp_path: Path) -> None:
    report = DoctorReport(
        ok=True,
        context="cli",
        platform="darwin",
        arch="arm64",
        python_version="3.12.12",
        state_dir=tmp_path,
        tools={
            "rsync": {"ok": True, "flavor": "rsync", "version": "3.5.1", "protocol_version": 35}
        },
        checks=[DoctorCheck(name="rsync", status="ok", summary="rsync 3.5.1")],
    )
    text = cli._render_doctor(report)
    assert "[     ok] rsync: rsync 3.5.1" in text
    assert "result: ok" in text


def test_doctor_exit_code_mapping(tmp_path: Path) -> None:
    def report_with(status: str, name: str) -> DoctorReport:
        return DoctorReport(
            ok=status != "error",
            context="cli",
            platform="darwin",
            arch="arm64",
            python_version="3.12.12",
            state_dir=tmp_path,
            tools={"rsync": {"ok": status == "ok"}},
            checks=[DoctorCheck(name=name, status=status, summary="s")],  # type: ignore[arg-type]
        )

    assert cli._doctor_exit_code(report_with("ok", "rsync")) == 0
    assert cli._doctor_exit_code(report_with("error", "rsync")) == 5
    assert cli._doctor_exit_code(report_with("error", "ssh")) == 5
    assert cli._doctor_exit_code(report_with("error", "state-dir")) == 6
    assert cli._doctor_exit_code(report_with("error", "unlisted-check")) == 1
