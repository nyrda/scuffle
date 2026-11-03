"""Find World of Warcraft installations to import, and copy the user's settings from them.

An installation found here is only ever read. Importing seeds a normal install
from it (see Updater.seed_from), so content that matches the published build is
copied and the rest is downloaded. Once that install succeeded, the WTF and
Interface folders can be copied too.

The search runs on a worker thread. It never follows a symbolic link, visits a
folder once, skips network mounts and stops after a fixed time and number of
folders, so a huge or slow drive cannot keep it busy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Callable, Iterator

from . import catalog
from .auth import protobuf
from .ngdp.formats import FormatError, parse_psv

# Folder names Battle.net uses, for a client folder without a readable .flavor.info.
KNOWN_FOLDERS = {
    '_retail_': 'wow', '_classic_': 'wow_classic', '_classic_era_': 'wow_classic_era',
    '_anniversary_': 'wow_anniversary', '_ptr_': 'wowt', '_xptr_': 'wowxptr', '_beta_': 'wow_beta',
    '_classic_ptr_': 'wow_classic_ptr', '_classic_era_ptr_': 'wow_classic_era_ptr', '_forever_': 'wowf',
}
GAME_EXECUTABLE = re.compile(r'wow[a-z0-9-]*\.exe', re.IGNORECASE)
SETTINGS_FOLDERS = ('WTF', 'Interface')        # settings, keybinds, macros and addons

SEARCH_SECONDS = 12
SEARCH_FOLDERS = 20_000      # folders looked at in all
FOLDER_ENTRIES = 4_000       # entries read from one folder
# System and package folders that never hold a game, on Windows and in Wine prefixes.
SKIPPED = {'windows', 'users', 'programdata', '$recycle.bin', 'system volume information', 'recovery',
           'perflogs', 'msocache', 'node_modules', 'lost+found', 'dosdevices', 'proc', 'sys', 'dev'}
# Reading a network share can stall for minutes, so mounted drives of these types are not searched.
NETWORK_FILESYSTEMS = {'nfs', 'nfs4', 'cifs', 'smb3', 'smbfs', 'sshfs', 'fuse.sshfs', 'davfs', 'fuse.davfs2',
                       'afs', 'ceph', 'glusterfs', 'fuse.glusterfs', 'fuse.rclone', '9p', 'fuse.s3fs'}


class NotImportable(ValueError):
    """A folder that cannot be imported, with the reason in the user's words."""


@dataclass(frozen=True)
class Found:
    path: Path                                       # holds .build.info and Data
    clients: dict[str, str] = field(default_factory=dict)   # client code -> its folder in path
    where: str = ''                                  # how it was found, such as "Steam prefix 1234"


@dataclass(frozen=True)
class Place:
    path: Path
    where: str
    depth: int          # how many folder levels below path are searched
    prefix: bool = False    # a Wine prefix, whose drive_c is searched


def nested(source: Path, root: Path) -> bool:
    """Whether one path contains the other by name alone, without touching either folder."""
    source, root = (Path(os.path.normcase(os.path.abspath(p))) for p in (source, root))
    return source == root or root.is_relative_to(source) or source.is_relative_to(root)


def overlaps(source: Path, root: Path) -> bool:
    """Whether one folder contains the other, also through links and other names for the same folder.

    A case-insensitive mount, such as NTFS on Linux, names one folder WoW and wow, so the
    folders' identities are compared as well as their paths.
    """
    source, root = Path(source).resolve(), Path(root).resolve()
    if source == root or root.is_relative_to(source) or source.is_relative_to(root):
        return True
    def identity(path):
        try:
            info = os.stat(path)
        except OSError:
            return None
        return info.st_dev, info.st_ino
    def lineage(path):
        return {key for p in (path, *path.parents) if (key := identity(p))}
    mine, theirs = identity(source), identity(root)
    return bool(mine and mine in lineage(root)) or bool(theirs and theirs in lineage(source))


