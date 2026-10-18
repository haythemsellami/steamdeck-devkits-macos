"""Vendored-core import surface: the snapshot must load and match the audit."""

import sys
from importlib import resources

from steamos_devkit._vendor import load_devkit_client, upstream_commit

AUDIT_PIN = "a00ceb7d91ea44a0c3e714a91a06417d6e5cdb33"


def test_upstream_commit_pin() -> None:
    assert upstream_commit() == AUDIT_PIN


def test_vendored_files_present() -> None:
    vendor = resources.files("steamos_devkit").joinpath("_vendor")
    for rel in (
        "devkit_client/__init__.py",
        "devkit_client/proxy.py",
        "devkit_client/captured_popen.py",
        "devkit_client/custom_terminal.py",
        "devkit-utils/steamos-get-status",
        "devkit-utils/steamos-prepare-upload",
        "devkit-utils/steamos-list-games",
        "devkit-utils/steam-client-create-shortcut",
        "devkit-utils/steam-devkit-rpc",
        "devkit-utils/steamos-delete",
        "devkit-utils/steamos-set-steam-client",
        "devkit-utils/steamos-dump-controller-config",
        "devkit-utils/steamos-set-password.sh",
        "devkit-utils/deckard-capture",
        "devkit-utils/devkit_utils/__init__.py",
        "devkit-utils/devkit_utils/resolve.py",
    ):
        assert vendor.joinpath(rel).is_file(), rel


def test_load_devkit_client() -> None:
    module = load_devkit_client()
    # protocol constants as audited (docs/audit-upstream.md section 3)
    assert module.DEFAULT_DEVKIT_SERVICE_HTTP == 32000
    assert module.STEAM_DEVKIT_TYPE == "_steamos-devkit._tcp.local."
    assert module.CURRENT_TXTVERS == b"1"
    # absolute intra-package imports resolved through the loader
    assert sys.modules["devkit_client"] is module
    assert "devkit_client.captured_popen" in sys.modules
    assert "devkit_client.custom_terminal" in sys.modules
    # idempotent
    assert load_devkit_client() is module
