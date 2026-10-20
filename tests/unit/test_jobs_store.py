"""Unit tests for jobs.store (spec section 7.1)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.jobs import JobStore
from steamos_devkit.models import JobKind, JobProgress, JobState


def _dead_pid() -> int:
    """A pid that definitely no longer exists (spawned and reaped here)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs")


def test_create_and_load_roundtrip(store: JobStore) -> None:
    info = store.create(
        kind=JobKind.DEPLOY, device="deck", gameid="my_game", spec={"a": 1}, timeout_s=60
    )
    assert info.state == JobState.QUEUED
    loaded = store.load(info.id)
    assert loaded.id == info.id
    assert loaded.kind == JobKind.DEPLOY
    assert loaded.device == "deck"
    assert loaded.gameid == "my_game"
    assert loaded.timeout_s == 60
    assert loaded.artifacts == []


def test_full_lifecycle(store: JobStore) -> None:
    info = store.create(kind=JobKind.SCREENSHOT, device="deck")
    store.start(info.id, 1234)
    store.progress(info.id, JobProgress(phase="transferring", bytes_done=10, bytes_total=100))
    store.attach_artifact(info.id, "art_01ARZ3NDEKTSV4RRFFQ69G5FAV")
    store.finish(info.id, JobState.SUCCEEDED, message="done")
    loaded = store.load(info.id)
    assert loaded.state == JobState.SUCCEEDED
    assert loaded.started_at is not None
    assert loaded.finished_at is not None
    assert loaded.progress is not None and loaded.progress.bytes_done == 10
    assert loaded.artifacts == ["art_01ARZ3NDEKTSV4RRFFQ69G5FAV"]
    assert loaded.message == "done"


def test_finish_with_error_code(store: JobStore) -> None:
    info = store.create(kind=JobKind.DEPLOY)
    store.finish(info.id, JobState.FAILED, message="boom", code=ErrorCode.REMOTE_COMMAND_FAILED)
    loaded = store.load(info.id)
    assert loaded.state == JobState.FAILED
    assert loaded.code == ErrorCode.REMOTE_COMMAND_FAILED


def test_finish_rejects_non_terminal(store: JobStore) -> None:
    info = store.create(kind=JobKind.DEPLOY)
    with pytest.raises(DevkitError):
        store.finish(info.id, JobState.RUNNING)


def test_load_missing_job(store: JobStore) -> None:
    store.directory.mkdir(parents=True)
    with pytest.raises(DevkitError) as exc_info:
        store.load("job_01ARZ3NDEKTSV4RRFFQ69G5FAV")
    assert exc_info.value.code == ErrorCode.JOB_NOT_FOUND


def test_id_traversal_guard(store: JobStore) -> None:
    with pytest.raises(DevkitError) as exc_info:
        store.load("../etc/passwd")
    assert exc_info.value.code == ErrorCode.JOB_NOT_FOUND


def test_malformed_journal(store: JobStore) -> None:
    info = store.create(kind=JobKind.DEPLOY)
    with open(store.path_for(info.id), "a", encoding="utf-8") as fh:
        fh.write("not json\n")
    with pytest.raises(DevkitError) as exc_info:
        store.load(info.id)
    assert exc_info.value.code == ErrorCode.STATE_CORRUPT


def test_list_filters(store: JobStore) -> None:
    a = store.create(kind=JobKind.DEPLOY, device="deck")
    b = store.create(kind=JobKind.SCREENSHOT, device="frame")
    store.finish(b.id, JobState.SUCCEEDED)
    assert [i.id for i in store.list_jobs()] == [a.id, b.id]
    assert [i.id for i in store.list_jobs(device="deck")] == [a.id]
    assert [i.id for i in store.list_jobs(state=JobState.SUCCEEDED)] == [b.id]


