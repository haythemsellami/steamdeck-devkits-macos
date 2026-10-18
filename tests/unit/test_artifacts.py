"""Unit tests for the artifact store (spec sections 3 and 8)."""

from pathlib import Path

import pytest

from steamos_devkit.artifacts import (
    EXCERPT_MAX_LINES,
    get_artifact,
    list_artifacts,
    read_artifact_excerpt,
    register_artifact,
)
from steamos_devkit.config import StateStore
from steamos_devkit.errors import DevkitError, ErrorCode

JOB = "job_01ARZ3NDEKTSV4RRFFQ69G5FAV"


@pytest.fixture
def state(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "state")
    store.ensure_layout()
    return store


def _register_file(state: StateStore, device: str = "deck", kind: str = "logs"):
    src = state.root / "staging.txt"
    src.write_text("line one\nline two\n", encoding="utf-8")
    return register_artifact(
        state,
        job_id=JOB,
        device=device,
        kind=kind,
        source=src,
        remote_source="/tmp/remote",
    )


def test_register_file_moves_and_records(state: StateStore) -> None:
    src = state.root / "shot.png"
    src.write_bytes(b"\x89PNG fake")
    artifact = register_artifact(
        state,
        job_id=JOB,
        device="deck",
        kind="screenshot",
        source=src,
        remote_source="/tmp/gamescope.png",
        media_type="image/png",
    )
    assert not src.exists(), "source must be moved, not copied"
    assert artifact.path.exists()
    assert artifact.bytes == 9
    assert len(artifact.sha256) == 64
    meta = artifact.path.parent / "meta.json"
    assert meta.exists()


def test_register_directory_hashes_manifest(state: StateStore) -> None:
    tree = state.root / "logtree"
    (tree / "sub").mkdir(parents=True)
    (tree / "a.log").write_text("aaa")
    (tree / "sub" / "b.log").write_text("bbb")
    artifact = register_artifact(
        state, job_id=JOB, device="deck", kind="logs", source=tree, remote_source="/home/deck"
    )
    assert artifact.path.is_dir()
    assert artifact.bytes == 6
    assert len(artifact.sha256) == 64


def test_register_missing_source_rejected(state: StateStore) -> None:
    with pytest.raises(DevkitError) as exc_info:
        register_artifact(
            state,
            job_id=JOB,
            device="deck",
            kind="logs",
            source=state.root / "nope",
            remote_source="/tmp/x",
        )
    assert exc_info.value.code == ErrorCode.VALIDATION


def test_device_name_sanitized_in_path(state: StateStore) -> None:
    artifact = _register_file(state, device="Living Room Deck")
    assert "Living_Room_Deck" in str(artifact.path)
    assert artifact.device == "Living Room Deck"


def test_list_and_get(state: StateStore) -> None:
    a = _register_file(state, device="deck", kind="logs")
    b = _register_file(state, device="frame", kind="screenshot")
    assert [x.id for x in list_artifacts(state)] == [a.id, b.id]
    assert [x.id for x in list_artifacts(state, device="deck")] == [a.id]
    assert [x.id for x in list_artifacts(state, kind="screenshot")] == [b.id]
    assert get_artifact(state, a.id).id == a.id
    with pytest.raises(DevkitError) as exc_info:
        get_artifact(state, "art_01ARZ3NDEKTSV4RRFFQ69G5FAV")
    assert exc_info.value.code == ErrorCode.ARTIFACT_NOT_FOUND


def test_excerpt_bounds(state: StateStore) -> None:
    src = state.root / "big.log"
    src.write_text("".join(f"line {i}\n" for i in range(3000)), encoding="utf-8")
    artifact = register_artifact(
        state, job_id=JOB, device="deck", kind="logs", source=src, remote_source="/x"
    )
    excerpt = read_artifact_excerpt(state, artifact.id, tail_lines=10)
    lines = excerpt.splitlines()
    assert len(lines) == 10
    assert lines[-1] == "line 2999"
    # over-limit requests are clamped, not fatal
    big = read_artifact_excerpt(state, artifact.id, tail_lines=99999)
    assert len(big.splitlines()) <= EXCERPT_MAX_LINES


def test_excerpt_rejects_directory(state: StateStore) -> None:
    tree = state.root / "d"
    tree.mkdir()
    (tree / "f").write_text("x")
    artifact = register_artifact(
        state, job_id=JOB, device="deck", kind="logs", source=tree, remote_source="/x"
    )
    with pytest.raises(DevkitError) as exc_info:
        read_artifact_excerpt(state, artifact.id)
    assert exc_info.value.code == ErrorCode.VALIDATION