def _read_small(path: Path, limit: int = 1 << 20) -> bytes:
    with open(path, 'rb') as handle:
        return handle.read(limit)


def _flavor(folder: Path) -> str | None:
    try:
        rows = parse_psv(_read_small(folder / '.flavor.info', 4096))
    except (OSError, FormatError, UnicodeDecodeError):
        return None
    code = rows[0].get('Product Flavor', '') if rows else ''
    return code if catalog.is_product(code) else None


def _has_indexes(path: Path) -> bool:
    data = path / 'Data' / 'data'
    try:
        with os.scandir(data) as entries:
            return any(entry.name.lower().endswith('.idx') for entry in entries)
    except OSError:
        return False


def looks_installed(path: Path) -> bool:
    """A cheap first check: a Battle.net installation has .build.info beside its Data folder."""
    return (path / '.build.info').is_file() and (path / 'Data').is_dir()


def available(path: Path) -> bool:
    """Whether an import's source still has content to copy, as when its drive is connected."""
    return looks_installed(path) and _has_indexes(path) and not (path / '.scuffle').exists()


def inspect(folder: Path, where: str = '') -> Found:
    """The clients a World of Warcraft installation holds. Raises NotImportable with the reason.

    A client folder, such as _retail_, stands for the installation around it.
    """
    folder = Path(folder)
    if not looks_installed(folder) and looks_installed(folder.parent) and _flavor(folder):
        folder = folder.parent
    if (folder / '.scuffle').exists():
        raise NotImportable('This is a Scuffle installation folder, not one to import from.')
    if not looks_installed(folder):
        raise NotImportable('No World of Warcraft installation in this folder. Choose the folder that holds '
                           '.build.info and Data, such as World of Warcraft.')
    if not _has_indexes(folder):
        raise NotImportable('This installation has no game content in Data/data to copy.')
    try:
        listed = {row.get('Product', '') for row in parse_psv(_read_small(folder / '.build.info'))}
    except (OSError, FormatError, UnicodeDecodeError):
        listed = set()
    clients = {}
    try:
        with os.scandir(folder) as entries:
            children = [entry for _, entry in zip(range(FOLDER_ENTRIES), entries)
                        if entry.is_dir(follow_symlinks=False)]
    except OSError as error:
        raise NotImportable(f'The folder cannot be read: {error.strerror or error}') from None
    for entry in sorted(children, key=lambda e: e.name):
        path = Path(entry.path)
        code = _flavor(path) or KNOWN_FOLDERS.get(entry.name.lower())
        if not code or code in clients:
            continue
        # Battle.net leaves a client's folder behind with its settings when it is uninstalled.
        if code in listed or _has_game(path):
            clients[code] = entry.name
    if not clients:
        raise NotImportable('This installation holds no client Scuffle knows.')
    order = [client.code for client in catalog.CLIENTS + catalog.OTHERS]
    ranked = sorted(clients, key=lambda code: (order.index(code) if code in order else len(order), code))
    return Found(folder, {code: clients[code] for code in ranked}, where)


def _has_game(folder: Path) -> bool:
    try:
        with os.scandir(folder) as entries:
            return any(GAME_EXECUTABLE.fullmatch(e.name) and e.is_file(follow_symlinks=False) for e in entries)
    except OSError:
        return False


def client_folder(source: Path, code: str) -> Path | None:
    """The source's own folder for one client, as inspect finds it."""
    try:
        found = inspect(source)
    except NotImportable:
        return None
    name = found.clients.get(code)
    return found.path / name if name else None


# Where to look

def _network_mounts() -> set[Path]:
    try:
        text = Path('/proc/self/mounts').read_text()
    except OSError:
        return set()
    mounts = set()
    for line in text.splitlines():
        parts = line.split(' ')
        if len(parts) > 2 and parts[2] in NETWORK_FILESYSTEMS:
            mounts.add(Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), parts[1])))
    return mounts


