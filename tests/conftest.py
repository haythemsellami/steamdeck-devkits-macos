"""Shared pytest fixtures for the steamos-devkit test suite.

Deliberately stdlib-only (plus pytest itself): this module must be importable
before the package under test exists, and must never import ``steamos_devkit``
— earlier waves land the package after this infrastructure.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the devkit state root into a per-test temporary directory.

    Every state file the core writes (keys, devices.json, titles/, jobs/,
    artifacts/, logs/) lands under the returned path. Nothing touches the
    real ``~/Library/Application Support/steamos-devkit/``.
    """
    root = tmp_path / "devkit-state"
    root.mkdir()
    monkeypatch.setenv("DEVKIT_STATE_DIR", str(root))
    return root


@pytest.fixture
def fixture_file() -> Callable[[str], str]:
    """Return the text of a file in ``tests/fixtures/`` by bare name.

    Usage: ``fixture_file("rsync-progress2-success.txt")``.
    Raises ``FileNotFoundError`` for unknown names — typos fail loudly.
    """

    def _load(name: str) -> str:
        path = FIXTURES_DIR / name
        return path.read_text(encoding="utf-8")

    return _load
