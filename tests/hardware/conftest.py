"""Hardware-suite guard: nothing here runs without an explicit target device.

Hardware tests contact a real SteamOS device over the network. They are
skipped unless ``DEVKIT_TEST_DEVICE=<name-or-ip>`` is set in the environment,
which keeps them out of CI and out of accidental local runs. See README.md
in this directory for how to run them and how to record results.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

_DEVICE_ENV_VAR = "DEVKIT_TEST_DEVICE"

# Reserved throwaway gameid for destructive hardware tests. Never deploy
# anything else in this suite, and always clean up with titles delete.
TEST_GAMEID = "zz_devkit_test"


_HARDWARE_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # This conftest lives in tests/hardware but pytest applies directory
    # conftest hooks to every item collected under the rootdir invocation,
    # so scope the skip explicitly to items collected from this directory.
    if os.environ.get(_DEVICE_ENV_VAR):
        return
    skip = pytest.mark.skip(
        reason=f"{_DEVICE_ENV_VAR} not set; hardware tests only run against an explicit device"
    )
    for item in items:
        if Path(str(item.fspath)).resolve().is_relative_to(_HARDWARE_DIR):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def device_selector() -> str:
    """The device selector (registry name or IPv4) the hardware run targets."""
    return os.environ[_DEVICE_ENV_VAR]


@pytest.fixture(scope="session")
def hardware_gameid() -> Iterator[str]:
    """The throwaway gameid destructive tests are allowed to use."""
    yield TEST_GAMEID
