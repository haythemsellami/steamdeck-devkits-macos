#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#MIT License
#
#Copyright (c) 2017-2022 Valve Software inc., Collabora Ltd
#
#Permission is hereby granted, free of charge, to any person obtaining a copy
#of this software and associated documentation files (the "Software"), to deal
#in the Software without restriction, including without limitation the rights
#to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#copies of the Software, and to permit persons to whom the Software is
#furnished to do so, subject to the following conditions:
#
#The above copyright notice and this permission notice shall be included in all
#copies or substantial portions of the Software.
#
#THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#SOFTWARE.


import contextlib
from dataclasses import dataclass
from genericpath import exists
import glob
import sys
import os
import platform
import traceback
import enum
import getpass
from io import StringIO
import json
import logging
from pathlib import Path
import queue
import shlex
import shutil
import socket
import string
import subprocess
import time
import unicodedata
import urllib
import urllib.parse
import urllib.request
import urllib.error
import webbrowser
import tempfile
import pathlib
import re
import threading
import appdirs
import datetime
import collections

if platform.system() == 'Windows':
    import winreg

import paramiko
import devkit_client.captured_popen as captured_popen
import devkit_client.custom_terminal as custom_terminal
import signalslot

try:
    import devkit_client.version
    __version__ = devkit_client.version.__version__
except:
    __version__ = '<undefined>'

def is_running_under_wsl():
    if platform.system() != 'Linux':
        return False
    return os.path.exists('/proc/sys/fs/binfmt_misc/WSLInterop') or 'WSL_DISTRO_NAME' in os.environ

if is_running_under_wsl():
    os.environ.setdefault('SDL_VIDEODRIVER', 'x11')
    os.environ.setdefault('PYOPENGL_PLATFORM', 'linux')

# Reserved names for the sideload system
STEAM_CLIENT_GAMEIDS = ( 'steam', 'steamdeckard' )
STEAMVR_CLIENT_GAMEIDS = ( 'steamvr', 'steamvrdeckard' )
SIDELOAD_GAMEIDS = STEAM_CLIENT_GAMEIDS + STEAMVR_CLIENT_GAMEIDS

# Must match Steam client ESteamPlayDebug
class SteamPlayDebug(enum.IntEnum):
    Disabled = 0
    Start = 1
    Wait = 2

# Note: ID values get stored in settings - don't shuffle
class RuntimeOptions(enum.IntEnum):
    Unspecified = 0
    SteamPlay = 1
    SLR1 = 2
    SLR3 = 3
    Android = 4
    SLR4_arm64 = 5
    Proton_Experimental = 6
    SLR4 = 7

RUNTIME_DESCRIPTIONS = collections.OrderedDict([
    (RuntimeOptions.Unspecified, "Not specified"),
    (RuntimeOptions.SteamPlay, "Proton Stable"),
    (RuntimeOptions.Proton_Experimental, "Proton Experimental"),
    (RuntimeOptions.SLR1, "Steam Linux Runtime 1.0 (scout)"),
    (RuntimeOptions.SLR3, "Steam Linux Runtime 3.0 (sniper)"),
    (RuntimeOptions.Android, "Lepton"),
    (RuntimeOptions.SLR4_arm64, "Steam Linux Runtime 4.0 ARM64"),
    (RuntimeOptions.SLR4, "Steam Linux Runtime 4.0")
])

RUNTIME_ALIASES = {
    RuntimeOptions.SteamPlay: 'proton-stable',
    RuntimeOptions.SLR1: 'SteamLinuxRuntime',
    RuntimeOptions.SLR3: 'SteamLinuxRuntime_sniper',
    RuntimeOptions.Android: 'lepton',
    RuntimeOptions.Proton_Experimental: 'proton-experimental',
    RuntimeOptions.SLR4_arm64: 'SteamLinuxRuntime_4-arm64',
    RuntimeOptions.SLR4: 'SteamLinuxRuntime_4',
}

STEAMOS_DEVKIT_SERVICE = "_steamos-devkit._tcp"
LOCAL_DOMAIN = "local"
DEFAULT_DEVKIT_SERVICE_HTTP = 32000

REMOTE_STEAM_LOGS_PATH = '/home/{login}/.local/share/Steam/logs'

STEAM_DEVKIT_TYPE = STEAMOS_DEVKIT_SERVICE + '.' + LOCAL_DOMAIN + '.'
ZEROCONF_TIMEOUT = 10000

# Format version number for TXT record. Increment if we make an
# incompatible change that would cause current clients to parse it
# incorrectly (hopefully we will never need this). See
# https://tools.ietf.org/html/rfc6763#section-6.7
CURRENT_TXTVERS = b'1'

# A translation table that case-folds ASCII, while leaving non-ASCII
# unaltered
ASCII_LOWERCASE = str.maketrans(string.ascii_uppercase,
                                string.ascii_lowercase)

# Default urllib timeout is way too long for LAN hosts
REQUEST_TIMEOUT = 5

# Root of the installation - do not use getcwd!
if getattr(sys, 'frozen', False):
    ROOT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    ROOT_DIR = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__), '..'
        )
    )

REBOOT_NOW = '/usr/bin/steamos-polkit-helpers/steamos-reboot-now'

DEVKIT_TOOL_FOLDER = 'devkit-game'

logger = logging.getLogger(__name__)

SUBPROCESS_CREATION_FLAGS = 0
if platform.system() == 'Windows':
    SUBPROCESS_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW


ENV_VAR_PATTERN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

def parse_env_from_command(command_str):
    """Parse leading KEY=VALUE environment variable assignments from a command string.

    Returns (env_dict, command_string).
    Example:
        >>> parse_env_from_command('PROTON_LOG=1 VKD3D_CONFIG=no_staggered_submit imgui.exe -w 1280')
        ({'PROTON_LOG': '1', 'VKD3D_CONFIG': 'no_staggered_submit'}, 'imgui.exe -w 1280')
    """
    env = {}
    if not command_str:
        return env, ''
    lexer = shlex.shlex(command_str, posix=True)
    lexer.whitespace_split = True
    pos = 0
    for token in lexer:
        if ENV_VAR_PATTERN.match(token):
            key, value = token.split('=', 1)
            env[key] = value
            pos = lexer.instream.tell()
        else:
            break
    return env, command_str[pos:].lstrip()


g_remote_debuggers = None
g_external_tools = None
g_disable_rsync_compress = False
g_lock = threading.Lock()

g_linux_captured_popen_factory = captured_popen.LinuxCapturedPopenFactory()
g_win_custom_terminal = custom_terminal.WinCustomTerminal()

g_zeroconf_listener = None

# A signal emitted to update status for long running async operations, for interested parties
g_signal_status = signalslot.Signal(args=['status'])

# This burned me twice now .. https://twitter.com/TTimo/status/1582509449838989313
from os import getenv as os_getenv
def getenv_monkey(key, default=None):
    if key.upper() in ('USER', 'USERNAME'):
        raise Exception('Never rely on USERNAME out of the environment! Some Windows system do not have it set (?!?). Use get_username instead.')
    return os_getenv(key, default)
os.getenv = getenv_monkey

def get_username():
    if platform.system() != 'Windows':
        return os_getenv('USER')
    USERNAME = os_getenv('USERNAME')
    if USERNAME is None or len(USERNAME) == 0:
        try:
            # I don't trust this module, especially under cx_Freeze, so only do a late import
            logger.warning('USERNAME environment variable is not set, trying from win32api - please fix!!')
            import win32api
            USERNAME = win32api.GetUserName()
            logger.warning(f'Obtained USERNAME from win32api: {USERNAME}')
        except Exception as e:
            log_exception(e)
            raise Exception('Unable to obtain USERNAME on this system. We should stop now.')
    return USERNAME

def _subprocess_encoding():
    """Return the correct encoding for capturing text output from subprocesses."""
    if platform.system() == 'Windows':
        import ctypes
        return f'cp{ctypes.windll.kernel32.GetOEMCP()}'
    return 'utf-8'

def windows_get_domain_and_name():
    return subprocess.check_output(
        'whoami',
        text=True,
        encoding=_subprocess_encoding(),
        errors='replace',
        creationflags=SUBPROCESS_CREATION_FLAGS,
    ).strip('\n')


class ResolveMachineArgs:
    def __init__(self, devkit, steamvr_path=None):
        self.machine, self.machine_name_type = devkit.machine_command_args
        self.login = None
        self.http_port = devkit.http_port
        self.is_deckard = devkit.is_deckard
        self.steamvr_path = steamvr_path


@dataclass
class RemoteDebugger:
    directory: str
    year: str

def _locate_vswhere():
    assert sys.platform == 'win32'
    if 'PROGRAMFILES(X86)' in os.environ:
        # 64-bit
        program_files_path = Path(os.environ['PROGRAMFILES(X86)'])
    elif 'PROGRAMFILES' in os.environ:
        # 32-bit
        program_files_path = Path(os.environ['PROGRAMFILES'])
    else:
        logger.warning('Unable to locate the program files directory')
        return None
    vswhere_path = program_files_path / 'Microsoft Visual Studio' / 'Installer' / 'vswhere.exe'
    if not vswhere_path.is_file():
        logger.info('Unable to locate vswhere.exe, probably Visual Studio '
                    'has not been installed')
        return None
    return vswhere_path

