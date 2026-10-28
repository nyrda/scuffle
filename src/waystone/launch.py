"""Sign in, renew the session and start a client."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable
import urllib.parse
import webbrowser

from . import catalog, system
from .auth.bgs import CredentialRejected, login, renew
from .auth.vault import Vault
from .launch_options import ClientOptions, OptionError, has_nvidia, linux_environment, wrapper_command
from .library import Library, LibraryError
from .runtime.game import GameRuntime, LinuxRuntime, WindowsRuntime
from .runtime.proton import Proton, find_protons, find_umu
from .settings import Settings, bridge_executable, data_dir

STARTUP_TIMEOUT = 120
SignIn = Callable[[str], bytes]


class LaunchError(RuntimeError):
    pass


def parse_ticket(text: str) -> bytes:
    """Accept the address shown after signing in, or its bare ST value."""
    text = text.strip()
    if '?' in text:
        text = (urllib.parse.parse_qs(urllib.parse.urlsplit(text).query).get('ST') or [''])[0]
    ticket = text.encode()
    if not 0 < len(ticket) <= 2048 or any(byte <= 32 or byte >= 127 for byte in ticket):
        raise ValueError('Paste the whole address shown after signing in, including ?ST=')
    return ticket


def looks_like_redirect(text: str) -> bool:
    return 'localhost:0' in text and 'ST=' in text


def open_browser(url: str) -> None:
    """Open a battle.net URL without sharing the browser's output with ours."""
    if system.WINDOWS:
        os.startfile(url)  # noqa: S606, the URL was checked to be on battle.net
        return
    opener = 'open' if sys.platform == 'darwin' else 'xdg-open'
    try:
        subprocess.Popen([opener, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except FileNotFoundError:
        webbrowser.open(url)


def select_proton(settings: Settings) -> Proton:
    found = find_protons()
    if settings.proton:
        chosen = Path(settings.proton)
        match = next((p for p in found if p.path == chosen), None)
        return match or Proton(chosen.name, chosen)
    if not found:
        raise LaunchError('No Proton build was found. Install one with Steam, ProtonUp or your launcher of choice.')
    return sorted(found, key=lambda p: p.path.stat().st_mtime)[-1]


def make_runtime(settings: Settings) -> GameRuntime:
    bridge = bridge_executable()
    if not bridge.is_file():
        raise LaunchError(f'The Waystone bridge is missing at {bridge}')
    if system.WINDOWS:
        return WindowsRuntime(bridge)
    umu = find_umu()
    if umu is None:
        raise LaunchError('umu-run was not found. Install umu-launcher or a launcher that ships it.')
    return LinuxRuntime(bridge, data_dir() / 'prefix', select_proton(settings), umu)


def log_dir() -> Path:
    """Where Proton writes steam-battlenet.log when its log is on."""
    return data_dir() / 'logs'


@dataclass
class LaunchPlan:
    """What the user's launch options add to a client's start."""
    arguments: list[str]
    environment: dict[str, str]
    wrapper: list[str]


def launch_plan(settings: Settings, code: str, *, windows: bool = system.WINDOWS) -> LaunchPlan:
    """The user's arguments, variables and wrappers for one client, checked before signing in."""
    label = catalog.get(code).label
    options = settings.client_launch.get(code) or ClientOptions()
    try:
        arguments = options.game_arguments()
    except OptionError as error:
        raise LaunchError(f'The launch options of {label} need a fix. {error}') from None
    if windows:
        return LaunchPlan(arguments, {}, [])
    try:
        wrapper = wrapper_command(settings.linux_launch)
    except OptionError as error:
        raise LaunchError(f'{error} Change it in Settings.') from None
    environment = linux_environment(settings.linux_launch, options, nvidia=has_nvidia(), log_dir=log_dir())
    return LaunchPlan(arguments, environment, wrapper)


class Launcher:
    def __init__(self, settings: Settings, library: Library, runtime: GameRuntime | None = None,
                 vault: Vault | None = None):
        self.settings, self.library = settings, library
        self.runtime = runtime or make_runtime(settings)
        self.vault = vault or Vault(data_dir() / 'session', self.runtime.bridge)

    def signed_in(self) -> bool:
        return self.vault.has_session()

    def sign_in(self, version: int, sign_in: SignIn) -> None:
        self.runtime.prepare()
        login(self.vault, self.settings.auth_host, version, sign_in)
        self.vault.record('signed_in')

    def renew(self, version: int, sign_in: SignIn) -> None:
        """Renew the session, signing in through the browser when needed."""
        self.runtime.prepare()
        if self.vault.has_session():
            try:
                renew(self.vault, self.settings.auth_host, version)
                self.vault.record('renewed')
                return
            except CredentialRejected as error:
                self.vault.record('rejected', str(error))
                # The ticket is spent. Keeping it would claim a sign-in that is gone.
                self.vault.session.unlink(missing_ok=True)
        self.sign_in(version, sign_in)
        renew(self.vault, self.settings.auth_host, version)
        self.vault.record('renewed')

    def play(self, code: str, sign_in: SignIn, *, mode: str | None = None,
             status: Callable[[str], None] | None = None) -> subprocess.Popen:
        entry = catalog.get(code)
        state = self.library.state(code)
        if state is None:
            raise LaunchError(f'{entry.label} is not installed')
        if self.library.pending(code):
            raise LaunchError(f'{entry.label} has an unfinished update')
        if self.library.running(code):
            raise LaunchError(f'{entry.label} is already running')
        executable = self.library.executable(code)
        plan = launch_plan(self.settings, code)
        arguments = entry.launch_arguments(mode, [*(state.get('launch_arguments') or []), *plan.arguments])
        version = int(state['version'].rsplit('.', 1)[-1])
        status = status or (lambda text: None)
        with self.vault.locked():
            status('Connecting')        # renews the saved session, or signs in if it has none
            self.renew(version, sign_in)
            status('Starting')
            ack = self.vault.directory / f'.launch-{os.getpid()}-{time.time_ns()}.ack'
            process = self.runtime.start(executable, arguments, self.vault.token, ack,
                                         environment=plan.environment, wrapper=plan.wrapper)
            try:
                deadline = time.monotonic() + STARTUP_TIMEOUT
                while time.monotonic() < deadline:
                    if ack.is_file():
                        try:
                            if json.loads(ack.read_text()).get('game_started') is True:
                                return process
                        except ValueError:
                            pass
                    if process.poll() is not None:
                        break
                    time.sleep(0.1)
                raise LaunchError(f'{entry.label} did not start. Check its runtime and try again.')
            finally:
                ack.unlink(missing_ok=True)
                self.vault.token.unlink(missing_ok=True)
