"""One-shot host onboarding: `devkit setup` (spec section 10).

Runs everything a fresh install needs, in order, and reports per-step
outcomes: state layout, keypair, rsync 3.x (installing via Homebrew when
asked to), ssh, terminal detection, and — on macOS — a short mDNS probe that
triggers the Local Network permission prompt before the user needs it.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

from steamos_devkit import discovery, pairing
from steamos_devkit.core import DevkitCore
from steamos_devkit.errors import DevkitError, ErrorCode
from steamos_devkit.models import SetupReport, SetupStep

logger = logging.getLogger(__name__)

_BREW_INSTALL_TIMEOUT_S = 600
_PROBE_TIMEOUT_S = 2.5

_HOMEBREW_CANDIDATES = (Path("/opt/homebrew/bin/brew"), Path("/usr/local/bin/brew"))


def _step(steps: list[SetupStep], name: str, status: str, summary: str) -> None:
    steps.append(SetupStep.model_validate({"name": name, "status": status, "summary": summary}))


def _find_brew() -> Path | None:
    which = shutil.which("brew")
    if which:
        return Path(which)
    for candidate in _HOMEBREW_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def _install_rsync(brew: Path) -> None:
    """`brew install rsync`; raises DevkitError on failure."""
    try:
        proc = subprocess.run(
            [str(brew), "install", "rsync"],
            capture_output=True,
            text=True,
            timeout=_BREW_INSTALL_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise DevkitError(
            ErrorCode.TIMEOUT,
            f"brew install rsync did not finish within {_BREW_INSTALL_TIMEOUT_S}s",
        ) from e
    if proc.returncode != 0:
        raise DevkitError(
            ErrorCode.DEPENDENCY_MISSING,
            "brew install rsync failed",
            details={"stderr_tail": proc.stderr[-500:]},
        )


def run_setup(
    core: DevkitCore, *, install_deps: bool = True, network_probe: bool = True
) -> SetupReport:
    """Run the onboarding steps; the report is honest about each one."""
    steps: list[SetupStep] = []

    # 1. state layout
    try:
        core.state.ensure_layout()
        _step(steps, "state-dir", "ok", f"state ready at {core.state.root}")
    except DevkitError as e:
        _step(steps, "state-dir", "error", e.message)

    # 2. keypair (so first pairing is instant and permissions are right)
    if core.state.key_path.exists():
        _step(steps, "keypair", "skipped", f"keypair already exists at {core.state.key_path}")
    else:
        try:
            pairing.ensure_key(core)
            _step(
                steps,
                "keypair",
                "ok",
                f"generated RSA-2048 keypair at {core.state.key_path} (mode 0400)",
            )
        except DevkitError as e:
            _step(steps, "keypair", "error", e.message)

    # 3. rsync 3.x (install via Homebrew when allowed)
    rsync = core.adapter.rsync()
    if rsync.ok:
        _step(
            steps,
            "rsync",
            "ok",
            f"{rsync.flavor} {rsync.version} (protocol {rsync.protocol_version}) at {rsync.path}",
        )
    elif not install_deps:
        _step(
            steps,
            "rsync",
            "error",
            f"rsync 3.x not found ({rsync.message}); install it with `brew install rsync`",
        )
    else:
        brew = _find_brew()
        if brew is None:
            _step(
                steps,
                "rsync",
                "error",
                "rsync 3.x not found and Homebrew is not installed; install Homebrew "
                "(https://brew.sh) then run `brew install rsync`, or re-run `devkit setup`",
            )
        else:
            try:
                _install_rsync(brew)
            except DevkitError as e:
                _step(steps, "rsync", "error", e.message)
            else:
                # adapter re-probes on every call, so this sees the fresh install
                rsync = core.adapter.rsync()
                if rsync.ok:
                    _step(steps, "rsync", "ok", f"installed rsync {rsync.version} via Homebrew")
                else:
                    _step(
                        steps,
                        "rsync",
                        "error",
                        f"brew install ran but rsync still not usable ({rsync.message}); "
                        "check your PATH or set DEVKIT_RSYNC",
                    )

    # 4. ssh
    try:
        ssh = core.adapter.ssh()
        _step(steps, "ssh", "ok", f"ssh at {ssh}")
    except DevkitError as e:
        _step(steps, "ssh", "error", e.message)

    # 5. terminal (informational — interactive shells only)
    terminal = core.adapter.discover_tools().terminal
    if terminal is None:
        _step(
            steps,
            "terminal",
            "warning",
            "no Terminal.app/iTerm2 found; `devkit shell` will be unavailable",
        )
    else:
        _step(steps, "terminal", "ok", f"interactive shells via {terminal}")

    # 6. Local Network probe (macOS): attempting mDNS is what registers/prompts
    # the responsible app for the TCC permission — better here than mid-pairing.
    if not network_probe:
        _step(steps, "local-network", "skipped", "probe disabled (--no-probe)")
    elif sys.platform != "darwin":
        _step(steps, "local-network", "skipped", "macOS-only check")
    else:
        try:
            found = discovery.discover(core, timeout_s=_PROBE_TIMEOUT_S)
        except DevkitError as e:
            if e.code == ErrorCode.PERMISSION_LOCAL_NETWORK:
                _step(
                    steps,
                    "local-network",
                    "error",
                    f"Local Network permission appears blocked: {e.message}",
                )
            else:
                _step(steps, "local-network", "warning", f"discovery probe failed: {e.message}")
        else:
            if found:
                _step(
                    steps,
                    "local-network",
                    "ok",
                    f"local network usable; discovered {len(found)} device(s)",
                )
            else:
                _step(
                    steps,
                    "local-network",
                    "warning",
                    "no devices found; if a Deck is on this LAN and stays undiscovered, check "
                    "System Settings > Privacy & Security > Local Network for this app",
                )

    return SetupReport(ok=all(step.status != "error" for step in steps), steps=steps)
