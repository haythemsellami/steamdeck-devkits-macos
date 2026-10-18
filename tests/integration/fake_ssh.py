r"""Scriptable fake of the paramiko SSH/SFTP surface used by the devkit core.

Covers exactly the surface upstream (and therefore our core) uses
(``client/devkit_client/__init__.py`` at the vendored commit):

- ``SSHClient.set_missing_host_key_policy(policy)``
- ``SSHClient.connect(host, username=..., pkey=..., timeout=..., look_for_keys=False)``
- ``SSHClient.exec_command(cmd, timeout=None)`` -> ``(stdin, stdout, stderr)``
  channel-likes: ``stdout.read()``/``readline()`` return bytes,
  ``stdout.channel.recv_exit_status()`` returns the scripted exit code,
  ``stdin.write()``/``flush()``/``close()``/``channel.shutdown_write()`` work.
- ``SSHClient.open_sftp()`` -> ``SFTPClient`` with ``get``/``put``/``listdir``/``stat``
  backed by an in-memory dict-of-bytes "remote filesystem".
- ``SSHClient.close()``

Runtime imports are stdlib-only; ``paramiko`` and ``pytest`` appear under
``TYPE_CHECKING`` for annotations. Nothing is imported from the project —
this module must work before the package under test exists.

Command scripting
-----------------
``commands`` is a list matched against each ``exec_command`` string with
``re.search``, first matching rule with remaining uses wins. Plain form::

    FakeSSHClient(commands=[
        (r"steamos-get-status --json", STATUS_JSON_BYTES, b"", 0),
    ])

For polling loops (screenshot / RGP stability) stack rules with
``max_uses=1`` so successive calls fall through to the next rule::

    FakeSSHClient(commands=[
        ScriptedResponse(r"ls -1t /tmp/\*\.rgp", b"", b"", 1, max_uses=2),
        ScriptedResponse(r"ls -1t /tmp/\*\.rgp", b"/tmp/foo.rgp\n", b"", 0),
    ])

Unmatched commands get a shell-like "command not found" on stderr and a
non-zero exit code (both configurable). Upstream greps stderr for
``gpu-trace: command not found``, so the default message keeps the
``<argv0>: command not found`` wording with bash's exit code 127.

One ``FakeSSHClient`` instance = one connection. Upstream opens a fresh
``SSHClient`` per operation, so ``make_paramiko_patch`` installs a factory
returning ``spawn()`` children that share the parent's command table,
filesystem, and call records.
"""

from __future__ import annotations

import io
import posixpath
import re
import stat as statmod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

BytesLike = bytes | str
CommandRule = tuple[str, BytesLike, BytesLike, int]

COMMAND_NOT_FOUND_EXIT_CODE = 127


def _as_bytes(value: BytesLike) -> bytes:
    if isinstance(value, str):
        return value.encode("utf-8")
    return value


@dataclass
class ScriptedResponse:
    """One command-table entry. ``max_uses=None`` means unlimited use."""

    pattern: str
    stdout: BytesLike = b""
    stderr: BytesLike = b""
    exit_code: int = 0
    max_uses: int | None = None
    uses: int = 0

    def matches(self, command: str) -> bool:
        if self.max_uses is not None and self.uses >= self.max_uses:
            return False
        return re.search(self.pattern, command) is not None

    def record_use(self) -> None:
        self.uses += 1


def _normalize_rule(rule: CommandRule | ScriptedResponse) -> ScriptedResponse:
    if isinstance(rule, ScriptedResponse):
        return rule
    pattern, stdout, stderr, exit_code = rule
    return ScriptedResponse(pattern=pattern, stdout=stdout, stderr=stderr, exit_code=exit_code)


class FakeChannel:
    """Stand-in for ``paramiko.Channel`` (exit status + write-side shutdown)."""

    def __init__(self, exit_code: int) -> None:
        self._exit_code = exit_code
        self.shutdown_write_called = False

    def recv_exit_status(self) -> int:
        return self._exit_code

    def shutdown_write(self) -> None:
        self.shutdown_write_called = True


class FakeChannelFile(io.BytesIO):
    """``paramiko.ChannelFile``-like: BytesIO with a ``.channel`` attribute."""

    def __init__(self, channel: FakeChannel, data: bytes) -> None:
        super().__init__(data)
        self.channel = channel


