"""User settings and the platform's configuration, data and cache folders."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import sys

from . import system
from .launch_options import ClientOptions, LinuxOptions


MEGABYTE = 1_000_000      # speed limits are in decimal MB/s, like every size and rate shown


def config_dir() -> Path:
    if system.WINDOWS:
        return Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'Waystone'
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'waystone'


def data_dir() -> Path:
    if system.WINDOWS:
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'Waystone'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'waystone'


def cache_dir() -> Path:
    if system.WINDOWS:
        return data_dir() / 'cache'
    return Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'waystone'


def bridge_executable() -> Path:
    """The packaged bridge, or the one a Nix wrapper points to."""
    override = os.environ.get('WAYSTONE_BRIDGE')
    if override:
        return Path(override)
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    return base / 'bridge' / 'wow_auth_bridge.exe'


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
    proton: str = ''                  # Linux only, empty means the newest found
    selected: str = 'wow'
    modes: dict[str, str] = field(default_factory=dict)
    last_played: dict[str, int] = field(default_factory=dict)   # client -> Unix time
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
            data['extra_clients'] = [c.code for c in catalog.CLIENTS if c.test] if data['show_test_clients'] else []
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        for name in ['extra_clients', 'hidden_clients']:
            # The catalog changes with what Blizzard publishes, so a code it does not know
            # now is kept for later. Only entries that cannot be a product are dropped.
            value = known.get(name, [])
            known[name] = sorted({code for code in value if catalog.is_product(code)}) if isinstance(value, list) else []
        known['show_other_clients'] = known.get('show_other_clients') is True
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
            if code not in catalog.BY_CODE:
                problems.append(f'Launch options of an unknown client {code} were removed.')
                continue
            options = ClientOptions.from_json(data, catalog.BY_CODE[code].label, problems)
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
        self.resume_downloads = self.resume_downloads is True
        from . import catalog
        malformed = [code for code in self.extra_clients + self.hidden_clients if not catalog.is_product(code)]
        if malformed:
            raise ValueError(f'Not a client code: {", ".join(map(repr, malformed))}')

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
        self.validate()
        from .library import atomic_json
        atomic_json(self.path(), asdict(self))