def _locate_remote_debugger(vswhere_path, version):
    # Starting from Visual Studio 2017, the official way to locate a VS
    # installation path is by using the provided "vswhere" executable.
    assert sys.platform == 'win32'
    if not vswhere_path:
        return
    # E.g. if version is 15, we will end up with "[15,16)",
    # meaning that we start looking from the version 15 up to
    # version 16, excluded.
    version_range = f"[{version},{version+1})"
    vs_install_path = subprocess.check_output(
        [str(vswhere_path), '-latest', '-version', version_range, '-prerelease', '-property', 'installationPath'],
        text=True,
        encoding='utf-8',
        errors='replace',
        creationflags=SUBPROCESS_CREATION_FLAGS,
    ).strip('\n')
    if not vs_install_path:
        logger.info(f'Unable to locate the installation path of Visual Studio {version}')
        return None
    remote_debugger = Path(vs_install_path) / 'Common7' / 'IDE' / 'Remote Debugger'
    return str(remote_debugger) if remote_debugger.is_dir() else None

def get_remote_debuggers():
    assert sys.platform == 'win32'
    global g_remote_debuggers
    if g_remote_debuggers is not None:
        return g_remote_debuggers
    ret = []
    vswhere_path = _locate_vswhere()
    # Look for Visual Studio 15 (2017) and 16 (2019)
    for version, year in [[15, 2017], [16, 2019], [17, 2022]]:
        debugger_dir = _locate_remote_debugger(vswhere_path, version)
        if debugger_dir:
            ret.append(RemoteDebugger(debugger_dir, year))

    g_remote_debuggers = ret
    return g_remote_debuggers


def _locate_cygwin_tool(name):
    assert sys.platform == 'win32'
    if getattr(sys, 'frozen', False):
        # Scripts that were frozen into a standalone executable via cx_freeze are expected to package the binaries
        tools_path = os.path.dirname(sys.executable)
        ret = os.path.join(tools_path, 'cygroot/bin', name)
        assert os.path.exists(ret)
        return ret
    # Running from source:
    # We normally package the cygwin tools when preparing builds for distribution.
    # Windows ssh.exe isn't a compatible transport for rsync for instance, we need everything from the cygwin tree.
    # Locate cygwin on the system and use it directly.
    CYGWIN_KEY = "SOFTWARE\\Cygwin\\setup"
    for hk in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hk, CYGWIN_KEY)
        except FileNotFoundError:
            logger.error('Cygwin not found in registry')
            continue
        root = winreg.QueryValueEx(key, "rootdir")[0]
        ret = os.path.join(root, 'bin', name)
        if os.path.exists(ret):
            return ret
    raise Exception('Required external tool not found: {!r}'.format(name))

def locate_cygwin_tools():
    '''Returns a tuple of paths (cygpath, ssh, rsync, ssh_know_hosts)'''
    global g_external_tools
    if g_external_tools is not None:
        return g_external_tools
    if sys.platform != 'win32':
        g_external_tools = (None, 'ssh', 'rsync', None)
        for tool in g_external_tools:
            if tool is not None:
                if shutil.which(tool) is None:
                    raise Exception(f'{tool} not found - please install.')
        return g_external_tools

    cygpath = _locate_cygwin_tool('cygpath.exe')
    ssh = _locate_cygwin_tool('ssh.exe')
    rsync = _locate_cygwin_tool('rsync.exe')

    USERPROFILE = os.getenv('USERPROFILE')
    if USERPROFILE is None:
        # said system has USERPROFILE though .. smh
        import pprint
        logger.error(pprint.pformat(dict(os.environ)))
        raise Exception(f'Unexpected: no USERPROFILE in your environment variables - please fix.')

    # cygwin's ssh.exe (which we wrap and distribute in the frozen build) seems to ignore the HOME environment
    # and expands ~/.ssh/known_hosts (for instance) relative to the location of the .exe,
    # which ends up putting it in some really odd locations, and sometimes even paths that it does not have write permissions to
    # for us this is only a problem with ~/.ssh/known_hosts although it could bleed to other configuration files
    # to avoid problems, we explicitly set UserKnownHostsFile

    dotssh_path = str(pathlib.PureWindowsPath(USERPROFILE, '.ssh'))
    if not os.path.isdir(dotssh_path):
        logger.info(f'creating "~/.ssh" path (based on USERPROFILE): {dotssh_path!r}')
        try:
            os.makedirs(dotssh_path)
        except Exception as e:
            log_exception(e)
            raise Exception(f'Unexpected: cannot create {dotssh_path!r} based on your USERPROFILE environment variable -- please fix.')

    ssh_known_hosts = str(pathlib.PureWindowsPath(USERPROFILE, '.ssh', 'known_hosts'))
    logger.info(f'Forcing ssh known_hosts path to {ssh_known_hosts!r}')
    g_external_tools = (cygpath, ssh, rsync, ssh_known_hosts)
    return g_external_tools


def parse_settings_arguments(jsonobject, args):
    if getattr(args, 'clear_settings', False):
        jsonobject['clear_settings'] = True

    for settings_file in getattr(args, 'settings_file', []):
        logger.info("settings-file given, reading %s", settings_file)

        try:
            with open(settings_file, "r") as f:
                jsonobject['settings'].update(json.load(f))
        except (IOError, FileNotFoundError):
            raise ValueError("Unable to read json settings file %s",
                             settings_file)

    for value in getattr(args, 'set_json', []):
        logger.info("set-json used, adding json string to settings")
        pair = value.split('=', 1)
        if len(pair) != 2:
            raise ValueError("set-json arguments require name=value,"
                             " %s is invalid", value)

        jsonobject['settings'][pair[0]] = json.loads(pair[1])

    for value in getattr(args, 'set_keyval', []):
        logger.info("set used, adding given settings")
        pair = value.split('=', 1)
        if len(pair) != 2:
            raise ValueError("set arguments must be name=value, %s is invalid",
                             value)

        jsonobject['settings'][pair[0]] = pair[1]

    deps = getattr(args, 'deps')
    if deps:
        logger.info("deps used, adding given dependencies")
        jsonobject['settings']['deps'] = deps


def log_exception(e):
    # https://stackoverflow.com/a/11415140/1043757
    lines = [l.strip('\n') for l in traceback.format_exception(type(e), e, e.__traceback__)]
    for l in lines:
        logger.error(l)


# NOTE: On Windows, when running under the VSCode debugger the spawned processes still exit when the devkit tool exits.
# I suspect this is caused by the debug setup intercepting the Popen and preventing the reparenting.
def spawn_detached(cmd, cwd=None):
    if platform.system() == 'Windows':
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        kwargs = { 'creationflags': creationflags }
    else:
        kwargs = { 'start_new_session': True }

    logger.debug(f'Spawning detached process: {" ".join(cmd)}')
    subprocess.Popen(
        args = cmd,
        cwd = cwd,
        stdin = subprocess.DEVNULL,
        stdout = subprocess.DEVNULL,
        stderr = subprocess.DEVNULL,
        **kwargs) # pyright: ignore[reportCallIssue]


def stream_byte_copy_thread(source, dest):
    buf = source.read()

    while buf:
        try:
            dest.write(buf)
        except TypeError:
            dest.write(str(buf))
        buf = source.read()


def stream_copy_logger(source, dest):
    buf = source.readline()

    while buf:
        dest.info(buf)
        buf = source.readline()


class MachineNotFoundError(Exception):
    def __init__(self, message, *, name=None):
        super().__init__(message)
        self.name = name


class MachineNameType(enum.Enum):
    GUESS = 0
    ADDRESS = 1
    SERVICE_NAME = 2

    def __repr__(self):
        return '<{}.{}>'.format(self.__class__.__name__, self.name)


