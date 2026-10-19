"""``devkit mcp`` — run the MCP server over stdio (spec sections 10-11).

The process hosts the one long-lived DevkitCore; this is the real no-wait /
cancel context (spec appendix A, wave-2). The ``devkit-mcp`` console script
(entry point ``run``) is the same server without the typer wrapper.
"""

from __future__ import annotations

import os

import typer

from steamos_devkit import constants
from steamos_devkit.cli.common import get_options


def _mcp(ctx: typer.Context) -> None:
    """Run the stdio MCP server (blocking) for GUI/desktop MCP hosts."""
    os.environ.setdefault(constants.CONTEXT_ENV_VAR, "mcp_stdio")
    from steamos_devkit.mcp_server import run_stdio

    run_stdio(state_dir=get_options(ctx).state_dir)


def run() -> None:
    """``devkit-mcp`` console script: the stdio server with default options."""
    os.environ.setdefault(constants.CONTEXT_ENV_VAR, "mcp_stdio")
    from steamos_devkit.mcp_server import run_stdio

    # state_dir=None: resolve_state_dir honors DEVKIT_STATE_DIR itself.
    run_stdio(state_dir=None)


def register(app: typer.Typer) -> None:
    app.command(name="mcp")(_mcp)
