"""Branding, protocol constants, ports, regexes, and timeouts.

Values here are frozen by docs/technical-spec.md sections 1, 3, 4 and 7.5;
changing one requires updating the spec in the same commit. All user-facing
strings route through this module (spec section 1).
"""

from typing import Final, Literal

# --- Branding (spec section 1) ---------------------------------------------
APP_NAME: Final = "steamos-devkit"  # appdirs app name; state dir leaf
APP_DISPLAY_NAME: Final = "SteamOS Devkit"  # GUI display name
PACKAGE_NAME: Final = "steamos_devkit"  # Python package
DIST_NAME: Final = "steamos-devkit-mac"  # Python distribution
CLI_COMMAND: Final = "devkit"
MCP_COMMAND: Final = "devkit-mcp"
# Placeholder until a controlled domain is available (spec section 1).
GUI_BUNDLE_ID: Final = "dev.steamosdevkit.mac"

# --- State layout (spec section 3) ------------------------------------------
STATE_DIR_ENV_VAR: Final = "DEVKIT_STATE_DIR"

# --- Host dependency overrides (spec section 12) -----------------------------
RSYNC_ENV_VAR: Final = "DEVKIT_RSYNC"
SSH_ENV_VAR: Final = "DEVKIT_SSH"
# Set by the MCP server (M4) so adapters can tell mcp_stdio from cli.
CONTEXT_ENV_VAR: Final = "DEVKIT_CONTEXT"
SETTINGS_FILENAME: Final = "settings.json"
DEVICES_FILENAME: Final = "devices.json"
LAST_DEVICE_FILENAME: Final = "last_device"
KEYS_DIRNAME: Final = "keys"
KEY_FILENAME: Final = "devkit_rsa"
PUBKEY_FILENAME: Final = "devkit_rsa.pub"
TITLES_DIRNAME: Final = "titles"
JOBS_DIRNAME: Final = "jobs"
ARTIFACTS_DIRNAME: Final = "artifacts"
LOGS_DIRNAME: Final = "logs"

# Legacy upstream locations read (never deleted) by migration (spec section 3).
LEGACY_UPSTREAM_DIRNAME: Final = "steamos-devkit"  # under the platform config dir
LEGACY_GUI_PICKLE: Final = "~/.devkit-client-gui/settings.pickle"
LEGACY_PICKLE_ADDED_BY_IP_KEY: Final = "Devkit.AddedByIP"
LEGACY_PICKLE_PREFERRED_DEVICE_KEY: Final = "DevkitsWindow.preferred_devkit_name"

# --- Discovery (spec section 4) ---------------------------------------------
MDNS_SERVICE_TYPE: Final = "_steamos-devkit._tcp.local."
MDNS_TXT_VERSION: Final = "1"  # TXT txtvers; on the wire it is b"1"
MDNS_PORT: Final = 5353  # UDP

# --- Device HTTP service (spec section 4) -----------------------------------
DEVICE_HTTP_PORT_DEFAULT: Final = 32000
DEVICE_HTTP_PORT_FORWARDED: Final = 32001  # port-forwarded kits
DEVICE_HTTP_PROPERTIES_PATH: Final = "/properties.json"
DEVICE_HTTP_LOGIN_NAME_PATH: Final = "/login-name"  # legacy
DEVICE_HTTP_REGISTER_PATH: Final = "/register"
# urllib's default timeout is far too long for LAN hosts (upstream :129-131).
LAN_REQUEST_TIMEOUT_S: Final = 5

# --- SSH (spec section 4) ----------------------------------------------------
SSH_PORT: Final = 22
SSH_USER_DECK: Final = "deck"
SSH_USER_FRAME: Final = "steamos"

# --- Other device ports (spec section 4) -------------------------------------
CEF_DEBUG_PROBE_PORT: Final = 8080
CEF_DEBUG_CONSOLE_PORT: Final = 8081
GDBSERVER_PORT: Final = 2345

# --- Title identifiers (spec section 4) --------------------------------------
GAMEID_REGEX: Final = r"^[A-Za-z_][A-Za-z0-9_.]+$"
# Reserved Valve-only sideload names; rejected for deploy with UNSUPPORTED.
RESERVED_GAMEIDS: Final = frozenset({"steam", "steamdeckard", "steamvr", "steamvrdeckard"})

# --- Device-side title layout (spec section 4) -------------------------------
DEVICE_TITLE_ROOT: Final = "~/devkit-game"
ARGV_SIDECAR_SUFFIX: Final = "-argv.json"
ENV_SIDECAR_SUFFIX: Final = "-env.json"
SETTINGS_SIDECAR_SUFFIX: Final = "-settings.json"


def device_title_dir(gameid: str) -> str:
    """Remote directory an uploaded title lives in."""
    return f"{DEVICE_TITLE_ROOT}/{gameid}"


def argv_sidecar(gameid: str) -> str:
    """Remote path of the title's argv sidecar (index 0 is the start command)."""
    return f"{DEVICE_TITLE_ROOT}/{gameid}{ARGV_SIDECAR_SUFFIX}"


def env_sidecar(gameid: str) -> str:
    """Remote path of the title's environment sidecar."""
    return f"{DEVICE_TITLE_ROOT}/{gameid}{ENV_SIDECAR_SUFFIX}"


def settings_sidecar(gameid: str) -> str:
    """Remote path of the title's launch-settings sidecar."""
    return f"{DEVICE_TITLE_ROOT}/{gameid}{SETTINGS_SIDECAR_SUFFIX}"


# --- Compat-tool aliases (spec section 4, upstream :118-126) -----------------
COMPAT_TOOL_ALIASES: Final = frozenset(
    {
        "proton-stable",
        "proton-experimental",
        "SteamLinuxRuntime",  # scout
        "SteamLinuxRuntime_sniper",
        "lepton",
        "SteamLinuxRuntime_4-arm64",
        "SteamLinuxRuntime_4",
    }
)
CompatToolAlias = Literal[
    "proton-stable",
    "proton-experimental",
    "SteamLinuxRuntime",
    "SteamLinuxRuntime_sniper",
    "lepton",
    "SteamLinuxRuntime_4-arm64",
    "SteamLinuxRuntime_4",
]

# --- Screenshot modes (spec section 4; gamescope >= 3.13) --------------------
SCREENSHOT_MODE_BASEPLANE: Final = 1
SCREENSHOT_MODE_ALL_REAL_LAYERS: Final = 2
SCREENSHOT_MODE_FULL_COMPOSITION: Final = 3
SCREENSHOT_MODE_SCREEN_BUFFER: Final = 4
SCREENSHOT_MODES: Final = frozenset(
    {
        SCREENSHOT_MODE_BASEPLANE,
        SCREENSHOT_MODE_ALL_REAL_LAYERS,
        SCREENSHOT_MODE_FULL_COMPOSITION,
        SCREENSHOT_MODE_SCREEN_BUFFER,
    }
)
ScreenshotMode = Literal[1, 2, 3, 4]

# --- Default timeouts in seconds (spec section 7.5) ---------------------------
TIMEOUT_DISCOVER_S: Final = 5
TIMEOUT_PAIR_S: Final = 120  # on-device approval may be slow
TIMEOUT_STATUS_S: Final = 15
TIMEOUT_DEPLOY_S: Final = 3600
TIMEOUT_FETCH_LOGS_S: Final = 600
TIMEOUT_SCREENSHOT_S: Final = 60
TIMEOUT_GPU_TRACE_S: Final = 300
TIMEOUT_RGP_S: Final = 180
TIMEOUT_CONTROLLER_CONFIG_S: Final = 60
