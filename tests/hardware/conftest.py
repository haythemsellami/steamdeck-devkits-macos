"""Hardware-suite guard: nothing here runs without an explicit target device.

Hardware tests contact a real SteamOS device over the network. They are
skipped unless ``DEVKIT_TEST_DEVICE=<name-or-ip>`` is set in the environment,
which keeps them out of CI and out of accidental local runs. See README.md
in this directory for how to run them and how to record results.

Modules run in a fixed order (pair first — everything needs the key it
creates; the destructive sweep last), not pytest's alphabetical default.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from steamos_devkit.core import DevkitCore

_DEVICE_ENV_VAR = "DEVKIT_TEST_DEVICE"

# Reserved throwaway gameid for destructive hardware tests. Never deploy
# anything else in this suite, and always clean up with titles delete.
TEST_GAMEID = "zz_devkit_test"


_HARDWARE_DIR = Path(__file__).resolve().parent

# Fixed module execution order.
_MODULE_ORDER = (
    "test_pair_and_status",
    "test_deploy_cycle",
    "test_diagnostics",
    "test_failure_modes",
    "test_cleanup",
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # This conftest lives in tests/hardware but pytest applies directory
    # conftest hooks to every item collected under the rootdir invocation,
    # so scope both the ordering and the skip explicitly to items collected
    # from this directory.
    positions = [
        index
        for index, item in enumerate(items)
        if Path(str(item.fspath)).resolve().is_relative_to(_HARDWARE_DIR)
    ]
    if positions:
        rank = {stem: n for n, stem in enumerate(_MODULE_ORDER)}
        ordered = sorted(
            (items[index] for index in positions),
            key=lambda item: rank.get(Path(str(item.fspath)).stem, len(rank)),
        )
        for index, item in zip(positions, ordered, strict=True):
            items[index] = item
    if os.environ.get(_DEVICE_ENV_VAR):
        return
    skip = pytest.mark.skip(
        reason=f"{_DEVICE_ENV_VAR} not set; hardware tests only run against an explicit device"
    )
    for index in positions:
        items[index].add_marker(skip)


@pytest.fixture(scope="session")
def device_selector() -> str:
    """The device selector (registry name or IPv4) the hardware run targets."""
    return os.environ[_DEVICE_ENV_VAR]


@pytest.fixture(scope="session")
def hardware_gameid() -> Iterator[str]:
    """The throwaway gameid destructive tests are allowed to use."""
    yield TEST_GAMEID


@pytest.fixture(scope="session")
def core() -> Iterator[DevkitCore]:
    """The real core against the operator's state root (DEVKIT_STATE_DIR honored)."""
    from steamos_devkit.core import DevkitCore

    devkit_core = DevkitCore()
    devkit_core.state.ensure_layout()
    yield devkit_core
    devkit_core.job_runner.shutdown()


HwRecord = Callable[..., None]


@pytest.fixture
def hwrecord() -> HwRecord:
    """One-line summary per test, printed for the hardware-matrix recorder.

    Format: ``[hardware-matrix] <test>: <outcome> key=value...`` — transcribe
    these into docs/hardware-matrix.md after a run (see README.md here).
    """

    def _record(test: str, outcome: str, **details: object) -> None:
        suffix = "".join(f" {key}={value}" for key, value in details.items())
        print(f"[hardware-matrix] {test}: {outcome}{suffix}")

    return _record


@pytest.fixture
def force_delete() -> Callable[[DevkitCore, str, str], None]:
    """Best-effort title cleanup that never masks a test's own failure."""

    def _delete(core: DevkitCore, selector: str, gameid: str) -> None:
        from steamos_devkit.errors import DevkitError
        from steamos_devkit.titles import delete_title

        try:
            delete_title(core, selector, gameid, confirm=True)
        except DevkitError:
            pass
        core.state.delete_title(gameid)  # local per-title config

    return _delete
