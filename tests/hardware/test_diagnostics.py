"""Diagnostics on hardware: steam log pull and a gamescope screenshot.

Covers hardware-matrix rows "Logs" and "Screenshot". The diagnostics module
is imported lazily inside each test so collection (and therefore the
DEVKIT_TEST_DEVICE skip) never depends on its presence on a branch.
"""

from __future__ import annotations

from collections.abc import Callable

from steamos_devkit.artifacts import get_artifact, read_artifact_excerpt
from steamos_devkit.core import DevkitCore
from steamos_devkit.models import JobState

HwRecord = Callable[..., None]


def test_fetch_logs_steam(core: DevkitCore, device_selector: str, hwrecord: HwRecord) -> None:
    from steamos_devkit import diagnostics

    handle = diagnostics.fetch_logs(core, device_selector, kinds=["steam"])
    info = handle.wait()
    assert info.state == JobState.SUCCEEDED
    assert info.artifacts, "fetch_logs registered no artifacts"
    artifact = get_artifact(core.state, info.artifacts[0])
    assert artifact.kind == "logs"
    assert artifact.path.exists()
    if artifact.bytes > 0:  # a fresh device may genuinely have no steam logs yet
        excerpt = read_artifact_excerpt(core.state, artifact.id)
        assert excerpt.strip(), "device has logs but the excerpt is empty"
    hwrecord("fetch_logs", "pass", bytes=artifact.bytes, artifact=artifact.id)


def test_screenshot_mode_3(core: DevkitCore, device_selector: str, hwrecord: HwRecord) -> None:
    from steamos_devkit import diagnostics

    handle = diagnostics.screenshot(core, device_selector, mode=3)
    info = handle.wait()
    assert info.state == JobState.SUCCEEDED
    assert info.artifacts, "screenshot registered no artifact"
    artifact = get_artifact(core.state, info.artifacts[0])
    assert artifact.kind == "screenshot"
    data = artifact.path.read_bytes()
    assert data[:4] == b"\x89PNG", f"artifact is not a PNG (head: {data[:8]!r})"
    hwrecord("screenshot", "pass", mode=3, bytes=len(data), artifact=artifact.id)
