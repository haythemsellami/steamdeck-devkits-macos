"""Self-tests for the fake paramiko harness.

These exercise the fake's own contract (matching, ordering, SFTP round-trip,
state sharing across spawns) so later waves can trust the harness before
building integration tests on it. Stdlib + pytest only at module level; the
paramiko patch test skips if paramiko is not installed yet.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fake_ssh import FakeSSHClient, ScriptedResponse, make_paramiko_patch

STATUS_JSON = b'{"hostname": "steamdeck"}\n'


@pytest.fixture
def fake() -> FakeSSHClient:
    client = FakeSSHClient(
        commands=[
            (r"steamos-get-status --json", STATUS_JSON, b"", 0),
            ScriptedResponse(
                r"ls -1t /tmp/\*\.rgp", b"", b"ls: No such file or directory\n", 1, max_uses=2
            ),
            ScriptedResponse(r"ls -1t /tmp/\*\.rgp", b"/tmp/foo.rgp\n", b"", 0),
        ],
        filesystem={
            "/tmp/foo.rgp": b"fake-rgp-bytes",
            "/home/deck/devkit-game/hello_triangle/bin/game.x86_64": b"\x7fELF-fake",
        },
    )
    client.connect("192.0.2.10", username="deck", pkey=None, timeout=5, look_for_keys=False)
    return client


def test_connect_records_kwargs(fake: FakeSSHClient) -> None:
    assert fake.connect_records == [
        {
            "hostname": "192.0.2.10",
            "port": 22,
            "username": "deck",
            "pkey": None,
            "timeout": 5,
            "look_for_keys": False,
            "allow_agent": True,
            "extra": {},
        }
    ]


def test_exec_command_serves_scripted_response(fake: FakeSSHClient) -> None:
    stdin, stdout, stderr = fake.exec_command("python3 ~/devkit-utils/steamos-get-status --json")
    stdin.write(b"")
    stdin.close()
    assert stdout.read() == STATUS_JSON
    assert stderr.read() == b""
    assert stdout.channel.recv_exit_status() == 0
    assert stdin.channel.shutdown_write_called is False
    stdin.channel.shutdown_write()
    assert stdin.channel.shutdown_write_called is True


def test_first_matching_rule_wins_and_exhaustion_falls_through(fake: FakeSSHClient) -> None:
    for _ in range(2):
        _, stdout, _ = fake.exec_command("ls -1t /tmp/*.rgp")
        assert stdout.channel.recv_exit_status() == 1
        assert stdout.read() == b""
    _, stdout, _ = fake.exec_command("ls -1t /tmp/*.rgp")
    assert stdout.channel.recv_exit_status() == 0
    assert stdout.read() == b"/tmp/foo.rgp\n"


def test_unmatched_command_gets_command_not_found(fake: FakeSSHClient) -> None:
    _, stdout, stderr = fake.exec_command("gpu-trace --capture")
    assert stdout.channel.recv_exit_status() == 127
    assert b"gpu-trace: command not found" in stderr.read()
    assert stdout.read() == b""


def test_unmatched_defaults_are_configurable() -> None:
    client = FakeSSHClient(default_exit_code=2, default_stderr=lambda cmd: f"fail: {cmd}\n")
    client.connect("192.0.2.10")
    _, _, stderr = client.exec_command("anything")
    assert stderr.read() == b"fail: anything\n"


def test_command_log_tracks_all_calls(fake: FakeSSHClient) -> None:
    fake.exec_command("echo hi")
    fake.exec_command("echo there", timeout=3)
    assert fake.commands_run == ["echo hi", "echo there"]
    assert fake.command_records[1]["timeout"] == 3
    assert fake.command_records[0]["matched_pattern"] is None


def test_exec_before_connect_raises() -> None:
    client = FakeSSHClient()
    with pytest.raises(RuntimeError, match="before connect"):
        client.exec_command("echo hi")


def test_close_blocks_further_use(fake: FakeSSHClient) -> None:
    fake.close()
    with pytest.raises(RuntimeError, match="closed"):
        fake.exec_command("echo hi")


def test_sftp_round_trip(fake: FakeSSHClient, tmp_path: Path) -> None:
    sftp = fake.open_sftp()
    local_out = tmp_path / "captures" / "foo.rgp"
    sftp.get("/tmp/foo.rgp", str(local_out))
    assert local_out.read_bytes() == b"fake-rgp-bytes"

    local_in = tmp_path / "note.txt"
    local_in.write_text("hello device")
    sftp.put(str(local_in), "/home/deck/note.txt")
    assert fake.filesystem["/home/deck/note.txt"] == b"hello device"

    assert sftp.stat("/tmp/foo.rgp").st_size == len(b"fake-rgp-bytes")
    assert sftp.listdir("/tmp") == ["foo.rgp"]
    assert "hello_triangle" in sftp.listdir("/home/deck/devkit-game")

    with pytest.raises(FileNotFoundError):
        sftp.get("/tmp/missing.bin", str(tmp_path / "nope"))
    with pytest.raises(FileNotFoundError):
        sftp.stat("/tmp/missing.bin")


def test_spawned_clients_share_state(fake: FakeSSHClient) -> None:
    child = fake.spawn()
    child.connect("192.0.2.10", username="deck")
    child.exec_command("echo from-child")
    sftp = child.open_sftp()
    assert sftp.stat("/tmp/foo.rgp").st_size == len(b"fake-rgp-bytes")
    assert fake.commands_run == ["echo from-child"]
    assert len(fake.connect_records) == 2  # parent fixture connect + child


def test_make_paramiko_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    paramiko = pytest.importorskip("paramiko")
    fake = make_paramiko_patch(
        monkeypatch,
        commands=[(r"echo hi", b"hi\n", b"", 0)],
    )
    client = paramiko.SSHClient()
    client.connect("192.0.2.10", username="deck")
    _, stdout, _ = client.exec_command("echo hi")
    assert stdout.read() == b"hi\n"
    assert fake.commands_run == ["echo hi"]