class ServiceListener:
    def __init__(self, zc):
        self.zc = zc
        self.devkits = {}
        self.devkit_events = queue.Queue()
        # singleton
        global g_zeroconf_listener
        assert g_zeroconf_listener is None
        g_zeroconf_listener = self

    def remove_service(self, _, type, name):
        # Called from the zeroconf thread
        assert type == STEAM_DEVKIT_TYPE, (name, type)
        assert name.endswith('.' + type), (name, type)
        service_name = name[:-len('.' + type)]
        logger.info("Service %r removed", service_name)
        if not service_name in self.devkits:
            logger.warning("Service %r not found", service_name)
            return
        del self.devkits[service_name]
        self.devkit_events.put(('del', service_name))

    def add_service(self, _, type, name):
        # Called from the zeroconf thread
        assert type == STEAM_DEVKIT_TYPE, (name, type)
        assert name.endswith('.' + type), (name, type)
        service_name = name[:-len('.' + type)]
        logger.info("Service %r found", service_name)
        get_service_delay = time.perf_counter_ns()
        info = self.zc.get_service_info(type, name, timeout=ZEROCONF_TIMEOUT)
        get_service_delay = time.perf_counter_ns() - get_service_delay
        logger.debug(f'zeroconf.get_service_info: {int(get_service_delay/1000)}ms')
        if info:
            if len(info.addresses) > 0:
                logger.info(
                    "  Address is %s:%d",
                    socket.inet_ntoa(info.addresses[0]), info.port)
            else:
                logger.info('No IPv4 address published for %s', service_name)
            logger.debug(
                "  Weight is %d, Priority is %d",
                info.weight, info.priority)
            logger.debug("  Server is %s", info.server)
            prop = info.properties
            if prop:
                logger.debug("  Properties are:")
                for key, value in prop.items():
                    logger.debug("    %s: %s", key, value)
                if b'txtvers' in prop and prop[b'txtvers'] != CURRENT_TXTVERS:
                    logger.warning(
                        'Incompatible txtvers %r, ignoring %s',
                        prop[b'txtvers'], service_name)
                    return
            if service_name in self.devkits:
                logger.info(f'updating {service_name}')
                self.devkits[service_name] = info
                self.devkit_events.put(('update', service_name))
            else:
                logger.info(f'adding {service_name}')
                self.devkits[service_name] = info
                self.devkit_events.put(('add', service_name))

    def address_for_service(self, name):
        if name not in self.devkits:
            return None
        info = self.devkits[name]
        if (len(info.addresses) <= 0):
            logger.warning(f'DNS-SD instance {name} did not publish an IP address yet. {info!r}')
            return None
        return socket.inet_ntoa(info.addresses[0])

    def port_for_service(self, name):
        if name not in self.devkits:
            return DEFAULT_DEVKIT_SERVICE_HTTP
        info = self.devkits[name]
        return info.port

    def update_service(self, *args):
        self.add_service(*args)

    # unrelated to the rest of ServiceListener, we just fill in service info into a Machine instance
    # either use cached data, or query as needed, but rely on the same zeroconf context as the listening service for consistency
    def update_service_info(self, machine):
        instance_name = machine.name + '.' + STEAM_DEVKIT_TYPE
        info = None
        if machine.name in self.devkits:
            info = self.devkits[machine.name]
            logger.debug(f'zeroconf service info for {machine.name} is in cache.')
        else:
            get_service_delay = time.perf_counter()
            logger.debug(f'query zeroconf for DNS-SD instance {instance_name}')
            info = self.zc.get_service_info(STEAM_DEVKIT_TYPE, instance_name, timeout=ZEROCONF_TIMEOUT)
            get_service_delay = time.perf_counter() - get_service_delay
            logger.debug(f'zeroconf.get_service_info: {get_service_delay:.1f}s')

        if info is None:
            raise MachineNotFoundError(
                f'mDNS devkit service {machine.name} not found',
                name=machine.name
            )

        logger.debug("Machine found")
        if len(info.addresses) <= 0:
            raise MachineNotFoundError(
                f'DNS-SD instance {machine.name} did not publish an IP address yet. {info!r}',
                name=machine.name
            )
        machine.address = socket.inet_ntoa(info.addresses[0])
        logger.info("Machine IP: %s", machine.address)

        if info.properties:
            settings = info.properties.get(b'settings')
            if settings is not None:
                machine.settings = json.loads(settings)

            if machine.login is None:
                login = info.properties.get(b'login')
                if login is not None:
                    machine.login = login.decode('utf-8')
                    logger.debug("Machine login: %s", machine.login)

                devkit1 = info.properties.get(b'devkit1')
                if devkit1 is not None:
                    machine.devkit1 = shlex.split(devkit1.decode('utf-8'))
                    logger.debug("devkit-1 entry point: %s", machine.devkit1)

        # mDNS is ASCII-case-insensitive, like normal DNS, and defines
        # UTF-8 to be fully composed.
        machine.normalized_name = unicodedata.normalize(
            'NFC', machine.name.translate(ASCII_LOWERCASE).rstrip('.'))


def get_public_key_comment():
    return ' devkit-client:{}@{}'.format(
        getpass.getuser(),
        socket.gethostname(),
    )


def get_public_key(key):
    public_key = (
        'ssh-rsa ' + key.get_base64() + get_public_key_comment() + '\n')
    return public_key


def _fix_key_permissions(key_path, pubkey_path):
    if platform.system() == 'Linux':
        # enforce/fix file permissions
        os.chmod(key_path, 0o400)
        os.chmod(pubkey_path, 0o400)
    else:
        # fix permissions for private keys the windows way, keep ssh happy
        # do not rely on get_username here, use the full domain\name of the current user - some systems fail if you just give username
        # also icacls.exe docs suggest this should work with the SID, but that will fail with "No mapping between account names and security IDs was done."
        # I really hate everything about this ...
        username = windows_get_domain_and_name()
        for cmd in (
            ['icacls.exe', key_path, '/Reset'],
            ['icacls.exe', key_path, '/Inheritance:r'],
            ['icacls.exe', key_path, '/Grant:r', f'{username}:(R)'],
            # for diagnostics
            ['icacls.exe', key_path],
        ):
            cp = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=SUBPROCESS_CREATION_FLAGS,
                text=True,
                encoding=_subprocess_encoding(),
                errors='replace'
            )
            if cp.returncode == 0:
                logger.debug(' '.join(cmd))
                logger.debug(cp.stdout.strip('\n'))
            else:
                logger.warning(' '.join(cmd))
                logger.warning(cp.stdout.strip('\n'))


def ensure_devkit_key():
    # this is called from threads and needs to be marshalled due to possible fs and permissions manipulations
    global g_lock
    with g_lock:
        key_folder = appdirs.user_config_dir('steamos-devkit')
        key_path = os.path.join(key_folder, 'devkit_rsa')
        pubkey_path = os.path.join(key_folder, 'devkit_rsa.pub')
        try:
            key = paramiko.PKey.from_path(key_path)
        except PermissionError as e:
            logger.warning(e)
            logger.warning('Fix permissions and try paramiko key load again.')
            _fix_key_permissions(key_path, pubkey_path)
            key = paramiko.PKey.from_path(key_path)
        except FileNotFoundError as e:
            # first time setup
            logger.warning(f'{key_path} not found - generating a new passwordless devkit key')
            os.makedirs(key_folder, exist_ok=True)
            # the key may not be writable, so make sure to delete first (on Windows espcially with our iacls.exe dance)
            for p in (key_path, pubkey_path):
                if os.path.exists(p):
                    os.unlink(p)
            key = paramiko.RSAKey.generate(2048)
            key.write_private_key_file(key_path)
            o = open(pubkey_path, 'w')
            o.write(get_public_key(key))
        _fix_key_permissions(key_path, pubkey_path)
        return (key, key_path, pubkey_path)


