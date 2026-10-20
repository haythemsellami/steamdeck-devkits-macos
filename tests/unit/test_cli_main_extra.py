"""Unit tests for cli.main()'s DevkitError rendering (review finding L11).

The pre-callback fallback must apply the same JSON-mode rule as the app
callback (flag parsing tolerant of the bool-flag forms typer accepts, plus
``common.stdout_is_tty()``), and once the callback ran its parsed ``Options``
are authoritative — no re-parsing of argv.
"""

import json
import sys
from collections.abc import Callable

import pytest
from pytest_mock import MockerFixture

from steamos_devkit import cli
from steamos_devkit.cli import common
from steamos_devkit.cli.common import Options
from steamos_devkit.errors import DevkitError, ErrorCode

_ERR = DevkitError(ErrorCode.DEVICE_NOT_FOUND, "no such device")


def _raising_app() -> Callable[[], None]:
    def _app() -> None:
        raise _ERR

    return _app


def _callback_then_raise(json_mode: bool) -> Callable[[], None]:
    """An app() that ran the callback (stashing Options) before failing."""

    def _app() -> None:
        cli._resolved_options = Options(json=json_mode, state_dir=None, verbose=False)
        raise _ERR

    return _app


@pytest.mark.parametrize(
    "argv",
    [
        ["devkit", "--json", "status"],
        ["devkit", "--json=true", "status"],
        ["devkit", "--json", "true", "status"],
        ["devkit", "status", "--json"],  # position does not matter to the scan
    ],
)
def test_main_json_flag_forms_on_tty(
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(common, "stdout_is_tty", lambda: True)
    mocker.patch.object(cli, "app", side_effect=_raising_app())
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "DEVICE_NOT_FOUND"


@pytest.mark.parametrize(
    "argv",
    [
        ["devkit", "--json=false", "status"],
        ["devkit", "--json", "false", "status"],
    ],
)
def test_main_json_flag_false_forms_on_tty(
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(common, "stdout_is_tty", lambda: True)
    mocker.patch.object(cli, "app", side_effect=_raising_app())
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[DEVICE_NOT_FOUND]" in captured.err


def test_main_no_flag_non_tty_is_json(
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["devkit", "status"])
    monkeypatch.setattr(common, "stdout_is_tty", lambda: False)
    mocker.patch.object(cli, "app", side_effect=_raising_app())
    with pytest.raises(SystemExit):
        cli.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "DEVICE_NOT_FOUND"


def test_main_no_flag_on_tty_is_human(
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["devkit", "status"])
    monkeypatch.setattr(common, "stdout_is_tty", lambda: True)
    mocker.patch.object(cli, "app", side_effect=_raising_app())
    with pytest.raises(SystemExit):
        cli.main()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[DEVICE_NOT_FOUND]" in captured.err


def test_main_prefers_callback_options_over_argv(
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Callback resolved JSON mode; argv has no --json and stdout is a TTY.
    monkeypatch.setattr(sys, "argv", ["devkit", "status"])
    monkeypatch.setattr(common, "stdout_is_tty", lambda: True)
    mocker.patch.object(cli, "app", side_effect=_callback_then_raise(json_mode=True))
    with pytest.raises(SystemExit):
        cli.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "DEVICE_NOT_FOUND"


def test_main_callback_human_mode_wins_over_non_tty(
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Callback resolved human mode; main() must not re-derive via stdout_is_tty.
    monkeypatch.setattr(sys, "argv", ["devkit", "--json=false", "status"])
    monkeypatch.setattr(common, "stdout_is_tty", lambda: False)
    mocker.patch.object(cli, "app", side_effect=_callback_then_raise(json_mode=False))
    with pytest.raises(SystemExit):
        cli.main()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[DEVICE_NOT_FOUND]" in captured.err


def test_main_drops_stale_callback_state(
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A leftover stash from a previous in-process invocation must not leak:
    # this app() fails before any callback ran, so the fallback rule applies.
    monkeypatch.setattr(cli, "_resolved_options", Options(json=False, state_dir=None, verbose=False))
    monkeypatch.setattr(sys, "argv", ["devkit", "status"])
    monkeypatch.setattr(common, "stdout_is_tty", lambda: False)
    mocker.patch.object(cli, "app", side_effect=_raising_app())
    with pytest.raises(SystemExit):
        cli.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "DEVICE_NOT_FOUND"