def test_recover_interrupted(store: JobStore) -> None:
    running = store.create(kind=JobKind.DEPLOY, device="deck")
    store.start(running.id, _dead_pid())
    done = store.create(kind=JobKind.DEPLOY, device="deck")
    store.finish(done.id, JobState.SUCCEEDED)
    recovered = store.recover_interrupted()
    assert recovered == [running.id]
    assert store.load(running.id).state == JobState.INTERRUPTED
    assert store.load(done.id).state == JobState.SUCCEEDED


def test_recover_leaves_live_pid_alone(store: JobStore) -> None:
    live = store.create(kind=JobKind.DEPLOY)
    store.start(live.id, os.getpid())  # this process is alive
    dead = store.create(kind=JobKind.DEPLOY)
    store.start(dead.id, _dead_pid())
    queued = store.create(kind=JobKind.DEPLOY)  # never started: no pid
    recovered = store.recover_interrupted()
    assert sorted(recovered) == sorted([dead.id, queued.id])
    assert store.load(live.id).state == JobState.RUNNING
    assert store.load(dead.id).state == JobState.INTERRUPTED
    assert store.load(queued.id).state == JobState.INTERRUPTED


def test_torn_tail_tolerated(store: JobStore) -> None:
    info = store.create(kind=JobKind.DEPLOY, device="deck")
    store.start(info.id, _dead_pid())
    # crash mid-append: truncated final record without a trailing newline
    with open(store.path_for(info.id), "a", encoding="utf-8") as fh:
        fh.write('{"t":"progress","phase":"transferring","bytes_')
    loaded = store.load(info.id)
    assert loaded.state == JobState.RUNNING


def test_mid_journal_corruption_still_raises(store: JobStore) -> None:
    info = store.create(kind=JobKind.DEPLOY)
    path = store.path_for(info.id)
    content = path.read_text(encoding="utf-8")
    path.write_text("garbage line\n" + content, encoding="utf-8")
    with pytest.raises(DevkitError) as exc_info:
        store.load(info.id)
    assert exc_info.value.code == ErrorCode.STATE_CORRUPT


def test_list_jobs_skips_corrupt_journal(store: JobStore) -> None:
    good = store.create(kind=JobKind.DEPLOY)
    bad = store.create(kind=JobKind.SCREENSHOT)
    store.path_for(bad.id).write_text("not json at all\n", encoding="utf-8")
    infos = store.list_jobs()
    assert [i.id for i in infos] == [good.id]
    # direct load still surfaces the corruption
    with pytest.raises(DevkitError) as exc_info:
        store.load(bad.id)
    assert exc_info.value.code == ErrorCode.STATE_CORRUPT


def test_loads_golden_success_journal(tmp_path: Path) -> None:
    fixture = Path(__file__).parent.parent / "fixtures" / "job-journal-success.jsonl"
    first = json.loads(fixture.read_text(encoding="utf-8").splitlines()[0])
    job_id = first["job"]["id"]
    store = JobStore(tmp_path / "jobs")
    store.directory.mkdir(parents=True)
    (store.directory / f"{job_id}.jsonl").write_text(fixture.read_text(encoding="utf-8"))
    loaded = store.load(job_id)
    assert loaded.state == JobState.SUCCEEDED
    assert loaded.kind == JobKind.DEPLOY
    assert loaded.device == "deck"
    assert loaded.message == "deploy complete"  # from the result payload


def test_recovers_golden_interrupted_journal(tmp_path: Path) -> None:
    fixture = Path(__file__).parent.parent / "fixtures" / "job-journal-interrupted.jsonl"
    first = json.loads(fixture.read_text(encoding="utf-8").splitlines()[0])
    job_id = first["job"]["id"]
    store = JobStore(tmp_path / "jobs")
    store.directory.mkdir(parents=True)
    (store.directory / f"{job_id}.jsonl").write_text(fixture.read_text(encoding="utf-8"))
    loaded = store.load(job_id)
    assert loaded.state == JobState.RUNNING
    assert loaded.progress is not None and loaded.progress.bytes_done == 12582912
    assert store.recover_interrupted() == [job_id]
    assert store.load(job_id).state == JobState.INTERRUPTED
