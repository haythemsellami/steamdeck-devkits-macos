"""Deploy cycle on hardware: configure -> preview -> deploy+launch -> listed.

Covers hardware-matrix rows "Deploy Linux-native" and "Launch". Uses the
throwaway ``zz_devkit_test`` gameid only; the title stays deployed on success
for ``test_cleanup.py`` to remove (and is removed best-effort on failure).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from steamos_devkit import titles as titles_mod
from steamos_devkit.core import DevkitCore
from steamos_devkit.models import DeploySpec, JobState, TitleConfig
from steamos_devkit.transfer import preview_deploy

HwRecord = Callable[..., None]


@pytest.fixture
def payload(tmp_path: Path) -> Path:
    """A minimal Linux x86_64 payload: a shell-script entrypoint plus a
    run-unique stamp file so the preview always reports an added file."""
    source = tmp_path / "payload"
    source.mkdir()
    entrypoint = source / "run.sh"
    entrypoint.write_text("#!/bin/sh\necho zz_devkit_test alive\n", encoding="utf-8")
    entrypoint.chmod(0o755)
    stamp = source / f"stamp-{os.urandom(4).hex()}.txt"
    stamp.write_text("run-unique marker\n", encoding="utf-8")
    return source


def test_deploy_cycle(
    core: DevkitCore,
    device_selector: str,
    hardware_gameid: str,
    payload: Path,
    hwrecord: HwRecord,
    force_delete: Callable[[DevkitCore, str, str], None],
) -> None:
    config = TitleConfig(entrypoint="run.sh")
    try:
        result = titles_mod.configure_title(core, device_selector, hardware_gameid, config)
        assert result.ok

        spec = DeploySpec(
            device=device_selector,
            gameid=hardware_gameid,
            source_dir=payload,
            launch=True,
            config=config,
        )
        preview = preview_deploy(core, device_selector, spec)
        assert preview.files_added >= 1, f"preview expected added files, got {preview}"

        handle = titles_mod.deploy(core, device_selector, spec)
        info = handle.wait()
        assert info.state == JobState.SUCCEEDED
        message = info.message or ""
        assert "upload complete" in message
        assert "registered with Steam" in message  # registration_complete
        assert "launch requested" in message

        listed = {t.gameid for t in titles_mod.list_titles(core, device_selector)}
        assert hardware_gameid in listed, f"title missing from titles list: {listed}"
    except Exception:
        force_delete(core, device_selector, hardware_gameid)
        raise
    hwrecord(
        "deploy_cycle",
        "pass",
        files_added=preview.files_added,
        bytes_to_transfer=preview.bytes_to_transfer,
    )
