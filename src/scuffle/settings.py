"""User settings and the platform's configuration, data and cache folders."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import threading

from . import system
from .launch_options import ClientOptions, LinuxOptions


MEGABYTE = 1_000_000      # speed limits are in decimal MB/s, like every size and rate shown
RETRY_AFTER = 3 * 3600    # seconds before an automatic update that failed is tried again for the same version


def config_dir() -> Path:
    if system.WINDOWS:
        return Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'Scuffle'
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'scuffle'


def data_dir() -> Path:
    if system.WINDOWS:
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'Scuffle'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'scuffle'


def cache_dir() -> Path:
    if system.WINDOWS:
        return data_dir() / 'cache'
    return Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'scuffle'


REGIONS = {'eu': 'Europe', 'us': 'Americas', 'kr': 'Korea', 'tw': 'Taiwan'}
LOCALE_NAMES = {'enUS': 'English (US)', 'enGB': 'English (UK)', 'deDE': 'Deutsch', 'esES': 'Español (España)',
                'esMX': 'Español (Latinoamérica)', 'frFR': 'Français', 'itIT': 'Italiano', 'koKR': '한국어',
                'ptBR': 'Português (Brasil)', 'ruRU': 'Русский', 'zhTW': '繁體中文'}
LOCALES = list(LOCALE_NAMES)


@dataclass
class Settings:
    root: str = ''
    region: str = 'eu'
    locale: str = 'enUS'
    extra_clients: list[str] = field(default_factory=list)   # test and other clients shown in the list
    hidden_clients: list[str] = field(default_factory=list)  # clients left out of the list, live ones too
    show_other_clients: bool = False  # other public clients, beyond the live and test ones
    bandwidth_limit: int = 0          # bytes per second, 0 means unlimited
    workers: int = 16
    pause_while_playing: bool = False
    resume_downloads: bool = False
    show_notifications: bool = True
    keep_in_tray: bool = False        # closing the window leaves Scuffle in the tray, where there is one
    # Client -> the new version a notification named, so each version is announced once.
    announced_updates: dict[str, str] = field(default_factory=dict)
    # Updates the user consented to in advance. The switch covers the live clients, less the
    # exceptions. A test or other client updates by itself only when chosen on its own.
    auto_update: bool = False
    auto_update_except: list[str] = field(default_factory=list)
    auto_update_extra: list[str] = field(default_factory=list)
    # Client -> the version an automatic update failed for and when, as {'version', 'at'}.
    auto_update_failures: dict[str, dict] = field(default_factory=dict)
    check_self_updates: bool = True   # whether Scuffle asks GitHub for a newer Scuffle
    skipped_self_update: str = ''     # the release the user chose to skip
    announced_self_update: str = ''   # the release a desktop notification already told about
    proton: str = ''                  # Linux only, empty means the newest found
    selected: str = 'wow'
    modes: dict[str, str] = field(default_factory=dict)
    last_played: dict[str, int] = field(default_factory=dict)   # client -> Unix time
    # Battle.net login -> the id of the WoW account the game starts with. Without one the game asks.
    game_accounts: dict[str, int] = field(default_factory=dict)
    # Sign in with the user's own browser, which they chose on the sign-in page, and not in Scuffle's window.
    sign_in_with_browser: bool = False
    # Launch options. The Linux ones apply to every client, and each client can add its own.
    linux_launch: LinuxOptions = field(default_factory=LinuxOptions)
    client_launch: dict[str, ClientOptions] = field(default_factory=dict)

    # What load() had to drop from the file, for the window to report. Not saved.
    problems = ()

    @property
    def auth_host(self) -> str:
        return f'{self.region}.actual.battle.net'

    @classmethod
    def path(cls) -> Path:
        return config_dir() / 'settings.json'

    @classmethod
    def load(cls) -> 'Settings':
        from . import catalog
        try:
            data = json.loads(cls.path().read_text())
        except FileNotFoundError:
            return cls()
        if 'show_test_clients' in data and 'extra_clients' not in data:
            # Earlier versions had one switch for every test client.
            data['extra_clients'] = [c.code for c in catalog.clients() if c.test] if data['show_test_clients'] else []
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        for name in ['extra_clients', 'hidden_clients']:
            # The catalog changes with what Blizzard publishes, so a code it does not know
            # now is kept for later. Only entries that cannot be a product are dropped.
            value = known.get(name, [])
            known[name] = sorted({code for code in value if catalog.is_product(code)}) if isinstance(value, list) else []
        known['show_other_clients'] = known.get('show_other_clients') is True
        known['sign_in_with_browser'] = known.get('sign_in_with_browser') is True
        launch, clients = known.pop('linux_launch', {}), known.pop('client_launch', {})
        settings = cls(**known)
        settings.validate()
        settings.load_launch_options(launch, clients)
        return settings

    def load_launch_options(self, launch, clients) -> None:
        """Read the launch options leniently. A bad value is dropped and reported, never fatal."""
        from . import catalog
        problems: list[str] = []
        self.linux_launch = LinuxOptions.from_json(launch, problems)
        self.client_launch = {}
        for code, data in (clients.items() if isinstance(clients, dict) else []):
            # Discovered clients are not in the catalog yet when settings load, so any
            # product code counts. Only an entry that cannot be a product is dropped.
            if not catalog.is_product(code):
                problems.append(f'Launch options of an unknown client {code!r} were removed.')
                continue
            options = ClientOptions.from_json(data, catalog.get(code).label, problems)
            if options is not None:
                self.client_launch[code] = options
        if not isinstance(clients, dict):
            problems.append('The launch options of the clients were unreadable and were removed.')
        self.problems = tuple(problems)

    def validate(self) -> None:
        if self.region not in REGIONS:
            raise ValueError(f'Unknown region {self.region!r}')
        if self.locale not in LOCALES:
            raise ValueError(f'Unknown locale {self.locale!r}')
        for name, default, low, high in [('bandwidth_limit', 0, 0, 10_000 * MEGABYTE),
                                          ('workers', 16, 1, 64)]:
            value = getattr(self, name)
            setattr(self, name, max(low, min(high, value)) if type(value) is int else default)
        self.pause_while_playing = self.pause_while_playing is True
        accounts = self.game_accounts if isinstance(self.game_accounts, dict) else {}
        self.game_accounts = {owner: chosen for owner, chosen in accounts.items()
                              if isinstance(owner, str) and owner.isdigit() and type(chosen) is int}
        self.resume_downloads = self.resume_downloads is True
        self.show_notifications = self.show_notifications is not False
        self.keep_in_tray = self.keep_in_tray is True
        announced = self.announced_updates if isinstance(self.announced_updates, dict) else {}
        self.announced_updates = {code: version for code, version in announced.items()
                                  if isinstance(code, str) and isinstance(version, str)}
        self.validate_automatic_updates()
        from . import catalog
        malformed = [code for code in self.extra_clients + self.hidden_clients if not catalog.is_product(code)]
        if malformed:
            raise ValueError(f'Not a client code: {", ".join(map(repr, malformed))}')

    def validate_automatic_updates(self) -> None:
        """Anything unreadable means no consent: the switch is off and a malformed entry is dropped."""
        from . import catalog
        self.auto_update = self.auto_update is True
        for name in ['auto_update_except', 'auto_update_extra']:
            value = getattr(self, name)
            # As in the client lists, a code the catalog does not know now is kept for later.
            setattr(self, name, sorted({code for code in value if catalog.is_product(code)})
                    if isinstance(value, list) else [])
        failures = self.auto_update_failures if isinstance(self.auto_update_failures, dict) else {}
        self.auto_update_failures = {
            code: {'version': failure['version'], 'at': failure['at']} for code, failure in failures.items()
            if catalog.is_product(code) and isinstance(failure, dict) and isinstance(failure.get('version'), str)
            and type(failure.get('at')) is int}

    def updates_automatically(self, code: str) -> bool:
        """Whether the user lets this client update without asking."""
        from . import catalog
        if catalog.get(code).optional:
            return code in self.auto_update_extra
        return self.auto_update and code not in self.auto_update_except

    def set_updates_automatically(self, code: str, on: bool) -> None:
        """One client's own choice. A live client turned on also turns the switch on."""
        from . import catalog
        if catalog.get(code).optional:
            chosen = set(self.auto_update_extra)
            self.auto_update_extra = sorted(chosen | {code} if on else chosen - {code})
            return
        excepted = set(self.auto_update_except)
        self.auto_update_except = sorted(excepted - {code} if on else excepted | {code})
        if on:
            self.auto_update = True

    def failed_recently(self, code: str, version: str, now: float) -> bool:
        """Whether an automatic update to this version failed too recently to try it again by itself."""
        failure = self.auto_update_failures.get(code)
        return bool(failure and failure['version'] == version and now - failure['at'] < RETRY_AFTER)

    def listed(self, code: str) -> None:
        """Show a client in the list again."""
        from . import catalog
        self.hidden_clients = [c for c in self.hidden_clients if c != code]
        if catalog.get(code).optional and code not in self.extra_clients:
            self.extra_clients = sorted(self.extra_clients + [code])

    def unlisted(self, code: str) -> None:
        """Leave a client out of the list, installed or not."""
        self.extra_clients = [c for c in self.extra_clients if c != code]
        if code not in self.hidden_clients:
            self.hidden_clients = sorted(self.hidden_clients + [code])

    def save(self) -> None:
        from .library import atomic_json
        # The window and a move on the download worker can both save. Each writes the
        # values of the moment it holds the lock, so the move's new folder is never undone.
        with _saving:
            self.validate()
            atomic_json(self.path(), asdict(self))


_saving = threading.Lock()