def _steam_roots(home: Path) -> list[Path]:
    roots = [home / '.steam/steam', home / '.local/share/Steam',
             home / '.var/app/com.valvesoftware.Steam/.local/share/Steam']
    libraries = []
    for root in roots:
        try:
            text = _read_small(root / 'steamapps/libraryfolders.vdf').decode('utf-8', 'replace')
        except OSError:
            continue
        libraries += [Path(value.replace('\\\\', '\\')) for value in re.findall(r'"path"\s+"([^"]+)"', text)]
    return roots + libraries


def linux_places(home: Path | None = None) -> list[Place]:
    home = home or Path.home()
    places = [Place(home / '.wine', 'Wine prefix', 0, prefix=True),
              Place(home / 'Games', 'Games folder', 3)]
    # Each game's prefix is compatdata/<app id>/pfx. The search finds them.
    places += [Place(steam / 'steamapps/compatdata', 'Steam', 1) for steam in _steam_roots(home)]
    places += [Place(home / '.local/share/bottles/bottles', 'Bottles', 1),
               Place(home / '.var/app/com.usebottles.bottles/data/bottles/bottles', 'Bottles', 1)]
    user = home.name
    # Such as /mnt/c/Program Files (x86)/World of Warcraft or /run/media/<user>/<drive>/Games/World of Warcraft.
    places += [Place(Path('/mnt'), 'Mounted drive', 3), Place(Path('/media'), 'Mounted drive', 3),
               Place(Path('/run/media') / user, 'Mounted drive', 3)]
    return places


def windows_places() -> list[Place]:
    import ctypes
    try:
        drives = os.listdrives()
    except (AttributeError, OSError):
        drives = []
    # A network drive can stall, and a removable one can be empty. Only local disks are searched,
    # even where Battle.net recorded an installation.
    fixed = [drive for drive in drives if ctypes.windll.kernel32.GetDriveTypeW(drive) == 3]
    local = {drive[:2].upper() for drive in fixed}
    program_data = Path(os.environ.get('ProgramData', r'C:\ProgramData'))
    places = [Place(path, 'Recorded by Battle.net', 0)
              for path in recorded(program_data / 'Battle.net/Agent/product.db') if str(path)[:2].upper() in local]
    for name in ('ProgramFiles(x86)', 'ProgramFiles'):
        if os.environ.get(name):
            places.append(Place(Path(os.environ[name]), 'Program Files', 1))
    places += [Place(Path(drive), f'Drive {drive[:2]}', 2) for drive in fixed]
    return places


def recorded(product_db: Path) -> list[Path]:
    """Installation folders Battle.net's agent records in its product.db, a protobuf file.

    Only strings that look like a Windows path are kept. Each one is inspected like any other folder,
    so a field that is not an installation folder costs one check.
    """
    try:
        data = _read_small(product_db, 8 << 20)
    except OSError:
        return []
    paths: list[str] = []

    def walk(blob: bytes, depth: int) -> None:
        try:
            items = protobuf.fields(blob)
        except ValueError:
            return
        for _, wire, value in items:
            if wire != 2:
                continue
            try:
                text = value.decode('utf-8')
            except UnicodeDecodeError:
                text = ''
            if re.fullmatch(r'[A-Za-z]:[\\/][^\x00-\x1f]*', text):
                paths.append(text)
            elif depth < 4:
                walk(value, depth + 1)
    walk(data, 0)
    return [Path(p) for p in dict.fromkeys(paths)]


def prefix_path(prefix: Path, windows: str) -> Path | None:
    """A Windows path inside a Wine prefix. C: is drive_c, other drives come from dosdevices."""
    drive, rest = windows[0].lower(), windows[3:].replace('\\', '/')
    if drive == 'c':
        return prefix / 'drive_c' / rest
    link = prefix / 'dosdevices' / f'{drive}:'
    try:
        # Wine writes relative links too, such as ../drive_c. A link that is absolute replaces the parent.
        return (link.parent / os.readlink(link)).resolve() / rest if link.is_symlink() else None
    except OSError:
        return None


