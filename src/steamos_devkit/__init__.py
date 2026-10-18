"""SteamOS Devkit for macOS — shared core package.

Frontends (CLI, MCP server, GUI) contain no device logic; everything goes
through :class:`steamos_devkit.core.DevkitCore` and the typed models in
:mod:`steamos_devkit.models`.
"""

from steamos_devkit.version import __version__

__all__ = ["__version__"]