class FakeChannelStdin(io.BytesIO):
    """``paramiko.ChannelStdinFile``-like: writable, closable, introspectable.

    Unlike a real closed buffer, ``getvalue()`` keeps working after
    ``close()`` so tests can assert on what the client wrote.
    """

    def __init__(self, channel: FakeChannel) -> None:
        super().__init__()
        self.channel = channel
        self._stdin_closed = False

    def write(self, data: bytes) -> int:  # type: ignore[override]
        if self._stdin_closed:
            raise ValueError("I/O operation on closed file")
        return super().write(data)

    def close(self) -> None:
        self._stdin_closed = True

    @property
    def closed_for_write(self) -> bool:
        return self._stdin_closed


class FakeSFTPAttributes:
    """Subset of ``paramiko.SFTPAttributes`` used by the core (``st_size``)."""

    def __init__(self, st_size: int = 0, st_mode: int = 0o100644, st_mtime: int = 0) -> None:
        self.st_size = st_size
        self.st_mode = st_mode
        self.st_mtime = st_mtime


class FakeSFTP:
    """``paramiko.SFTPClient`` subset over a dict-of-bytes remote filesystem.

    Paths are plain strings as the core passes them (``/tmp/x.rgp``,
    ``~/devkit-game/...``); ``~`` is not expanded — the fake never shells out.
    Directories are implicit in key prefixes.
    """

    def __init__(self, filesystem: dict[str, bytes]) -> None:
        self._fs = filesystem
        self.closed = False

    def _require_open(self) -> None:
        if self.closed:
            raise RuntimeError("FakeSFTP: operation on closed SFTP session")

    @staticmethod
    def _norm(path: str) -> str:
        return posixpath.normpath(path)

    def _read_remote(self, remotepath: str) -> bytes:
        key = self._norm(remotepath)
        try:
            return self._fs[key]
        except KeyError:
            raise FileNotFoundError(f"No such file: {remotepath}") from None

    def get(self, remotepath: str, localpath: str) -> FakeSFTPAttributes:
        """Copy remote -> local, creating local parent dirs like callers expect."""
        self._require_open()
        data = self._read_remote(remotepath)
        local = Path(localpath)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        return FakeSFTPAttributes(st_size=len(data))

    def put(self, localpath: str, remotepath: str) -> FakeSFTPAttributes:
        self._require_open()
        data = Path(localpath).read_bytes()
        self._fs[self._norm(remotepath)] = data
        return FakeSFTPAttributes(st_size=len(data))

    def listdir(self, path: str = ".") -> list[str]:
        """Basenames of direct children, mirroring ``paramiko.SFTPClient.listdir``."""
        self._require_open()
        norm = self._norm(path)
        prefix = "" if norm in (".", "/") else norm.rstrip("/") + "/"
        children = set()
        for key in self._fs:
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix) :].lstrip("/")
            if rest:
                children.add(rest.split("/", 1)[0])
        if not children and norm not in (".", "/"):
            if not self._is_dir(path) and norm not in self._fs:
                raise FileNotFoundError(f"No such file or directory: {path}")
        return sorted(children)

    def _is_dir(self, path: str) -> bool:
        prefix = self._norm(path).rstrip("/") + "/"
        return any(key.startswith(prefix) for key in self._fs)

    def stat(self, path: str) -> FakeSFTPAttributes:
        self._require_open()
        key = self._norm(path)
        if key in self._fs:
            return FakeSFTPAttributes(st_size=len(self._fs[key]), st_mode=statmod.S_IFREG | 0o644)
        if self._is_dir(path) or key in ("/",):
            return FakeSFTPAttributes(st_size=0, st_mode=statmod.S_IFDIR | 0o755)
        raise FileNotFoundError(f"No such file: {path}")

    def close(self) -> None:
        self.closed = True


@dataclass
class _SharedState:
    """State shared by a fake and its ``spawn()``-ed children."""

    rules: list[ScriptedResponse]
    filesystem: dict[str, bytes]
    connects: list[dict[str, Any]]
    commands: list[dict[str, Any]]
    default_exit_code: int
    default_stderr: BytesLike | Callable[[str], BytesLike] | None