def default_places() -> list[Place]:
    return windows_places() if sys.platform == 'win32' else linux_places()


class Search:
    """Walk the places, yielding each installation found. Stops at its time or folder budget."""

    def __init__(self, places: list[Place] | None = None, *, seconds: float = SEARCH_SECONDS,
                 folders: int = SEARCH_FOLDERS, cancelled: Callable[[], bool] = lambda: False,
                 exclude: Path | None = None):
        self.places = places            # None for the usual places, listed once the search runs
        self.deadline = time.monotonic() + seconds
        self.budget = folders
        self.cancelled = cancelled
        self.exclude = exclude          # the Scuffle folder, which is never offered
        self.complete = True            # False when the time or folder budget ran out
        self._seen: set[tuple[int, int]] = set()
        self._found: set[Path] = set()
        self._network = _network_mounts() if sys.platform.startswith('linux') else set()

    def _over(self) -> bool:
        if self.cancelled():
            return True
        if time.monotonic() >= self.deadline or self.budget <= 0:
            self.complete = False
            return True
        return False

    def _on_network(self, path: Path) -> bool:
        return any(path.is_relative_to(mount) for mount in self._network)

    def _first_visit(self, path: Path, *, follow: bool) -> bool:
        try:
            info = os.stat(path, follow_symlinks=follow)
        except OSError:
            return False
        key = (info.st_dev, info.st_ino)
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    def _candidate(self, path: Path, where: str) -> Iterator[Found]:
        if self._on_network(path) or not looks_installed(path):
            return
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved in self._found or (self.exclude and overlaps(resolved, self.exclude)):
            return
        self._found.add(resolved)
        try:
            yield inspect(path, where)
        except NotImportable:
            pass

    def __iter__(self) -> Iterator[Found]:
        # Listing the places reads Steam's and Battle.net's records, which counts against the time too.
        for place in default_places() if self.places is None else self.places:
            if self._over():
                return
            if self._on_network(place.path):
                continue
            # A place itself may be a link, such as ~/.steam/steam. Below it, links are never followed.
            if not self._first_visit(place.path, follow=True):
                continue
            if place.prefix:
                yield from self._prefix(place.path, place.where)
            else:
                yield from self._walk(place.path, place.where, place.depth)

    def _prefix(self, prefix: Path, where: str) -> Iterator[Found]:
        for windows in recorded(prefix / 'drive_c/ProgramData/Battle.net/Agent/product.db'):
            path = prefix_path(prefix, str(windows))
            if path is not None:
                yield from self._candidate(path, where)
        yield from self._walk(prefix / 'drive_c', where, 2)

    def _walk(self, folder: Path, where: str, depth: int) -> Iterator[Found]:
        if self._over() or self._on_network(folder):
            return
        self.budget -= 1
        if looks_installed(folder):
            yield from self._candidate(folder, where)
            return
        if depth <= 0 or (folder / '.scuffle').exists():
            return
        try:
            with os.scandir(folder) as entries:
                children = [e for _, e in zip(range(FOLDER_ENTRIES), entries)
                            if e.is_dir(follow_symlinks=False) and not _junction(e)
                            and not e.name.startswith('.') and e.name.lower() not in SKIPPED]
        except OSError:
            return
        for entry in sorted(children, key=lambda e: e.name.lower()):
            path = Path(entry.path)
            if self._over():
                return
            if not self._first_visit(path, follow=False):
                continue
            if (path / 'drive_c').is_dir() and not (path / 'drive_c').is_symlink():
                yield from self._prefix(path, f'Wine prefix {entry.name}')
            elif where == 'Steam' and (path / 'pfx/drive_c').is_dir() and not (path / 'pfx').is_symlink():
                yield from self._prefix(path / 'pfx', f'Steam prefix {entry.name}')
            else:
                yield from self._walk(path, where, depth - 1)


