"""Host adapter selection (spec section 12)."""

import sys

from steamos_devkit.adapters.base import HostAdapter

__all__ = ["HostAdapter", "get_adapter"]


def get_adapter(platform: str | None = None) -> HostAdapter:
    """Return the adapter for the current (or given) platform."""
    name = platform if platform is not None else sys.platform
    if name == "darwin":
        from steamos_devkit.adapters.macos import MacOSAdapter

        return MacOSAdapter()
    if name == "win32":
        from steamos_devkit.adapters.windows import WindowsAdapter

        return WindowsAdapter()
    from steamos_devkit.adapters.linux import LinuxAdapter

    return LinuxAdapter()
