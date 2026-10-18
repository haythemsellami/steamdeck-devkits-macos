"""Artifact store (spec sections 3 and 8).

Every captured payload (logs, screenshots, traces…) lands under
``artifacts/<device>/<art_ulid>/`` with the payload plus a ``meta.json``
sidecar carrying the :class:`Artifact` record. Registration is atomic-ish:
any failure removes the partial artifact directory.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import ulid

from steamos_devkit.config import StateStore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import Artifact

EXCERPT_MAX_BYTES = 65_536  # 64 KiB (spec section 11)
EXCERPT_MAX_LINES = 2_000

_UNSAFE_DIR_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _device_dir_name(device: str) -> str:
    safe = _UNSAFE_DIR_CHARS.sub("_", device).strip("._")
    return safe or "unknown"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_tree(root: Path) -> tuple[str, int]:
    """Digest a directory as a sorted manifest of relpath+file digests."""
    digest = hashlib.sha256()
    total = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        file_digest = _hash_file(path)
        digest.update(f"{rel}\x00{file_digest}\x00".encode())
        total += path.stat().st_size
    return digest.hexdigest(), total


def register_artifact(
    state: StateStore,
    *,
    job_id: str,
    device: str,
    kind: str,
    source: Path,
    remote_source: str,
    media_type: str | None = None,
) -> Artifact:
    """Move ``source`` (file or directory) into the store and record it."""
    if not source.exists():
        raise DevkitError(ErrorCode.VALIDATION, f"artifact source missing: {source}")
    artifact_id = f"art_{ulid.new()}"
    dest_dir = state.artifacts_dir / _device_dir_name(device) / artifact_id
    dest_dir.mkdir(parents=True, exist_ok=False)
    try:
        if source.is_dir():
            payload = dest_dir / "payload"
            shutil.move(str(source), str(payload))
            digest, total = _hash_tree(payload)
        else:
            payload = dest_dir / source.name
            shutil.move(str(source), str(payload))
            digest, total = _hash_file(payload), payload.stat().st_size
        artifact = Artifact.model_validate(
            {
                "id": artifact_id,
                "job_id": job_id,
                "device": device,
                "kind": kind,
                "path": payload,
                "bytes": total,
                "sha256": digest,
                "media_type": media_type,
                "remote_source": remote_source,
                "created_at": datetime.now(UTC),
            }
        )
        (dest_dir / "meta.json").write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
    except Exception:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    return artifact


def list_artifacts(
    state: StateStore, *, device: str | None = None, kind: str | None = None
) -> list[Artifact]:
    """All registered artifacts, oldest first, optionally filtered."""
    artifacts: list[Artifact] = []
    for meta in sorted(state.artifacts_dir.glob("*/art_*/meta.json")):
        try:
            artifact = Artifact.model_validate_json(meta.read_text(encoding="utf-8"))
        except Exception as e:
            raise DevkitError(
                ErrorCode.STATE_CORRUPT, f"malformed artifact meta {meta}: {e}"
            ) from e
        if device is not None and artifact.device != device:
            continue
        if kind is not None and artifact.kind != kind:
            continue
        artifacts.append(artifact)
    artifacts.sort(key=lambda a: a.created_at)
    return artifacts


def get_artifact(state: StateStore, artifact_id: str) -> Artifact:
    for artifact in list_artifacts(state):
        if artifact.id == artifact_id:
            return artifact
    raise DevkitError(ErrorCode.ARTIFACT_NOT_FOUND, f"no such artifact: {artifact_id}")


def read_artifact_excerpt(
    state: StateStore,
    artifact_id: str,
    *,
    tail_lines: int = 200,
    max_bytes: int = EXCERPT_MAX_BYTES,
) -> str:
    """Bounded tail of a file artifact (spec section 11: never flood context)."""
    artifact = get_artifact(state, artifact_id)
    path = artifact.path
    if not path.is_file():
        raise DevkitError(
            ErrorCode.VALIDATION, f"artifact {artifact_id} is a directory; retrieve files instead"
        )
    tail_lines = min(tail_lines, EXCERPT_MAX_LINES)
    max_bytes = min(max_bytes, EXCERPT_MAX_BYTES)
    size = path.stat().st_size
    with open(path, "rb") as fh:
        if size > max_bytes:
            fh.seek(-max_bytes, 2)
        data = fh.read()
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > tail_lines:
        lines = lines[-tail_lines:]
    return "\n".join(lines)