def _junction(entry: os.DirEntry) -> bool:
    """A Windows junction, which is_dir reports as a folder, though it leads elsewhere like a link."""
    return getattr(entry, 'is_junction', lambda: False)()


# Copying settings

def _child(folder: Path, name: str) -> Path | None:
    """An entry of folder by name, in any case. A case-sensitive disk can hold WTF or wtf."""
    try:
        with os.scandir(folder) as entries:
            return next((Path(e.path) for e in entries if e.name.lower() == name.lower()), None)
    except OSError:
        return None


def copy_settings(source_game: Path, target_game: Path, *, replaceable: dict[str, bytes] | None = None) -> dict:
    """Copy WTF and Interface from a client folder into a new one. The source is only read.

    A file the target already has is kept, except one in replaceable, by its lower-case path,
    whose content is still exactly what Scuffle wrote, such as its default Config.wtf.
    Links are neither followed nor copied, so nothing outside the source is read and
    nothing in the target can point back into it.
    """
    replaceable = {k.lower(): v for k, v in (replaceable or {}).items()}
    if overlaps(source_game, target_game):
        raise NotImportable('The settings would be copied into the folder they come from.')
    target_game = Path(target_game)
    root = target_game.resolve()
    result = {'copied': 0, 'kept': 0, 'skipped': 0, 'failed': 0, 'error': ''}
    for name in SETTINGS_FOLDERS:
        top = _child(source_game, name)
        if top is None or top.is_symlink() or _junction_path(top) or not top.is_dir():
            continue
        for current, folders, files in os.walk(top, followlinks=False):
            current = Path(current)
            relative = Path(name) / current.relative_to(top)
            try:
                destination = _target_folder(target_game, relative)
            except OSError as error:
                destination, result['error'] = None, result['error'] or str(error)
            if destination is None or not destination.resolve().is_relative_to(root):
                # A link in the target would let a copy land outside it, maybe in the source.
                result['skipped'] += len(files)
                folders.clear()
                continue
            for folder in list(folders):
                if (current / folder).is_symlink() or _junction_path(current / folder):
                    folders.remove(folder)
                    result['skipped'] += 1
            for file in files:
                try:
                    outcome = _copy_one(current / file, destination, (relative / file).as_posix().lower(),
                                        replaceable)
                except OSError as error:
                    outcome, result['error'] = 'failed', result['error'] or str(error)
                result[outcome] += 1
    return result


def _copy_one(origin: Path, destination: Path, key: str, replaceable: dict[str, bytes]) -> str:
    if origin.is_symlink() or not origin.is_file():
        return 'skipped'
    existing = _child(destination, origin.name)
    if existing is not None:
        expected = replaceable.get(key)
        if expected is None or existing.is_symlink() or not existing.is_file() or \
                _read_small(existing, len(expected) + 1) != expected:
            return 'kept'
    _copy_file(origin, existing or destination / origin.name)
    return 'copied'


def _junction_path(path: Path) -> bool:
    return getattr(path, 'is_junction', lambda: False)()


def _target_folder(target_game: Path, relative: Path) -> Path | None:
    """The target folder for a source folder, reusing a folder of the same name in another case."""
    folder = target_game
    for part in relative.parts:
        found = _child(folder, part)
        if found is not None and (found.is_symlink() or not found.is_dir()):
            return None
        folder = found or folder / part
        folder.mkdir(exist_ok=True)
    return folder


def _copy_file(origin: Path, destination: Path) -> None:
    """Through a temporary file, so a crash never leaves half a file that later counts as the user's."""
    fd, temporary = tempfile.mkstemp(prefix='.scuffle-copy-', dir=destination.parent)
    try:
        with os.fdopen(fd, 'wb') as target, open(origin, 'rb') as source:
            while block := source.read(1 << 20):
                target.write(block)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
