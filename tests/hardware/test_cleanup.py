"""Final sweep: delete the throwaway hardware title (ordered last, see conftest).

Destructive tests leave ``zz_devkit_test`` deployed on success so this test
has something to delete; on partial runs it is a no-op pass.
"""

from __future__ import annotations

from collections.abc import Callable

from steamos_devkit import titles as titles_mod
from steamos_devkit.core import DevkitCore

HwRecord = Callable[..., None]


def test_cleanup_title(
    core: DevkitCore, device_selector: str, hardware_gameid: str, hwrecord: HwRecord
) -> None:
    listed = {t.gameid for t in titles_mod.list_titles(core, device_selector)}
    if hardware_gameid not in listed:
        hwrecord("cleanup", "pass", note="nothing-to-clean")
        return
    result = titles_mod.delete_title(core, device_selector, hardware_gameid, confirm=True)
    assert result.ok
    remaining = {t.gameid for t in titles_mod.list_titles(core, device_selector)}
    assert hardware_gameid not in remaining, f"title still present after delete: {remaining}"
    core.state.delete_title(hardware_gameid)  # local per-title config
    hwrecord("cleanup", "pass", deleted=hardware_gameid)
