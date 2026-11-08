"""Sign in, renew the session and start a client."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import socket
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable
import urllib.parse
import webbrowser

from . import catalog, system
from .auth.bgs import BuildRefused, CredentialRejected, login, renew
from .auth.vault import Login, Vault
from .launch_options import ClientOptions, OptionError, linux_environment, wrapper_command
from .library import Library, LibraryError
from .runtime.bridge import bridge_executable
from .runtime.game import GameRuntime, LinuxRuntime
from .runtime.proton import Proton, find_protons, find_umu
from .settings import Settings, data_dir

STARTUP_TIMEOUT = 120
SignIn = Callable[[str], bytes]


class LaunchError(RuntimeError):
    pass


def queue_status(position: int, exit_time: int, now: float) -> str:
    """The launch phase while Battle.net's login queue holds the logon."""
    parts = ['In login queue']
    if position:
        parts.append(f'{position:,} ahead')
    if exit_time > now:
        minutes = round((exit_time - now) / 60)
        parts.append(f'about {minutes} min' if minutes else 'under a minute')
    return ' · '.join(parts)


def parse_ticket(text: str) -> bytes:
    """Accept the address shown after signing in, or its bare ST value."""
    text = text.strip()
    if '?' in text:
        text = (urllib.parse.parse_qs(urllib.parse.urlsplit(text).query).get('ST') or [''])[0]
    ticket = text.encode()
    if not 0 < len(ticket) <= 2048 or any(byte <= 32 or byte >= 127 for byte in ticket):
        raise ValueError('Paste the whole address shown after signing in, including ?ST=')
    return ticket


def ticket_account(ticket: bytes) -> str | None:
    """The account id a sign-in ticket ends in, such as 314159265 in EU-<hex>-314159265.

    Only a hint for the sign-in page. A ticket of another shape is used as before.
    """
    match = re.fullmatch(rb'[A-Z]{2,4}-[0-9A-Fa-f]+-([1-9][0-9]*)', ticket)
    return match.group(1).decode() if match else None


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
    return max(found, key=lambda p: p.built)


def make_runtime(settings: Settings) -> GameRuntime:
    if system.WINDOWS:
        from .runtime.windows import WindowsRuntime
        return WindowsRuntime()
    try:
        bridge = bridge_executable()
    except OSError as error:
        raise LaunchError(f'The Scuffle bridge could not be copied to its folder: {error}') from None
    if not bridge.is_file():
        raise LaunchError(f'The Scuffle bridge is missing at {bridge}')
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
    environment = linux_environment(settings.linux_launch, options, log_dir=log_dir())
    return LaunchPlan(arguments, environment, wrapper)


def chosen_account(settings: Settings, known: dict | None) -> dict | None:
    """The WoW account the game starts with, or None to let the game ask.

    A sole account needs no choice. Of several, only the saved choice counts, so an account
    that is gone or a different Battle.net login falls back to the game's own question.
    """
    if not known:
        return None
    accounts = known['game_accounts']
    saved = settings.game_accounts.get(str(known['owner']))
    if saved is None and len(accounts) == 1:
        return accounts[0]
    # A chosen account that is gone never turns into the one left.
    return next((account for account in accounts if account['id'] == saved), None)


