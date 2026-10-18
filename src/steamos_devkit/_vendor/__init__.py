"""Vendored snapshot of Valve's steamos-devkit client core (MIT).

Read-only in normal work: contents are managed by ``scripts/sync-upstream.sh``
plus the patch series in ``patches/`` (see docs/decisions.md D1). The pinned
upstream commit is recorded in ``UPSTREAM_COMMIT``. ``devkit-utils/`` is the
device-side protocol surface and is used verbatim.

Upstream's modules use absolute intra-package imports (``import
devkit_client.captured_popen``), so the vendored core must be loaded under its
original top-level name via :func:`load_devkit_client`; a plain
``import steamos_devkit._vendor.devkit_client`` does not work.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_VENDOR_DIR = Path(__file__).resolve().parent


def load_devkit_client() -> ModuleType:
    """Load the vendored upstream core as the top-level ``devkit_client`` package.

    Idempotent: returns the already-loaded module on repeat calls. The module
    is also registered under ``steamos_devkit._vendor.devkit_client`` so both
    names resolve to a single instance.
    """
    existing = sys.modules.get("devkit_client")
    if existing is not None:
        return existing
    package_dir = _VENDOR_DIR / "devkit_client"
    spec = importlib.util.spec_from_file_location(
        "devkit_client",
        package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load vendored devkit_client from {package_dir}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["devkit_client"] = module
    sys.modules[__name__ + ".devkit_client"] = module
    spec.loader.exec_module(module)
    return module


def upstream_commit() -> str:
    """The pinned upstream sha this snapshot was taken from."""
    return (_VENDOR_DIR / "UPSTREAM_COMMIT").read_text(encoding="utf-8").strip()