class FakeSSHClient:
    """Drop-in fake for ``paramiko.SSHClient`` covering the devkit surface."""

    def __init__(
        self,
        *,
        commands: Iterable[CommandRule | ScriptedResponse] = (),
        filesystem: Mapping[str, BytesLike] | None = None,
        default_exit_code: int = COMMAND_NOT_FOUND_EXIT_CODE,
        default_stderr: BytesLike | Callable[[str], BytesLike] | None = None,
        _state: _SharedState | None = None,
    ) -> None:
        if _state is None:
            _state = _SharedState(
                rules=[_normalize_rule(rule) for rule in commands],
                filesystem={key: _as_bytes(value) for key, value in (filesystem or {}).items()},
                connects=[],
                commands=[],
                default_exit_code=default_exit_code,
                default_stderr=default_stderr,
            )
        self._state = _state
        self.missing_host_key_policy: Any = None
        self.connected = False
        self.closed = False

    # -- introspection helpers (shared across spawns) ----------------------

    @property
    def connect_records(self) -> list[dict[str, Any]]:
        return self._state.connects

    @property
    def command_records(self) -> list[dict[str, Any]]:
        return self._state.commands

    @property
    def commands_run(self) -> list[str]:
        return [record["command"] for record in self._state.commands]

    @property
    def filesystem(self) -> dict[str, bytes]:
        return self._state.filesystem

    def spawn(self) -> FakeSSHClient:
        """A fresh "connection" sharing this fake's script, fs, and records."""
        return FakeSSHClient(_state=self._state)

    # -- paramiko surface ---------------------------------------------------

    def set_missing_host_key_policy(self, policy: Any) -> None:
        self.missing_host_key_policy = policy

    def connect(
        self,
        hostname: str,
        port: int = 22,
        username: str | None = None,
        pkey: Any = None,
        timeout: float | None = None,
        look_for_keys: bool = True,
        allow_agent: bool = True,
        **kwargs: Any,
    ) -> None:
        if self.closed:
            raise RuntimeError("FakeSSHClient: connect() on a closed client")
        self._state.connects.append(
            {
                "hostname": hostname,
                "port": port,
                "username": username,
                "pkey": pkey,
                "timeout": timeout,
                "look_for_keys": look_for_keys,
                "allow_agent": allow_agent,
                "extra": kwargs,
            }
        )
        self.connected = True

    def _require_connected(self, op: str) -> None:
        if self.closed:
            raise RuntimeError(f"FakeSSHClient: {op}() on a closed client")
        if not self.connected:
            raise RuntimeError(f"FakeSSHClient: {op}() before connect()")

    def _default_stderr(self, command: str) -> bytes:
        configured = self._state.default_stderr
        if configured is None:
            argv0 = command.split(None, 1)[0] if command.split() else command
            return f"bash: {argv0}: command not found\n".encode()
        if callable(configured):
            return _as_bytes(configured(command))
        return _as_bytes(configured)

    def exec_command(
        self,
        command: str,
        bufsize: int = -1,
        timeout: float | None = None,
        get_pty: bool = False,
        environment: Any = None,
    ) -> tuple[FakeChannelStdin, FakeChannelFile, FakeChannelFile]:
        self._require_connected("exec_command")
        rule = next((rule for rule in self._state.rules if rule.matches(command)), None)
        if rule is not None:
            rule.record_use()
            stdout_bytes = _as_bytes(rule.stdout)
            stderr_bytes = _as_bytes(rule.stderr)
            exit_code = rule.exit_code
        else:
            stdout_bytes = b""
            stderr_bytes = self._default_stderr(command)
            exit_code = self._state.default_exit_code
        self._state.commands.append(
            {
                "command": command,
                "timeout": timeout,
                "get_pty": get_pty,
                "matched_pattern": rule.pattern if rule is not None else None,
                "exit_code": exit_code,
            }
        )
        channel = FakeChannel(exit_code)
        return (
            FakeChannelStdin(channel),
            FakeChannelFile(channel, stdout_bytes),
            FakeChannelFile(channel, stderr_bytes),
        )

    def open_sftp(self) -> FakeSFTP:
        self._require_connected("open_sftp")
        return FakeSFTP(self._state.filesystem)

    def close(self) -> None:
        self.connected = False
        self.closed = True


def make_paramiko_patch(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeSSHClient | None = None,
    **client_kwargs: Any,
) -> FakeSSHClient:
    """Swap ``paramiko.SSHClient`` for a factory of fakes sharing one state.

    Returns the root fake for scripting and assertions::

        fake = make_paramiko_patch(monkeypatch, commands=[...])
        ...  # exercise core code
        assert fake.commands_run == [...]

    Every ``paramiko.SSHClient()`` the code under test constructs returns a
    ``spawn()``-ed child, so per-operation reconnects (upstream's pattern)
    share the command table, filesystem, and call records.
    """
    if fake is None:
        fake = FakeSSHClient(**client_kwargs)
    monkeypatch.setattr("paramiko.SSHClient", lambda *args, **kwargs: fake.spawn())
    return fake