class Launcher:
    def __init__(self, settings: Settings, library: Library, runtime: GameRuntime | None = None,
                 vault: Vault | None = None):
        self.settings, self.library = settings, library
        self.runtime = runtime or make_runtime(settings)
        self.vault = vault or Vault(data_dir() / 'session', self.runtime.bridge)

    def signed_in(self) -> bool:
        return self.vault.has_session()

    def sign_in(self, version: int, sign_in: SignIn, queue=None) -> Login:
        """Add the account the browser signs in to, or renew its session if it is listed. It becomes active."""
        self.runtime.prepare()
        added = login(self.vault, self.settings.auth_host, version, sign_in, queue)
        self.vault.record('signed_in', 'with a login ticket' if added.has_login_ticket() else 'without a login ticket')
        return added

    def renew(self, version: int, sign_in: SignIn, account: Login | None = None, queue=None) -> Login:
        """Renew a login's session, the active one's by default, signing in through the browser when needed.

        Returns the login renewed. A browser sign-in to another account adds that account and stops,
        so a launch never starts an account other than the one it was for.
        """
        self.runtime.prepare()
        account = account or self.vault.active()
        wanted = account.label if account is not None else ''     # read before a rejection deletes it
        if account is not None and account.has_session():
            try:
                renew(account, self.settings.auth_host, version, queue)
                self.vault.record('renewed')
                return account
            except CredentialRejected as error:
                self.vault.record('rejected', str(error))
                if self.renew_with_login_ticket(account, version, queue):
                    return account
                # The ticket is spent. Keeping it would claim a sign-in that is gone. Other logins stay.
                self.vault.remove(account)
        added = self.sign_in(version, sign_in, queue)
        if account is not None and account.id is not None and added.id != account.id:
            raise LaunchError(f'Battle.net ended the session of {wanted}, and the browser signed in to '
                              f'{added.label} instead. {added.label} was added and is now the active account. '
                              'Press Play to play with it.')
        renew(added, self.settings.auth_host, version, queue)
        self.vault.record('renewed')
        return added

    def renew_with_login_ticket(self, account: Login, version: int, queue) -> bool:
        """Try the ticket the last browser sign-in handed back, so an expired session needs no browser."""
        if not account.has_login_ticket():
            return False
        try:
            renew(account, self.settings.auth_host, version, queue, credential=account.load_login_ticket())
        except CredentialRejected as error:
            self.vault.record('login_ticket_rejected', str(error))
            return False
        self.vault.record('renewed_with_login_ticket')
        return True

    def play(self, code: str, sign_in: SignIn, *, mode: str | None = None,
             status: Callable[[str], None] | None = None) -> subprocess.Popen:
        entry = catalog.get(code)
        state = self.library.state(code)
        if state is None:
            raise LaunchError(f'{entry.label} is not installed')
        if self.library.moving():
            raise LaunchError('The installation folder is being moved. Play once the move is done.')
        if self.library.pending(code):
            raise LaunchError(f'{entry.label} has an unfinished update')
        if self.library.running(code):
            raise LaunchError(f'{entry.label} is already running')
        executable = self.library.executable(code)
        plan = launch_plan(self.settings, code)
        # The product config's own launch_arguments are not passed. Several clients publish -launch,
        # which current games reject with a command line error before their window opens.
        arguments = entry.launch_arguments(mode, plan.arguments)
        version = int(state['version'].rsplit('.', 1)[-1])
        status = status or (lambda text: None)
        with self.vault.locked():
            # Read once under the lock, so switching accounts meanwhile never changes the session used.
            login = self.vault.active()
            status('Connecting')        # renews the saved session, or signs in if it has none
            try:
                login = self.renew(version, sign_in, login,
                                   lambda position, exit_time: status(queue_status(position, exit_time, time.time())))
            except (TimeoutError, ConnectionError, socket.gaierror) as error:
                raise LaunchError('Battle.net did not answer. Check your connection and try again.') from error
            except BuildRefused as error:
                raise LaunchError(f'Battle.net is refusing logons from {entry.label} {state["version"]} right '
                                  'now, which can happen while Blizzard rolls out a patch. '
                                  'Try again later.') from error
            status('Starting')
            ack = self.vault.directory / f'.launch-{os.getpid()}-{time.time_ns()}.ack'
            account = chosen_account(self.settings, login.accounts())
            process = self.runtime.start(executable, arguments, self.vault.token, ack,
                                         environment=plan.environment, wrapper=plan.wrapper,
                                         account=(account['name'], account['region']) if account else None)
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
                # A wrapper's or variable's failure is otherwise silent, since its output is not kept.
                hint = ' Your launch options may be the cause.' if plan.arguments or plan.environment \
                    or plan.wrapper else ''
                raise LaunchError(f'{entry.label} did not start. Check its runtime and try again.{hint}')
            finally:
                ack.unlink(missing_ok=True)
                self.vault.token.unlink(missing_ok=True)
