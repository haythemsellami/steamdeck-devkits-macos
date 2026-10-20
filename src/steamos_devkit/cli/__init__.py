"""``devkit`` CLI (spec section 10).

The app is assembled here; each command module lives alongside and exposes
``register(app)``. Add new command modules in the marked section below —
one import + one register call per module, append-only, to keep concurrent
work conflict-free.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import typer

from steamos_devkit import constants
from steamos_devkit.cli import common
from steamos_devkit.cli.common import Options, exit_code_for, exit_code_for_error
from steamos_devkit.errors import DevkitError

logger = logging.getLogger(__name__)

# The callback's parsed Options, stashed so main()'s exception path can render
# the failure with the same JSON-mode decision the callback made (L11). Reset
# at every main() entry; None means the callback never ran.
_resolved_options: Options | None = None

app = typer.Typer(
    name=constants.CLI_COMMAND,
    help=f"{constants.APP_DISPLAY_NAME} for macOS — CLI over the shared DevkitCore.",
    add_completion=False,
    no_args_is_help=True,
)


@app.callback()
def _callback(
    ctx: typer.Context,
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON (automatic when stdout is not a TTY)."
    ),
    state_dir: Path | None = typer.Option(
        None,
        "--state-dir",
        help=f"State root override (also ${constants.STATE_DIR_ENV_VAR}).",
    ),
    verbose: bool = typer.Option(False, "--verbose", help="Debug logging on stderr."),
) -> None:
    json_mode = json_output or not common.stdout_is_tty()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        stream=sys.stderr,
        force=True,
    )
    ctx.obj = Options(json=json_mode, state_dir=state_dir, verbose=verbose)
    global _resolved_options
    _resolved_options = ctx.obj


# --- command registration: one import + register per module, append-only ---
# Append order is deliberate (not sorted); isort must leave this block alone.
# isort: off
from steamos_devkit.cli import meta  # noqa: E402
from steamos_devkit.cli import profiling  # noqa: E402
from steamos_devkit.cli import session  # noqa: E402
from steamos_devkit.cli import deploy  # noqa: E402
from steamos_devkit.cli import jobs  # noqa: E402
from steamos_devkit.cli import titles  # noqa: E402
from steamos_devkit.cli import device  # noqa: E402
from steamos_devkit.cli import diagnostics  # noqa: E402
from steamos_devkit.cli import mcp  # noqa: E402
# isort: on

meta.register(app)
profiling.register(app)
session.register(app)
titles.register(app)
deploy.register(app)
jobs.register(app)
device.register(app)
diagnostics.register(app)
mcp.register(app)
# --- end command registration ---

_JSON_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_JSON_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _argv_json_mode(argv: list[str]) -> bool:
    """Best-effort ``--json`` scan for errors raised before the callback ran.

    Mirrors the callback's flag with the value forms typer accepts for bool
    flags across versions: bare ``--json``, ``--json=true``/``--json=false``,
    and the space-separated ``--json false``.
    """
    requested = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--json":
            following = argv[i + 1].lower() if i + 1 < len(argv) else ""
            if following in _JSON_TRUE_VALUES or following in _JSON_FALSE_VALUES:
                requested = following in _JSON_TRUE_VALUES
                i += 1  # consume the value token too
            else:
                requested = True
        elif arg.startswith("--json="):
            requested = arg.partition("=")[2].lower() in _JSON_TRUE_VALUES
        i += 1
    return requested


def main() -> None:
    global _resolved_options
    _resolved_options = None  # fresh invocation; drop any stale callback state
    try:
        app()
    except DevkitError as err:
        # When the callback ran, its parsed Options are authoritative; the argv
        # scan is only a fallback for failures before the callback executed.
        json_mode = (
            _resolved_options.json
            if _resolved_options is not None
            else _argv_json_mode(sys.argv[1:]) or not common.stdout_is_tty()
        )
        if json_mode:
            from steamos_devkit.models import Result

            sys.stdout.write(Result.failure(err.code, err.message).model_dump_json() + "\n")
        else:
            print(str(err), file=sys.stderr)
        sys.exit(exit_code_for_error(err))


__all__ = ["app", "exit_code_for", "main"]

if __name__ == "__main__":
    main()