class DevkitClient(object):
    def __init__(self, quiet=True):
        self.keypath = os.path.join(
            appdirs.user_config_dir('steamos-devkit'),
            'devkit_rsa',
        )
        (self.cygpath, self.ssh, self.rsync, self.ssh_known_hosts) = locate_cygwin_tools()
        self.ssh_result = {}
        self.last_device_list_time = 0
        self.rsync_process = None
        self._cancel_requested = False

    def machine_readable_command_reader_thread(self, command_stdout):
        json_output = str(command_stdout.read(), 'utf-8')
        try:
            self.ssh_result = json.loads(json_output)
        except Exception as e:
            logger.error('output does not parse as json:')
            logger.error('%s', json_output)
            self.ssh_result = e

    def remote_shell_command(self, username, ipaddress):
        cmd = [
            # NOTE: this is the path to the ssh client, and may contain spaces (especially likely on windows)
            self.ssh
        ]
        cmd += [
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'IdentitiesOnly=yes',
            '-t',
            '-i', self.keypath,
            '{}@{}'.format(username, ipaddress),
        ]
        return cmd

    def ssh_command(
            self, username, ipaddress, command, stdindata,
            stream_output_to=None
        ):
        logger.debug('%s@%s: %s', username, ipaddress, command)
        ssh = paramiko.SSHClient()
        key, key_path, _ = ensure_devkit_key()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        logger.debug(f'Connecting to {username}@{ipaddress} with private key {key_path!r}')

        ssh.connect(
            ipaddress,
            username=username,
            pkey=key,
            timeout=REQUEST_TIMEOUT,
            look_for_keys=False
            )

        stdin, stdout, stderr = ssh.exec_command(command)
        chan = stdout.channel

        stderr_thread = threading.Thread(target=stream_copy_logger,
                               args=(stderr, logger))
        stderr_thread.start()
        if stream_output_to is not None:
            stdout_thread = threading.Thread(
                target=stream_byte_copy_thread,
                args=(stdout, stream_output_to))
        else:
            # stdout will be json data
            stdout_thread = threading.Thread(
                target=self.machine_readable_command_reader_thread,
                args=(stdout,))

        stdout_thread.start()

        # Send stdin data if there is any
        if stdindata is not None:
            logger.info("Writing stdin data to command input: %r", stdindata)
            stdin.write(stdindata)
            stdin.flush()
            stdin.channel.shutdown_write()
        stdin.close()

        exit_status = chan.recv_exit_status()

        stdout_thread.join()
        stderr_thread.join()

        if stream_output_to is not None:
            # output is not None, so just return since output went to it already
            return

        json_or_exception = self.ssh_result

        logger.info("exit status: %d", exit_status)

        ssh.close()

        if exit_status != 0:
            raise subprocess.CalledProcessError(exit_status, command)

        if isinstance(json_or_exception, Exception):
            raise json_or_exception
        else:
            # json_or_exception is stdout output
            return json_or_exception

    def on_cancel_transfer(self, **kwargs):
        logger.info('Cancelling by user request')
        self._cancel_requested = True
        if self.rsync_process:
            logger.info('Terminating rsync process')
            self.rsync_process.terminate()

    def rsync_transfer(
        self,
        localdir,
        user,
        ipaddress,
        remotedir,                  # when upload is False, can be a list of folders to download from the remote side
        delete_extraneous = False,  # delete extraneous files on the remote (clean upload)
        skip_newer_files = False,   # leave files with a newer modification time untouched, allowing local changes on the remote to persist
        verify_checksums = False,   # force checksum content verification
        upload = True,              # by default we are uploading to the remote (this is our most common use case)
        extra_cmdline = [],         # additional command line parms, typically for content filtering
    ):
        '''Folder transfers. Either direction, controlled by the upload parm.'''
        self._cancel_requested = False

        # Single source transfer
        if not os.path.isdir(localdir):
            raise Exception(f'Source directory does not exist: {localdir}')
        if sys.platform == 'win32':
            localdir = self.native_to_cygwin_path(localdir)

        if self.ssh_known_hosts:
            # we double shell escape here, which will be a no-op in most cases,
            # but in pathological setups (Windows with spaces in the path),
            # a single quote escaping is apparently not enough, presumably because the rsh gets funnelled through another shell:
            # https://github.com/PowerShell/Win32-OpenSSH/issues/1784
            # ^ if this bug gets fixed, will this regress?
            ssh_known_hosts = f'-o UserKnownHostsFile={shlex.quote(shlex.quote(self.ssh_known_hosts))}'
        else:
            ssh_known_hosts = ''

        rsh_cmd = f'{shlex.quote(self.ssh)} {ssh_known_hosts} -o StrictHostKeyChecking=no -i {shlex.quote(self.keypath)}'

        cmd = [
            self.rsync,
            "-av",
        ]
        if not g_disable_rsync_compress:
            cmd.append("-z")
        cmd += [
            # TODO: parse this out of the output and signal it to the UI for progress
            #"--info=progress2",
            "--chmod=Du=rwx,Dgo=rx,Fu=rwx,Fog=rx",
            "-e", rsh_cmd,
        ]
        if delete_extraneous:
            cmd += ['--delete', '--delete-excluded', '--delete-delay']
        if skip_newer_files:
            cmd += ['--update']
        if verify_checksums:
            if skip_newer_files:
                # UI expected to prevent this
                logger.warning('WARNING: combining --update and --checksum in rsync command line - may cause unexpected behavior.')
            cmd += ['--checksum']
        cmd += extra_cmdline
        if isinstance(remotedir, list):
            if upload:
                raise Exception('Multiple remote sources only supported for download (upload=False)')
            upload_cmd = [f'{user}@{ipaddress}:{source.rstrip("/")}' for source in remotedir]
            upload_cmd.append('{}/'.format(localdir.rstrip('/')))
        else:
            # NOTE: we force folder to folder here with the trailing /
            # makes the function easier to use but limits our ability to pull remote patterns..
            upload_cmd = [
                '{}/'.format(localdir.rstrip('/')),
                '{}@{}:{}/'.format(user, ipaddress, remotedir.rstrip('/')),
            ]
            if not upload:
                upload_cmd.reverse()
        cmd += upload_cmd
        logger.info(shlex.join(cmd))
        self.rsync_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=SUBPROCESS_CREATION_FLAGS,
            )
        while True:
            output = self.rsync_process.stdout.readline()
            if output == b'' and self.rsync_process.poll() is not None:
                break
            if output:
                logger.info(output.decode('utf-8').strip())
        retcode = self.rsync_process.wait()
        self.rsync_process = None
        if retcode != 0:
            raise subprocess.CalledProcessError(retcode, cmd)
        return retcode

    def test_cygpath(self, _path):
        # had to move the cygpath.exe under cygroot/bin/ to avoid the path above the binary to get interpreted as '/' (root)
        logger.info('DBG: testing cygpath.exe behavior, see https://cygwin.com/pipermail/cygwin/2022-June/251750.html')
        wp = pathlib.WindowsPath(_path)
        for i in range(1, len(wp.parts)+1):
            cwp = pathlib.WindowsPath(*wp.parts[:i])
            path = str(cwp)
            cmd=[self.cygpath, path]
            p = subprocess.Popen(
                cmd,
                creationflags=SUBPROCESS_CREATION_FLAGS,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            output = str(p.communicate()[0], 'utf-8')
            s_cmd = ' '.join(cmd)
            logger.info(f'DBG: {s_cmd!r}: {p.returncode} {output!r}')

    def native_to_cygwin_path(self, path):
        p = subprocess.Popen(
            [
                self.cygpath,
                path,
            ],
            creationflags=SUBPROCESS_CREATION_FLAGS,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = p.communicate()

        if stderr:
            logger.debug(f'cygpath stderr: {stderr.decode("utf-8", "replace")}')

        assert p.returncode == 0
        output = stdout.decode('utf-8')
        logger.debug(output)
        return output.split('\n')[0]


class Machine:
    def __init__(self, name, devkit1=(), login=None, http_port=DEFAULT_DEVKIT_SERVICE_HTTP):
        self.address = name
        self.name = name
        self.normalized_name = name
        self.login = login
        self.devkit1 = devkit1
        self.settings = {}
        self._http_port = http_port

    @property
    def http_port(self):
        return self._http_port


def resolve_machine(name, devkit1=(), login=None, need_login=True,
                    need_devkit1=True, name_type=MachineNameType.GUESS,
                    http_port=DEFAULT_DEVKIT_SERVICE_HTTP):
    machine = Machine(name, devkit1=devkit1, login=login, http_port=http_port)

    if name_type is MachineNameType.SERVICE_NAME:
        # Make .local. equivalent to .local
        name = name.rstrip('.')

        # Make .local optional
        if name.endswith('.' + LOCAL_DOMAIN):
            name = name[:-len('.' + LOCAL_DOMAIN)]

        # Make ._steamos-devkit._tcp optional
        if name.endswith('.' + STEAMOS_DEVKIT_SERVICE):
            name = name[:-len('.' + STEAMOS_DEVKIT_SERVICE)]

        machine.name = name

        logger.info("Looking for machine with service name: %s", machine.name)

        global g_zeroconf_listener
        if g_zeroconf_listener is None:
            raise MachineNotFoundError(
                'Zeroconf service is not configured yet.'
            )

        g_zeroconf_listener.update_service_info(machine)

    elif name_type is MachineNameType.ADDRESS:
        # name is (assumed to be) a resolvable DNS hostname.
        # Normalize by squashing to lower case (DNS is case-insensitive
        # for ASCII) and stripping any trailing '.', but continue to use
        # what the user typed on the command-line as machine.address
        machine.normalized_name = machine.name.translate(
            ASCII_LOWERCASE).rstrip('.')

    else:
        assert name_type is MachineNameType.GUESS

        if name.endswith((  # any of:
            '.' + STEAM_DEVKIT_TYPE,
            '.' + STEAMOS_DEVKIT_SERVICE + '.' + LOCAL_DOMAIN,
            '.' + STEAMOS_DEVKIT_SERVICE + '.',
            '.' + STEAMOS_DEVKIT_SERVICE,
        )):
            return resolve_machine(
                name, devkit1=devkit1, login=login, need_login=need_login,
                need_devkit1=need_devkit1,
                name_type=MachineNameType.SERVICE_NAME,
                http_port=machine.http_port
            )
        elif '.' in name:
            return resolve_machine(
                name, devkit1=devkit1, login=login, need_login=need_login,
                need_devkit1=need_devkit1,
                name_type=MachineNameType.ADDRESS,
                http_port=machine.http_port
            )
        else:
            try:
                return resolve_machine(
                    name, devkit1=devkit1, login=login, need_login=need_login,
                    need_devkit1=need_devkit1,
                    name_type=MachineNameType.SERVICE_NAME,
                    http_port=machine.http_port
                )
            except MachineNotFoundError as e:
                # treat as if it was an ADDRESS, and fall through to looking
                # up the login name if necessary
                logger.warning('%s; assuming resolvable address instead', e)

    # If necessary, ask the server which user to log in as
    if ((machine.login is None or not machine.devkit1) and
            (need_login or need_devkit1)):
        logger.debug('Requesting /properties.json')
        request = urllib.request.Request(f'http://{machine.address}:{machine.http_port}/properties.json')
        try:
            response = urllib.request.urlopen(
                request,
                timeout=REQUEST_TIMEOUT
                )
        except socket.error as e:
            pass
        else:
            json_object = json.load(response)
            response.close()
            logger.debug('-> %r', json_object)

            if 'settings' in json_object:
                machine.settings = json.loads(json_object['settings'])

            if 'devkit1' in json_object:
                devkit1 = json_object['devkit1']

                if isinstance(devkit1, str):
                    machine.devkit1 = (devkit1,)
                else:
                    machine.devkit1 = tuple(devkit1)

            if 'login' in json_object:
                machine.login = json_object['login']

    # Backwards compatibility
    if machine.login is None and need_login:
        logger.debug('Requesting /login-name')
        request = urllib.request.Request(f'http://{machine.address}:{machine.http_port}/login-name')
        try:
            response = urllib.request.urlopen(
                request,
                timeout=REQUEST_TIMEOUT
                )
        except urllib.error.HTTPError as e:
            logger.error(
                "The devkit returned HTTP error %d",
                e.code)
            logger.error(
                "Additional output: %s",
                e.file.read().decode('utf-8', 'replace'))
        except socket.error as e:
            logger.error(
                "Unable to connect to devkit on that address. "
                "Check the devkit is running: %s", e)
        else:
            machine.login = response.read().decode('utf-8', 'strict').strip()
            response.close()

    return machine


MAGIC_PHRASE = '900b919520e4cf601998a71eec318fec'

def register(args):
    key, _, _ = ensure_devkit_key()

    machine = resolve_machine(
        args.machine,
        name_type=args.machine_name_type,
        # we will not be executing a ssh command, so this can be skipped
        need_login=False,
        need_devkit1=False,
        http_port=getattr(args, 'http_port', DEFAULT_DEVKIT_SERVICE_HTTP),
    )

    logger.info("Registering with devkit at ip: %s", machine.address)

    data = get_public_key(key)
    # Right now we don't have a pairing dialog mechanism in place on the target machines
    # Someone discovering this port open in the wild could get lucky, pass a key and get registered
    # Add some random 'signature' token to reduce the luck factor a little bit
    # Of course if someone is eavesdropping on the traffic, or gains access to either this source or the approve-ssh-key, this falls apart too
    data = data.strip('\n') + ' ' + MAGIC_PHRASE + '\n'

    # Now that we have a key, register it
    request = urllib.request.Request(
        f'http://{machine.address}:{machine.http_port}/register',
        data=data.encode('ascii'),
        headers={'Content-Type': 'text/plain'},
        method='POST'
        )
    response = urllib.request.urlopen(
        request,
        # If there is a prompt in the Steam client, this may take a while
        timeout=30
        )
    ret = response.read().decode('utf-8', 'strict').strip()
    response.close()
    return ret

def read_game_details(machine, gamename):
    game_folder = os.path.join(
        appdirs.user_config_dir('steamos-devkit'), "games")

    path = os.path.join(game_folder, gamename + ".json")
    jsondata = '{}'

    try:
        with open(path, "r") as f:
            jsondata = f.read()
    except (IOError, FileNotFoundError):
        return None

    json_object = json.loads(jsondata)
    if machine in json_object:
        return json_object[machine]
    else:
        return None


def save_game_details(name, machine, username, gameid, destdir):
    game_folder = os.path.join(
        appdirs.user_config_dir('steamos-devkit'), "games")
    try:
        os.makedirs(game_folder)
    except (FileExistsError):
        pass

    path = os.path.join(game_folder, name + ".json")
    jsondata = '{}'
    try:
        with open(path, "r") as f:
            jsondata = f.read()
    except (IOError, FileNotFoundError):
        pass

    # parse json data
    json_object = json.loads(jsondata)

    json_object[machine] = {
        "destdir": destdir,
        "gameid": gameid,
        "username": username,
    }

    try:
        with open(path + ".new", "w") as f:
            json.dump(json_object, f)
        os.replace(path + '.new', path)
    except (IOError):
        logger.error("Unable to write settings for game data to %s", path)


def list_games(args):
    logger.info(f'List games on {args.machine}')

    (ssh, client, machine) = _open_ssh_for_args_all(args)

    (out_text, _, _) = _simple_ssh(ssh, f'python3 ~/devkit-utils/steamos-list-games', check_status=True)
    json_output = json.loads(out_text)
    return json_output


def new_or_ensure_game(args):
    logger.info(f'Create/update {args.name} on {args.machine}')

    (ssh, client, machine) = _open_ssh_for_args_all(args)

    gameid = args.name
    cmd = f'python3 ~/devkit-utils/steamos-prepare-upload --gameid {gameid}'
    if args.restart_steam:
        cmd += ' --restart-steam 1'
    if args.use_mask_unmask:
        cmd += ' --use-mask-unmask 1'
    if args.prevent_auto_repair:
        cmd += ' --prevent-auto-repair 1'
    (out_text, _, _) = _simple_ssh(ssh, cmd, check_status=True)
    #logger.debug(f'steamos-prepare-upload: {out_text}')
    json_output = json.loads(out_text)

    user = json_output['user']
    destdir = json_output['directory']

    logger.info("Performing rsync of files")
    args.cancel_signal.connect(client.on_cancel_transfer)
    client.rsync_transfer(
        args.directory,
        user,
        machine.address,
        destdir,
        delete_extraneous = args.delete_extraneous,
        skip_newer_files = args.skip_newer_files,
        verify_checksums = args.verify_checksums,
        upload = True,
        extra_cmdline = args.filter_args
    )

    if client._cancel_requested:
        logger.info('Cancelling by user request')
        return True

    if args.steam_play_debug != SteamPlayDebug.Disabled:
        if get_remote_debuggers() is not None:
            ssh = _open_ssh_for_args(args)
            sftp = ssh.open_sftp()

            remote_home, _, _ = _simple_ssh(ssh, f'realpath ~', silent=True, check_status=True)
            remote_home = remote_home.strip('\n')

            if client._cancel_requested:
                logger.info('Cancelling by user request')
                return True

            # Transfer the bundled devkit-msvsmon content: patch and remote setup script
            msvsmon_devkit_local_path = os.path.join(ROOT_DIR, 'devkit-msvsmon')
            assert os.path.exists(msvsmon_devkit_local_path)
            msvsmon_remote_path = f'{remote_home}/devkit-msvsmon'
            client.rsync_transfer(
                msvsmon_devkit_local_path,
                user,
                machine.address,
                msvsmon_remote_path
            )

            if client._cancel_requested:
                logger.info('Cancelling by user request')
                return True

            # Transfer the remote debugging tools that will be deployed
            for remote_debugger in get_remote_debuggers():
                msvsmon_remote_path_sub = f'{remote_home}/devkit-msvsmon/msvsmon{remote_debugger.year}'
                client.rsync_transfer(
                    remote_debugger.directory,
                    user,
                    machine.address,
                    msvsmon_remote_path_sub
                )

            if client._cancel_requested:
                logger.info('Cancelling by user request')
                return True

            # Drop in additional webservices.dll to work around a bug in current Proton release
            webservices_x86 = r'C:\Windows\SysWOW64\webservices.dll'
            webservices_x64 = r'C:\Windows\System32\webservices.dll'
            assert os.path.exists(webservices_x86) and os.path.exists(webservices_x64)
            for remote_debugger in get_remote_debuggers():
                sftp.put(webservices_x86, f'{remote_home}/devkit-msvsmon/msvsmon{remote_debugger.year}/x86/webservices.dll')
                sftp.put(webservices_x64, f'{remote_home}/devkit-msvsmon/msvsmon{remote_debugger.year}/x64/webservices.dll')

            if client._cancel_requested:
                logger.info('Cancelling by user request')
                return True

            if args.steam_play_debug == SteamPlayDebug.Wait:
                # Locate and copy the backend DLLs that support the follow child process functionality
                logger.info('Locate and copy child process extension DLL:')
                success = False
                extensions_root = os.path.join(appdirs.user_data_dir(), r'microsoft\visualstudio')
                for extension_dir in os.listdir(extensions_root):
                    if extension_dir.startswith('15.0_'):
                        year = '2017'
                    elif extension_dir.startswith('16.0_'):
                        year = '2019'
                    elif extension_dir.startswith('17.0_'):
                        year = '2022'
                    else:
                        continue
                    glob_pattern = os.path.join(extensions_root, extension_dir, 'Extensions')
                    glob_pattern += r'\**\ChildProcessDebuggerBackend.dll'
                    # logger.debug(f'glob pattern: {glob_pattern!r}')
                    for srcpath in glob.glob(glob_pattern, recursive=True):
                        bitness = os.path.split(os.path.split(srcpath)[0])[1]
                        # logger.debug(bitness)
                        if not bitness in ('x86', 'x64'):
                            continue
                        dstpath = f'{remote_home}/devkit-msvsmon/msvsmon{year}/{bitness}/ChildProcessDebuggerBackend.dll'
                        logger.info(f'Transfer {srcpath} -> {dstpath}')
                        sftp.put(srcpath, dstpath)
                        # good enough: let's assume we're fine as soon as we see something
                        # things could still be bad, extension installed for the wrong visual studio, bitness missing etc. .. oh well
                        success = True
                if not success:
                    raise Exception('The Microsoft Child Process Debugging Power Tool extension could not be located.\nThis is required in order to enable the wait for attach feature.\nPlease check documentation on partner site.\n')

            if client._cancel_requested:
                logger.info('Cancelling by user request')
                return True

            # NOTE: we specify the python interpreter, so even though msvsmoninstall.py is CRLF it gets executed correctly
            command = 'python3 ~/devkit-msvsmon/msvsmoninstall.py'
            _simple_ssh(ssh, command, check_status=True)
        else:
            raise Exception('Cannot setup the remote debug tools. Please install the Visual Studio remote debugging tools on your development system.')

    if args.env_vars:
        logger.info(f'Environment variables to set for {gameid}: {args.env_vars}')

    jsonobject = {
        'gameid': gameid,
        'directory': destdir,
        'argv': args.argv,
        'env': args.env_vars,
        'settings': {},
        'force_appid': args.force_appid,
        'lepton_args': args.lepton_args,
    }

    parse_settings_arguments(jsonobject, args)

    if gameid not in SIDELOAD_GAMEIDS:
        # the parameter generation is too convoluted for flat command line arguments, so we just pass a json blob
        (out_text, _, _) = _simple_ssh(ssh, f'python3 ~/devkit-utils/steam-client-create-shortcut --parms {shlex.quote(json.dumps(jsonobject))}', check_status=True)
        #logger.debug(f'steam-client-create-shortcut: {out_text}')
        json_output = json.loads(out_text)
        if 'error' in json_output:
            # Does that work to bring a popup?
            raise Exception(json_output['error'])
        logger.info(json_output['success'])
    else:
        # if the steam sideloaded client was already running, then we just paved over it with our upload!
        # under those conditions, the game-updated hook often gets stuck or throws exceptions
        # (because the steam process is still running, but badly mangled and unresponsive)
        # really, we should stop steam and keep gamescope in a holding pattern before we upload ..
        # we still need to write a few critical files though, let's reproduce the useful bits of game-updated here:

        if client._cancel_requested:
            logger.info('Cancelling by user request')
            return True

        remote_path = f'/home/{machine.login}/{DEVKIT_TOOL_FOLDER}'

        argv_fd, argv_path = tempfile.mkstemp(prefix=f'{gameid}-argv.json', text=True)
        json.dump(jsonobject['argv'], open(argv_path, 'wt'))
        settings_fd, settings_path = tempfile.mkstemp(prefix=f'{gameid}-settings.json', text=True)
        json.dump(jsonobject['settings'], open(settings_path, 'wt'))

        sftp = ssh.open_sftp()
        sftp.put(argv_path, str(pathlib.PurePosixPath(remote_path, f'{gameid}-argv.json')))
        sftp.put(settings_path, str(pathlib.PurePosixPath(remote_path, f'{gameid}-settings.json')))

        if args.env_vars:
            env_fd, env_path = tempfile.mkstemp(prefix=f'{gameid}-env.json', text=True)
            json.dump(args.env_vars, open(env_path, 'wt'))
            sftp.put(env_path, str(pathlib.PurePosixPath(remote_path, f'{gameid}-env.json')))
            os.close(env_fd)
            os.unlink(env_path)

        os.close(argv_fd)
        os.unlink(argv_path)
        os.close(settings_fd)
        os.unlink(settings_path)

    return True


def set_steam_client(args):
    # Supported incoming args.name values:
    # None, 'default' or 'SteamStatus.OS': reset to the default OS client
    # 'SteamStatus.OS_DEV': setup the default OS client in 'dev mode'
    # 'steam' or 'SteamStatus.SIDELOADED': setup the sideloaded Steam client
    if args.set_config is None or args.set_config == 'default':
        args.set_config = 'SteamStatus.OS'
    elif args.set_config in SIDELOAD_GAMEIDS:
        args.set_config = 'SteamStatus.SIDELOADED'
    logger.info(
        "Setting the steam client for the main session to %r on machine %r",
        args.set_config,
        args.machine
    )
    command = f'python3 ~/devkit-utils/steamos-set-steam-client --client {args.set_config} --gameid {args.name}'
    if args.args is not None:
        command += f' --args={shlex.quote(args.args)}'
    if args.gdbserver:
        command += ' --gdbserver'
    ssh = _open_ssh_for_args(args)
    (_, _, exit_status) = _simple_ssh(ssh, command)
    if exit_status != 0:
        raise Exception('Failed to set steam client, please see status window.')

def sync_logs(args):
    client = DevkitClient()
    logger.info(
        "Sync logs files from machine %s to %s",
        args.machine, args.local_folder)

    machine = resolve_machine(
        args.machine, name_type=args.machine_name_type,
        http_port=getattr(args, 'http_port', DEFAULT_DEVKIT_SERVICE_HTTP),
    )

    # Build list of remote sources in priority order (first wins on conflicts based on my local testing - seems to contradict documentation wut?)
    # Priority: steamvr_logs > steam_logs > dumps
    remote_sources = []

    # SteamVR logs first (highest priority if different from Steam logs)
    steam_logs_path = REMOTE_STEAM_LOGS_PATH.format(login=machine.login)
    if args.steamvr_logpath and args.steamvr_logpath != steam_logs_path:
        remote_sources.append(args.steamvr_logpath)

    remote_sources.append(steam_logs_path)
    remote_sources.append('/tmp/dumps')

    # Per-device folder structure - rsync will create subdirs by remote path basename
    local_device_folder = os.path.join(args.local_folder, args.device_name)
    os.makedirs(local_device_folder, exist_ok=True)

    client.rsync_transfer(
        local_device_folder,
        machine.login,
        machine.address,
        remote_sources,
        delete_extraneous = False,
        skip_newer_files = False,
        verify_checksums = False,
        upload = False,
    )

    # Proton logs from the home directory
    proton_folder = os.path.join(local_device_folder, 'proton')
    os.makedirs(proton_folder, exist_ok=True)
    client.rsync_transfer(
        proton_folder,
        machine.login,
        machine.address,
        f'/home/{machine.login}',
        delete_extraneous = False,
        skip_newer_files = False,
        verify_checksums = False,
        upload = False,
        extra_cmdline = ['--include=steam-*.log', '--exclude=*'],
    )


# Open a remote shell, or execute a command interactively against a device
# This is handy for privileged operations (sudo) and remote CLI interactions
def remote_shell(args, remote_commands=None):
    client = DevkitClient()
    logger.info(
        "Open remote shell to %s", args.machine
    )
    machine = resolve_machine(
        args.machine,
        login=args.login,
        need_login=True, need_devkit1=True,
        name_type=args.machine_name_type,
        http_port=getattr(args, 'http_port', DEFAULT_DEVKIT_SERVICE_HTTP),
    )
    ssh_command = client.remote_shell_command(
        machine.login,
        machine.address
    )
    if remote_commands is not None:
        assert type(remote_commands) is list
        ssh_command += remote_commands
    return run_in_tethered_terminal(ssh_command)

# Returns the child process
def run_in_tethered_terminal(commands):
    cwd = os.getcwd()
    if platform.system() == 'Windows':
        creationflags = 0
        p = g_win_custom_terminal.Popen(commands)
        if p is not None:
            return p

        # After a bunch of iteration, writing a ps1 script and running it from the ssh.exe directory seems the most reliable
        # trying to pass all command line parameters with a direct powershell.exe invocation runs into a nightmare of path escaping business
        # this also enables us to provide better error handling and diagnostics
        ssh_path = commands[0]
        cwd = os.path.dirname(ssh_path)
        ssh_exe = rf'.\{os.path.basename(ssh_path)}'
        with tempfile.NamedTemporaryFile(
            mode='wt',
            prefix='devkit-remote-shell',
            suffix='.ps1',
            delete=False, # we just leak those .. whatever
        ) as batch:
            logging.info(f'Writing and executing {batch.name}:')
            args_list = ','.join(f'"{c}"' for c in commands[1:])
            # NOTE: exit codes from the remote command script/shell are not propagated back out .. maybe force a pause when debug is on?
            cmd = f"""
# All errors are Terminating errors
$ErrorActionPreference = 'Stop'
try {{
    cd "{cwd}"
    Start-Process -NoNewWindow -Wait -FilePath {ssh_exe} -ArgumentList {args_list}
}} catch {{
    pause
    Exit -1
}}
# Bit of a catch-all since we don't have good error propagation, maybe remove once this proves reliable?
Start-Sleep -Seconds 3
"""
            batch.write(cmd)
            batch.flush()
            # cx_Freeze broke things in 6.12
            # https://github.com/marcelotduarte/cx_Freeze/pull/1659
            powershell_path = shutil.which('powershell.exe')
            if powershell_path is None:
                logger.warning(f'shutil.which powershell.exe failed: {os.environ["PATH"]!r}')
                powershell_path = r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
                if not os.path.exists(powershell_path):
                    raise Exception('Could not locate powershell.exe. shutil.which failed, not at default location.')
            commands = [powershell_path, '-ExecutionPolicy', 'Bypass', batch.name]
        # ensures we get a separate console when running unpackaged
        creationflags = subprocess.CREATE_NEW_CONSOLE
        # we cannot use captured output here, we'd actually capture the shell stdout and break the terminal
        logger.info(f'Run in terminal, cwd {cwd!r}: {" ".join(commands)}')
        p = subprocess.Popen(
            commands,
            cwd=cwd,
            creationflags=creationflags,
        )
        return p

    # Linux
    matched = False
    for terminal_prefix in (
        ['konsole', '-e'],
        ['gnome-terminal', '--'],
        ['xterm', '-e'],
    ):
        shell_path = shutil.which(terminal_prefix[0])
        if shell_path is not None:
            commands = [ shell_path, ] + terminal_prefix[1:] + commands
            logger.info(f'Open terminal: {commands!r}')
            matched = True
            break
    if not matched:
        raise Exception('Could not find a suitable terminal to run command!')
    logger.info(f'Run in terminal, cwd {cwd!r}: {" ".join(commands)}')
    p = g_linux_captured_popen_factory.Popen(
        commands,
        cwd,
    )
    return p


def set_password(args):
    return remote_shell(args, ['~/devkit-utils/steamos-set-password.sh'])


def cef_console(args):
    client = DevkitClient()
    logger.info(
        "Open CEF dev console to %s", args.machine
    )
    machine = resolve_machine(
        args.machine,
        login=args.login,
        need_login=False, need_devkit1=False,
        name_type=args.machine_name_type,
        http_port=getattr(args, 'http_port', DEFAULT_DEVKIT_SERVICE_HTTP),
    )
    webbrowser.open(f'http://{machine.address}:8081', new=2, autoraise=True)


def _simple_ssh(ssh, cmd, silent=False, check_status=False):
    if not silent:
        logger.info(cmd)
    _, stdout, stderr = ssh.exec_command(cmd)
    exit_status = stdout.channel.recv_exit_status()
    out_text = stdout.read().decode('UTF-8', 'replace')
    err_text = stderr.read().decode('UTF-8', 'replace')
    if not silent or (check_status and exit_status != 0):
        if len(out_text) > 0:
            logger.info(out_text)
        if len(err_text) > 0:
            logger.info(err_text)
    if check_status and exit_status != 0:
        raise Exception('command failed, check console')
    return (out_text, err_text, exit_status)


class _AutoClosingSSHClient(paramiko.SSHClient):
    def __del__(self):
        self.close()


def _open_ssh_for_args_all(args, machine=None):
    client = DevkitClient()
    if machine is None:
        machine = resolve_machine(
            args.machine,
            login=args.login,
            need_login=True, need_devkit1=False,
            name_type=args.machine_name_type,
            http_port=getattr(args, 'http_port', DEFAULT_DEVKIT_SERVICE_HTTP),
        )
    ssh = _AutoClosingSSHClient()
    key, key_path, _ = ensure_devkit_key()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    logger.debug(f'Connecting to {machine.login}@{machine.address} with private key {key_path!r}')

    ssh.connect(
        machine.address,
        username=machine.login,
        pkey=key,
        timeout=REQUEST_TIMEOUT,
        look_for_keys=False
    )
    return (ssh, client, machine)

def open_ssh_for_args_all(name, name_type, machine):
    class ArgWrap:
        def __init__(self, name, name_type):
            self.machine = name
            self.machine_name_type = name_type
            self.login = None
    return _open_ssh_for_args_all(ArgWrap(name, name_type), machine)

def _open_ssh_for_args(args):
    return _open_ssh_for_args_all(args)[0]


def _obtain_trace(ssh, sftp, args):
    local_trace_file = args.local_filename
    os.makedirs(os.path.dirname(local_trace_file), exist_ok=True)
    # technically unsecure .. we obtain a unique temp file, delete it and let the gpu-trace service write it out again (tm)
    out_text, _, _ = _simple_ssh(ssh, 'mktemp -p /tmp XXXXXXXXX-trace.zip', silent=True, check_status=True)
    remote_trace_file = out_text.strip('\n')
    _simple_ssh(ssh, f'rm {remote_trace_file}', silent=True, check_status=True)

    capture_cmd = f'gpu-trace --capture --no-gpuvis -o {remote_trace_file}'
    _, err_text, exit_status = _simple_ssh(ssh, capture_cmd)

    # On Steam Deck gpu-trace is installed by default, but not yet on Steam Frame
    if exit_status != 0 and err_text.find('gpu-trace: command not found') != -1:
        raise Exception(f'gpu-trace not found - install the gpu-trace package and reboot!\n{err_text}')

    if exit_status != 0 or err_text.find('Failed to capture trace') != -1:
        # recognize this error, tell the daemon to start tracing
        # (assuming that tracing mode not enabled is the cause .. this is fragile unfortunately)
        logger.info('start tracing and try again')
        start_cmd = 'gpu-trace --start'
        _simple_ssh(ssh, start_cmd)
        logger.info('sleep and let buffers fill up')
        time.sleep(5)
        # issue again
        _, err_text, exit_status = _simple_ssh(ssh, capture_cmd)

    if exit_status != 0:
        # will catch 'command not found' at least (gpu-trace not installed)
        raise Exception('command failed, check console')

    sftp.get(remote_trace_file, local_trace_file)
    # keep /tmp clean - limited space that will quickly fill up otherwise
    # old gpu-trace uses a permission mask that prevents the deletion - will be fixed soon (9/20/2022)
    _simple_ssh(ssh, f'rm {remote_trace_file}', silent=True)

    return local_trace_file


def gpu_trace(args):
    ssh = _open_ssh_for_args(args)
    sftp = ssh.open_sftp()

    local_trace_file = _obtain_trace(ssh, sftp, args)

    if args.launch:
        if not os.path.exists(args.gpuvis_path):
            raise Exception(f'Invalid GPU Vis path - does not exist: {args.gpuvis_path}')
        gpuvis_cmd = [args.gpuvis_path, local_trace_file]
        logger.info(' '.join(gpuvis_cmd))
        spawn_detached(gpuvis_cmd)


def rgp_capture(args):
    global g_signal_status
    g_signal_status.emit(status='Trigger RGP collection...')

    ssh = _open_ssh_for_args(args)

    _simple_ssh(ssh, 'rm /tmp/*.rgp', silent=True)
    # Steam client sets RADV_THREAD_TRACE_TRIGGER=/tmp/rgp.trigger
    # we could set to a different path, but atm RADV always writes the trace to /tmp regardless of the location of the trigger
    _simple_ssh(ssh, 'touch /tmp/rgp.trigger', silent=True)
    logger.info('RGP: trigger file touched, waiting for capture file to appear')

    g_signal_status.emit(status='Wait for trace...')

    remote_filename = None
    filename_wait = time.time()
    while time.time() - filename_wait < 10:
        out_text, _, exit_status = _simple_ssh(ssh, 'ls -1t /tmp/*.rgp', silent=True)
        if exit_status == 0:
            break
        time.sleep(.5)
    if exit_status != 0:
        raise Exception('Timeout waiting for capture file. Vulkan titles only. Check "Enable Graphics Profiling" in Developer settings on the device.')
    remote_path = out_text.split('\n')[0]
    remote_filename = pathlib.PurePosixPath(remote_path).parts[-1]
    logger.info(f'RGP: capture file appeared after {time.time() - filename_wait:.1f}s: {remote_path}')

    # wait for the full write by checking on the size and making sure the file is not longer opened
    size = 0
    stable_count = 0
    writer_active = False
    write_wait = time.time()
    while True:
        if time.time() - write_wait > 60:
            raise Exception(f'Timeout waiting for capture file to finish writing: {remote_path}')
        out_text, _, _ = _simple_ssh(ssh, f'stat -c %s {remote_path}', silent=True, check_status=True)
        updated_size = int(out_text)
        _, _, fuser_status = _simple_ssh(ssh, f'fuser {remote_path}', silent=True, check_status=False)
        now_writing = fuser_status == 0
        if now_writing != writer_active:
            logger.info(f'RGP: {"writer holding file open" if now_writing else "file no longer held open"} (size={updated_size})')
            writer_active = now_writing
        if updated_size != 0 and updated_size == size and not writer_active:
            stable_count += 1
            if stable_count >= 3:
                break
        else:
            if updated_size != size:
                logger.info(f'RGP: capture file growing, size={updated_size}')
            stable_count = 0
        size = updated_size
        time.sleep(.5)
    logger.info(f'RGP: write complete after {time.time() - write_wait:.1f}s, size={size}')

    g_signal_status.emit(status='Download trace...')

    # transfer
    os.makedirs(args.local_folder, exist_ok = True)
    local_path = os.path.join(args.local_folder, remote_filename)
    sftp = ssh.open_sftp()
    sftp.get(remote_path, local_path)
    local_size = os.path.getsize(local_path)
    if local_size != size:
        raise Exception(f'Downloaded capture truncated: {local_size} bytes locally vs {size} on device: {local_path}')
    logger.info(f'Downloaded to {local_path} ({local_size} bytes)')

    if args.launch:
        g_signal_status.emit(status='Launch profiler...')
        if not os.path.exists(args.rgp_path):
            raise Exception(f'Invalid Radeon GPU Profiler path - does not exist: {args.rgp_path}')
        profiler_cmd = [args.rgp_path, local_path]
        spawn_detached(profiler_cmd)


def config_steam_wrapper_flags(devkit, enable, disable):
    '''NOTE: This isn't as generic as you think!
    steam-launch-wrapper only reads a hardcoded set of possible environment variables from this location.
    '''
    ssh = _open_ssh_for_args(ResolveMachineArgs(devkit))
    _simple_ssh(ssh, 'mkdir -p $XDG_RUNTIME_DIR/steam/env', silent=True, check_status=True)
    if disable:
        for env in disable:
            logger.info(f'Turn off Steam launch flag: {env}')
            _simple_ssh(ssh, f'rm -f $XDG_RUNTIME_DIR/steam/env/{shlex.quote(env)}', silent=True, check_status=True)
    if enable:
        for (k,v) in enable.items():
            logger.info(f'Set Steam launch flag: {k}={v}')
            _simple_ssh(ssh, f'echo {shlex.quote(str(v))} > $XDG_RUNTIME_DIR/steam/env/{shlex.quote(k)}', silent=True, check_status=True)
    return (enable, disable)


def _session_select_command(session_arg):
    # SteamOS 3.9+ prefers holo-session-select; SteamOS 3.8 and earlier only have steamos-session-select.
    # Determine the right binary on the device at execution time instead of relying on a cached value.
    return f'if command -v holo-session-select >/dev/null 2>&1; then holo-session-select {session_arg}; else steamos-session-select {session_arg}; fi'


def restart_session(args):
    ssh = _open_ssh_for_args(args)
    # FIXME: unify session management between SteamOS and Steam Frame
    if args.is_deckard:
        (_, _, exit_status) = _simple_ssh(ssh, 'systemctl --user unmask steam ; systemctl --user restart steam')
    else:
        _simple_ssh(ssh, _session_select_command('gamescope'), check_status=True)




def screenshot(args):
    if args.is_deckard:
        ssh = _open_ssh_for_args(args)
        # we do not set filename and timestamp options on the script, we manage those ourselves
        cmd = ['python3', '~/devkit-utils/deckard-capture', '--json']

        (out_text, _, exit_status) = _simple_ssh(ssh, ' '.join(cmd))
        if exit_status != 0:
            try:
                result = json.loads(out_text)
                error_msg = result.get('error', out_text)
            except json.JSONDecodeError:
                error_msg = out_text
            raise Exception(f'Steam Frame capture failed: {error_msg}')

        try:
            result = json.loads(out_text)
            if not result.get('success', False):
                raise Exception(f'Steam Frame capture failed: {result.get("error", "Unknown error")}')
        except json.JSONDecodeError:
            raise Exception('Failed to parse deckard-capture output')

        remote_path = result.get('output')
        remote_filename = pathlib.PurePosixPath(remote_path).parts[-1]
        if args.folder is None or len(args.folder) == 0:
            logger.warning(f'Screenshot folder is not set - forcing to {os.getcwd()}')
            args.folder = os.getcwd()
        if args.filename is None or len(args.filename) == 0:
            args.filename = remote_filename
        suffix = pathlib.Path(remote_filename).suffix
        filename = str(pathlib.Path(args.filename).with_suffix(''))
        if args.do_timestamp:
            timestamp = '-' + datetime.datetime.now().strftime('%Y%m%d%H%M%S')
            local_path = str(pathlib.Path(args.folder, filename + timestamp)) + suffix
        else:
            local_path = str(pathlib.Path(args.folder, filename)) + suffix
        if os.path.exists(local_path):
            # do not overwrite ..
            suffix = pathlib.Path(local_path).suffix
            base = local_path[:-len(suffix)]
            i = 0
            while True:
                test_path = f'{base}_{i:03}{suffix}'
                if not os.path.exists(test_path):
                    local_path = test_path
                    break
                i += 1
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        sftp = ssh.open_sftp()
        sftp.get(remote_path, local_path)
        logger.info(f'Downloaded to {local_path}')
        _simple_ssh(ssh, f'rm "{remote_path}"', silent=True)

        return f'Screenshot saved to {local_path}'

    ssh = _open_ssh_for_args(args)
    # gamescope takes a screenshot asynchronously - we have to wait after signaling
    # wipe any existing screenshot first:
    # - so they don't accumulate in tmpfs
    # - so we can wait and confirm a new screenshot was delivered
    _simple_ssh(ssh, 'rm /tmp/gamescope*.png', silent=True)
    _simple_ssh(ssh, f'DISPLAY=:0 xprop -root -f GAMESCOPECTRL_DEBUG_REQUEST_SCREENSHOT 32c -set GAMESCOPECTRL_DEBUG_REQUEST_SCREENSHOT {args.xprop}', silent=True, check_status=True)
    attempts = 100
    while attempts > 0:
        time.sleep(.1)
        attempts -= 1
        out_text, err_text, exit_status = _simple_ssh(ssh, 'find /tmp -maxdepth 1 -type f -name "gamescope*.png"', silent=True)
        #logger.debug(f'{out_text!r} {err_text!r} {exit_status}')
        if exit_status == 0 and len(out_text) > 0:
            remote_path = out_text.split('\n')[0]
            remote_filename = pathlib.PurePosixPath(remote_path).parts[-1]

            if args.folder is None or len(args.folder) == 0:
                logger.warning(f'Screenshot folder is not set - forcing to {os.getcwd()}')
                args.folder = os.getcwd()

            if args.filename is None or len(args.filename) == 0:
                args.filename = remote_filename

            suffix = pathlib.Path(remote_filename).suffix
            filename = str(pathlib.Path(args.filename).with_suffix(''))
            if args.do_timestamp:
                # use the timestamp of the incoming file, but use the file prefix that has been set
                m = re.search('_[0-9-_]*', remote_filename)
                timestamp = ''
                if m is not None:
                    timestamp = m.group(0)
                else:
                    # gamescope stopped settings timestamps ..
                    timestamp = '-' + datetime.datetime.now().strftime('%Y%m%d%H%M%S')
                local_path = str(pathlib.Path(args.folder, filename + timestamp)) + suffix
            else:
                local_path = str(pathlib.Path(args.folder, filename)) + suffix

            if os.path.exists(local_path):
                # do not overwrite ..
                suffix = pathlib.Path(local_path).suffix
                base = local_path[:-len(suffix)]
                i = 0
                while True:
                    test_path = f'{base}_{i:03}{suffix}'
                    if not os.path.exists(test_path):
                        local_path = test_path
                        break
                    i += 1

            os.makedirs(os.path.dirname(local_path), exist_ok=True)

            sftp = ssh.open_sftp()
            sftp.get(remote_path, local_path)
            logger.info(f'Downloaded to {local_path}')
            break
    if attempts <= 0:
        raise Exception('Could not retrieve screenshot: timeout.')

def sync_utils(args, machine=None):
    client = DevkitClient()
    if machine is None:
        machine = resolve_machine(
            args.machine, login=args.login, need_login=True, need_devkit1=True,
            name_type=args.machine_name_type,
            http_port=getattr(args, 'http_port', DEFAULT_DEVKIT_SERVICE_HTTP),
        )
    user = machine.login or 'root'

    utils_local_path = os.path.join(ROOT_DIR, 'devkit-utils')
    assert os.path.exists(utils_local_path)
    utils_remote_path = '~/devkit-utils'
    logger.info(f'Sync utility scripts to {args.machine}')
    client.rsync_transfer(
        utils_local_path,
        user,
        machine.address,
        utils_remote_path
    )

# Returns None to indicate a failure
def steamos_get_status(args):
    ssh = _open_ssh_for_args(args)
    (out_text, err_text, exit_status) = _simple_ssh(ssh, 'python3 ~/devkit-utils/steamos-get-status --json', silent=True, check_status=False)
    if exit_status != 0:
        logger.warning(err_text)
        return
    try:
        ret = json.loads(out_text)
    except json.JSONDecodeError as e:
        log_exception(e)
        logger.warning('Could not parse steamos-get-status --json output')
        return
    return ret

# Returns None to indicate a failure
def set_session(args):
    ssh = _open_ssh_for_args(args)
    # Must match SESSION_NAMES in steamos-get-status
    # Translate from SESSION_NAMES to the right args and setup for the steamos-session-select script..
    set_wayland = False
    if args.session in ('plasma-x11', 'plasma-wayland'):
        select_arg = 'plasma'
        set_wayland = ( args.session == 'plasma-wayland' )
    else:
        select_arg = args.session
    if set_wayland:
        _simple_ssh(ssh, 'echo wayland > ${XDG_CONF_DIR:-"$HOME/.config"}/steamos-session-type')
    else:
        _simple_ssh(ssh, 'rm -f ${XDG_CONF_DIR:-"$HOME/.config"}/steamos-session-type')
    _simple_ssh(ssh, _session_select_command(select_arg))
    if args.wait:
        steamos_status = None
        count = 0
        while count < 5:
            count += 1
            time.sleep(1)
            steamos_status = steamos_get_status(args)
            if steamos_status is None:
                continue
            if steamos_status['session_status'] == args.session:
                if steamos_status['session_status'] != 'gamescope':
                    return steamos_status
                # give a bit extra for the steam client to start when in gamescope
                if steamos_status['steam_status'] != 'SteamStatus.NOT_RUNNING':
                    return steamos_status
        logger.warning('timeout waiting for requested session change')
        return steamos_status # return the last status anyway

def dump_controller_config(args):
    (ssh, client, machine) = _open_ssh_for_args_all(args)
    # wipe any old data from /tmp first
    _simple_ssh(ssh, 'rm -f /tmp/config_*.tmp')
    cmd = ['python3', '~/devkit-utils/steamos-dump-controller-config']
    #cmd.append('--verbose')
    if args.appid:
        cmd += ['--appid', args.appid]
    elif args.gameid:
        cmd += ['--gameid', args.gameid]
    (out_text, _, _) = _simple_ssh(ssh, ' '.join(cmd), check_status=True)
    json_output = json.loads(out_text)
    if 'error' in json_output:
        # Does that work to bring a popup?
        raise Exception(json_output['error'])
    logger.info(json_output['success'])
    # retrieve files
    os.makedirs(args.folder, exist_ok=True)
    client = DevkitClient()
    client.rsync_transfer(
        args.folder,
        machine.login,
        machine.address,
        '/tmp',
        delete_extraneous = False,
        skip_newer_files = False,
        verify_checksums = False,
        upload = False,
        # doing this way because rsync_transfer only takes in directories
        extra_cmdline = [ '--include=config_*.vdf', '--exclude=*' ]
    )
    return json_output['success']

def delete_title(args):
    ssh = _open_ssh_for_args(args)
    cmd = ['python3', '~/devkit-utils/steamos-delete']
    if args.gameid:
        cmd += ['--delete-title', args.gameid]
    if args.delete_all:
        cmd += ['--delete-all-titles']
    if args.reset_steam_client:
        cmd += ['--reset-steam-client']
    (out_text, _, exit_status) = _simple_ssh(ssh, ' '.join(cmd))
    if exit_status != 0:
        raise Exception(out_text)
    return out_text

def simple_command(devkit, cmd):
    (ssh, client, machine) = _open_ssh_for_args_all(ResolveMachineArgs(devkit))
    if type(cmd) is list:
        cmd = shlex.join(cmd)
    _simple_ssh(ssh, cmd)

def sync_pattern(devkit, host_folder, pattern):
    os.makedirs(host_folder, exist_ok=True)
    (_, client, machine) = _open_ssh_for_args_all(ResolveMachineArgs(devkit))
    client.rsync_transfer(
        host_folder,
        machine.login,
        machine.address,
        f'/home/{machine.login}',
        delete_extraneous = False,
        skip_newer_files = False,
        verify_checksums = False,
        upload = False,
        extra_cmdline = pattern,
    )

def set_renderdoc_replay(devkit, enable):
    (ssh, _, machine) = _open_ssh_for_args_all(ResolveMachineArgs(devkit))
    _simple_ssh(
        ssh,
        'systemctl --user stop devkit-renderdoc-replay 2>/dev/null; killall -9 renderdoccmd',
        silent=True,
        check_status=False,
    )
    if enable:
        # renderdoccmd adds a 'RenderDoc/' subfolder..
        _simple_ssh(
            ssh,
            f'systemd-run --user --unit=devkit-renderdoc-replay --collect '
            f'env RENDERDOC_TEMP=/home/{machine.login} renderdoccmd remoteserver',
            silent=True,
            check_status=True,
        )
        (out, _, status) = _simple_ssh(
            ssh,
            'sleep 2; pgrep -x renderdoccmd',
            silent=True,
            check_status=False,
        )
        if status != 0 or not out.strip():
            raise Exception('renderdoc replay server did not stay running after launch')

def enable_cef_debugging(devkit):
    ssh = _open_ssh_for_args(ResolveMachineArgs(devkit))
    _simple_ssh(ssh, 'touch ~/.steam/steam/.cef-enable-remote-debugging')

def run_game(devkit, gameid):
    ssh = _open_ssh_for_args(ResolveMachineArgs(devkit))
    _simple_ssh(ssh, f'python3 ~/devkit-utils/steam-devkit-rpc run-game gameid={gameid}', check_status=True)
