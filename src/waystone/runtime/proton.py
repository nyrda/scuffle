"""Find Proton builds and umu-run where Steam and other launchers keep them."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil


@dataclass(frozen=True)
class Proton:
    name: str
    path: Path

    @property
    def built(self) -> float:
        """When the build was made, from its version file. A folder's time changes when it is used."""
        try:
            return float((self.path / 'version').read_text().split()[0])
        except (OSError, ValueError, IndexError):
            return self.path.stat().st_mtime

    @property
    def wine(self) -> Path:
        for relative in ['files/bin/wine', 'dist/bin/wine']:
            if (self.path / relative).is_file():
                return self.path / relative
        raise FileNotFoundError(f'{self.path} contains no Wine binary')


def compatibility_dirs() -> list[Path]:
    home = Path.home()
    data = Path(os.environ.get('XDG_DATA_HOME', home / '.local/share'))
    steam_roots = [home / '.steam/root', home / '.steam/steam', data / 'Steam',
                   home / '.var/app/com.valvesoftware.Steam/data/Steam']
    dirs = [root / 'compatibilitytools.d' for root in steam_roots]
    dirs += [root / 'steamapps/common' for root in steam_roots]
    dirs += [Path('/usr/share/steam/compatibilitytools.d'), Path('/usr/local/share/steam/compatibilitytools.d')]
    dirs += [data / 'umu/compatibilitytools', data / 'lutris/runners/proton']
    unique, seen = [], set()
    for directory in dirs:
        try:
            key = directory.resolve()
        except OSError:
            continue
        if key not in seen and directory.is_dir():
            seen.add(key)
            unique.append(directory)
    return unique


def is_proton(path: Path) -> bool:
    return (path / 'proton').is_file() and any((path / r).is_file() for r in ['files/bin/wine', 'dist/bin/wine'])


def find_protons() -> list[Proton]:
    found, seen = [], set()
    for directory in compatibility_dirs():
        for candidate in sorted(directory.iterdir()):
            try:
                if candidate.resolve() in seen or not is_proton(candidate):
                    continue
            except OSError:
                continue
            seen.add(candidate.resolve())
            found.append(Proton(candidate.name, candidate))
    return found


def find_umu() -> Path | None:
    home = Path.home()
    data = Path(os.environ.get('XDG_DATA_HOME', home / '.local/share'))
    for candidate in [shutil.which('umu-run'), home / '.local/bin/umu-run', '/usr/bin/umu-run',
                      data / 'faugus-launcher/umu-run', data / 'lutris/runtime/umu/umu-run']:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return Path(candidate)
    return None


def wine_wrapper() -> list[str]:
    """NixOS cannot run Proton's prebuilt Wine outside an FHS environment."""
    if Path('/etc/NIXOS').exists():
        steam_run = shutil.which('steam-run')
        if not steam_run:
            raise FileNotFoundError('NixOS needs steam-run to start Proton\'s Wine')
        return [steam_run]
    return []
