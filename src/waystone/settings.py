"""User settings and the platform's configuration, data and cache folders."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import sys

from . import system


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
    extra_clients: list[str] = field(default_factory=list)   # test clients shown in the list
    bandwidth_limit: int = 0          # bytes per second, 0 means unlimited
    workers: int = 16
    proton: str = ''                  # Linux only, empty means the newest found
    selected: str = 'wow'
    modes: dict[str, str] = field(default_factory=dict)
    last_played: dict[str, int] = field(default_factory=dict)   # client -> Unix time

    @property
    def auth_host(self) -> str:
        return f'{self.region}.actual.battle.net'

    @classmethod
    def path(cls) -> Path:
        return config_dir() / 'settings.json'

    @classmethod
    def load(cls) -> 'Settings':
        try:
            data = json.loads(cls.path().read_text())
        except FileNotFoundError:
            return cls()
        if 'show_test_clients' in data and 'extra_clients' not in data:
            # Earlier versions had one switch for every test client.
            from . import catalog
            data['extra_clients'] = [c.code for c in catalog.CLIENTS if c.test] if data['show_test_clients'] else []
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        settings = cls(**known)
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.region not in REGIONS:
            raise ValueError(f'Unknown region {self.region!r}')
        if self.locale not in LOCALES:
            raise ValueError(f'Unknown locale {self.locale!r}')
        if self.bandwidth_limit < 0 or not 1 <= self.workers <= 64:
            raise ValueError('Bandwidth limit and worker count are out of range')
        from . import catalog
        unknown = [code for code in self.extra_clients if code not in catalog.BY_CODE]
        if unknown:
            raise ValueError(f'Unknown clients in settings: {", ".join(unknown)}')

    def save(self) -> None:
        self.validate()
        from .library import atomic_json
        atomic_json(self.path(), asdict(self))
