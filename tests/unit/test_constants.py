"""Unit tests for steamos_devkit.constants (spec sections 1, 3, 4, 7.5)."""

import re

from steamos_devkit import constants as c


def test_device_title_paths() -> None:
    assert c.device_title_dir("my.game") == "~/devkit-game/my.game"
    assert c.argv_sidecar("my.game") == "~/devkit-game/my.game-argv.json"
    assert c.env_sidecar("my.game") == "~/devkit-game/my.game-env.json"
    assert c.settings_sidecar("my.game") == "~/devkit-game/my.game-settings.json"


def test_protocol_constants_frozen_by_spec() -> None:
    assert c.MDNS_SERVICE_TYPE == "_steamos-devkit._tcp.local."
    assert c.MDNS_TXT_VERSION == "1"
    assert c.DEVICE_HTTP_PORT_DEFAULT == 32000
    assert c.DEVICE_HTTP_PORT_FORWARDED == 32001
    assert c.SSH_PORT == 22
    assert c.SSH_USER_DECK == "deck"
    assert c.SSH_USER_FRAME == "steamos"
    assert c.CEF_DEBUG_PROBE_PORT == 8080
    assert c.CEF_DEBUG_CONSOLE_PORT == 8081
    assert c.GDBSERVER_PORT == 2345


def test_gameid_regex_is_spec_verbatim() -> None:
    assert c.GAMEID_REGEX == r"^[A-Za-z_][A-Za-z0-9_.]+$"
    assert re.fullmatch(c.GAMEID_REGEX, "my.game_1")


def test_default_timeouts_match_spec_75() -> None:
    assert c.TIMEOUT_DISCOVER_S == 5
    assert c.TIMEOUT_PAIR_S == 120
    assert c.TIMEOUT_STATUS_S == 15
    assert c.TIMEOUT_DEPLOY_S == 3600
    assert c.TIMEOUT_FETCH_LOGS_S == 600
    assert c.TIMEOUT_SCREENSHOT_S == 60
    assert c.TIMEOUT_GPU_TRACE_S == 300
    assert c.TIMEOUT_RGP_S == 180
    assert c.TIMEOUT_CONTROLLER_CONFIG_S == 60


def test_branding_constants() -> None:
    assert c.APP_NAME == "steamos-devkit"
    assert c.DIST_NAME == "steamos-devkit-mac"
    assert c.CLI_COMMAND == "devkit"
    assert c.GUI_BUNDLE_ID == "dev.steamosdevkit.mac"


def test_screenshot_modes() -> None:
    assert c.SCREENSHOT_MODES == frozenset({1, 2, 3, 4})
