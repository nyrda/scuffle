"""One installation folder that holds every installed client.

All clients share the CASC store under Data/. Each client has its own plan,
state and game folder under .scuffle/clients/<code>/ and <root>/<folder>/.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
from typing import Callable, Iterator

from . import catalog, system
from .ngdp.cdn import CDNClient, TransferCancelled
from .ngdp.formats import EncodingTable, decode_blte, read_config, read_download, read_install
from .ngdp.models import DownloadEntry
from .ngdp.store import STATE_DIR, CascSource, CascStore, StoreError

IDENTITY = {'format': 1, 'kind': 'scuffle'}
BUILD_INFO_COLUMNS = [
    'Branch!STRING:0', 'Active!DEC:1', 'Build Key!HEX:16', 'CDN Key!HEX:16', 'Install Key!HEX:16',
    'IM Size!DEC:4', 'CDN Path!STRING:0', 'CDN Hosts!STRING:0', 'CDN Servers!STRING:0', 'Tags!STRING:0',
    'Armadillo!STRING:0', 'Last Activated!STRING:0', 'Version!STRING:0', 'KeyRing!HEX:16', 'Product!STRING:0']


GAME_EXECUTABLE = re.compile(r'Wow[A-Za-z]*\.exe')


class LibraryError(RuntimeError):
    pass


class Cancelled(LibraryError):
    pass


def atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        system.sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def atomic_json(path: Path, value) -> None:
    atomic_bytes(path, (json.dumps(value, indent=2) + '\n').encode())


def mount_points() -> list[Path]:
    """Every mount point Linux knows of, including bind mounts. Empty elsewhere."""
    if not sys.platform.startswith('linux'):
        return []
    try:
        text = Path('/proc/self/mountinfo').read_text()
    except OSError:
        # Without it a bind mount of the same file system cannot be told apart, so nothing is deleted.
        raise LibraryError('Linux mount information is unreadable, so nothing is deleted') from None
    unescape = lambda field: re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), field)
    return [Path(unescape(line.split(' ')[4])) for line in text.splitlines() if len(line.split(' ')) > 4]


KEPT_FOLDERS = {'wtf', 'interface', 'screenshots'}     # the user's settings, addons and pictures


def remove_tree(path: Path) -> bool:
    """Delete a folder, making read-only entries writable first. False when something stays."""
    def retry(function, name, error):
        # Only a refused delete is retried. Anything else, such as an unreadable folder, stays.
        if function not in (os.unlink, os.rmdir, os.remove):
            raise error
        for target in (os.path.dirname(name), name):
            try:
                os.chmod(target, os.stat(target, follow_symlinks=False).st_mode | stat.S_IWRITE | stat.S_IEXEC)
            except OSError:
                pass
        function(name)
    if not path.exists():
        return True
    try:
        shutil.rmtree(path, onexc=retry)
    except (OSError, TypeError):
        return False
    system.sync_directory(path.parent)
    return True


def md5_file(path: Path) -> bytes:
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'md5').digest()


def default_preferences(region: str, locale: str) -> bytes:
    """The Config.wtf a new client folder starts with. Imported settings may replace it."""
    return (f'SET portal "{region.upper()}"\n'
            f'SET textLocale "{locale}"\n'
            f'SET audioLocale "{locale}"\n').encode()


def flavor_info(code: str) -> bytes:
    return f'Product Flavor!STRING:0\n{code}\n'.encode()


def managed_path(name: str) -> str:
    """Use one spelling for Windows directories on a case-sensitive host."""
    normalized = name.replace('\\', '/')
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts or any(p in {'', '.', '..'} or ':' in p for p in path.parts):
        raise LibraryError(f'Unsafe installation path: {name!r}')
    if '\x00' in normalized or any(ord(c) < 32 for c in normalized):
        raise LibraryError('Control character in installation path')
    return '/'.join([part.upper() for part in path.parts[:-1]] + [path.name])


def contained(root: Path, relative: str) -> Path:
    result = root / managed_path(relative)
    if not result.resolve().is_relative_to(root.resolve()):
        raise LibraryError('An installation path escapes through a symbolic link')
    return result


def build_info_row(state: dict) -> list[str]:
    version, cdn = state['version_record'], state['cdn_record']
    profiles = ':'.join(f"Windows x86_64 {state['region'].upper()}? {state['locale']} {part}?"
                        for part in ['speech', 'text'])
    return [state['region'], '1', version['BuildConfig'], version['CDNConfig'], '', '', cdn['Path'],
            cdn['Hosts'], cdn.get('Servers', ''), profiles, '', '', version['VersionsName'],
            state.get('keyring') or version.get('KeyRing', ''), state['product']]


def build_info(states: list[dict]) -> bytes:
    rows = [build_info_row(state) for state in sorted(states, key=lambda s: s['product'])]
    return ('\n'.join('|'.join(row) for row in [BUILD_INFO_COLUMNS, *rows]) + '\n').encode()


class Library:
    """The installation folder. Use as a context manager for changes."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.state_dir = self.root / STATE_DIR
        self.metadata = self.state_dir / 'metadata'
        self.clients_dir = self.state_dir / 'clients'
        self.has_download_job: Callable[[str], bool] = lambda code: False
        self.uninstall_record = self.state_dir / 'uninstall.json'
        self.move_record = self.state_dir / 'move.json'      # the folder is being moved, see move.py

    # Identity and locking

    def exists(self) -> bool:
        try:
            return json.loads((self.state_dir / 'identity.json').read_text()) == IDENTITY
        except (OSError, ValueError):
            return False

    def create(self) -> None:
        if self.exists():
            return
        if self.root.exists() and any(self.root.iterdir()):
            raise LibraryError('Choose an empty folder for a new installation')
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_json(self.state_dir / 'identity.json', IDENTITY)

    def reject_symlinks(self) -> None:
        names = [STATE_DIR, f'{STATE_DIR}/metadata', f'{STATE_DIR}/clients', 'Data', 'Data/data', 'Data/config',
                 f'{STATE_DIR}/library.lock', f'{STATE_DIR}/identity.json', f'{STATE_DIR}/uninstall.json',
                 f'{STATE_DIR}/move.json']
        if self.clients_dir.is_dir() and not self.clients_dir.is_symlink():
            for client in self.clients_dir.iterdir():
                names += [str(client.relative_to(self.root)), str(client.relative_to(self.root) / 'staging')]
        if self.root.is_dir():
            # Client folders follow Battle.net's _name_ convention.
            names += [p.name for p in self.root.iterdir() if p.name.startswith('_') and p.name.endswith('_')]
        for name in names:
            if (self.root / name).is_symlink():
                raise LibraryError(f'Managed directory must not be a symbolic link: {name}')

    @contextmanager
    def locked(self):
        """Hold the folder exclusively while clients are changed."""
        self.reject_symlinks()
        if not self.exists():
            raise LibraryError(f'{self.root} is not a Scuffle installation')
        try:
            with system.locked_file(self.state_dir / 'library.lock', blocking=False):
                if self.moving():
                    # Checked under the lock, which the move holds while it writes the marker.
                    raise LibraryError('This installation folder is being moved. Scuffle finishes or '
                                       'undoes the move when it starts.')
                if self.uninstall_record.exists():
                    self.recovered_uninstall = self._finish_uninstall()
                elif not self.uninstall_record.is_symlink():
                    self.sweep_trash()
                yield self
        except system.LockBusy:
            raise LibraryError('Another Scuffle operation is using this installation') from None

    def moving(self) -> bool:
        return self.move_record.exists() or self.move_record.is_symlink()

    # Clients

    def client_dir(self, code: str) -> Path:
        catalog.get(code)
        return self.clients_dir / code

    def state(self, code: str) -> dict | None:
        if self.uninstalling() == code:
            return None
        try:
            return json.loads((self.client_dir(code) / 'state.json').read_text())
        except FileNotFoundError:
            return None

    def installed(self) -> list[str]:
        if not self.clients_dir.is_dir():
            return []
        # Any product code, since the catalog grows with what Blizzard publishes.
        return sorted(p.name for p in self.clients_dir.iterdir()
                      if catalog.is_product(p.name) and p.name != self.uninstalling()
                      and (p / 'state.json').is_file())

    # Uninstall is a roll-forward transaction. The durable record hides the client
    # before any files move; locked() finishes it before another writer proceeds.

    def uninstalling(self) -> str | None:
        if self.state_dir.is_symlink() or self.uninstall_record.is_symlink():
            raise LibraryError('Uninstall record must not be a symbolic link')
        try:
            return json.loads(self.uninstall_record.read_text())['code']
        except FileNotFoundError:
            return None

    def _removal_path(self, relative: str) -> Path:
        """Validate each component without changing its spelling or following links."""
        managed_path(relative)
        path = self.root
        for part in PurePosixPath(relative).parts:
            if part in {'.', '..'} or '/' in part or '\\' in part:
                raise LibraryError('Unsafe uninstall path')
            if (path / part).is_symlink():
                raise LibraryError(f'Uninstall refuses symbolic links: {relative}')
            path = contained(path, part)
        if path == self.root or not path.resolve().is_relative_to(self.root):
            raise LibraryError('Uninstall path must stay inside the installation')
        return path

    def _removal_tree(self, path: Path) -> None:
        self._removal_path(path.relative_to(self.root).as_posix())     # forward slashes on Windows too
        device = self.root.lstat().st_dev
        def foreign(entry: Path) -> bool:
            # A bind mount, a junction or another drive mounted inside is not ours to delete.
            return (os.path.ismount(entry) or entry.lstat().st_dev != device
                    or (hasattr(entry, 'is_junction') and entry.is_junction()))
        real = Path(os.path.realpath(path))
        # A bind mount of the same file system has the same device, so the mount table decides.
        inside = [point for point in mount_points() if point.is_relative_to(real)]
        if inside:
            raise LibraryError(f'Uninstall refuses another file system mounted at {inside[0]}')
        if path.exists() and foreign(path):
            raise LibraryError(f'Uninstall refuses another file system: {path.relative_to(self.root)}')
        if path.is_dir():
            def unreadable(error):
                # A folder that cannot be listed cannot be checked, so it is not deleted.
                raise LibraryError(f'Uninstall cannot check {error.filename}') from error
            for directory, folders, files in os.walk(path, followlinks=False, onerror=unreadable):
                for name in folders + files:
                    child = Path(directory) / name
                    if child.is_symlink():
                        raise LibraryError(f'Uninstall refuses symbolic links: {child.relative_to(self.root)}')
                    if foreign(child):
                        raise LibraryError(f'Uninstall refuses another file system: {child.relative_to(self.root)}')

    def _uninstall_folder(self, code: str, folder: str) -> Path:
        if (not isinstance(folder, str) or managed_path(folder) != folder or '/' in folder
                or folder.casefold() in {'data', STATE_DIR.casefold()}
                or folder.startswith(('Scuffle-backup-', '.scuffle-trash-'))):
            raise LibraryError('Unsafe client folder for uninstall')
        game = self._removal_path(folder)
        self._removal_tree(game)
        for other in self.clients_dir.iterdir():
            if other.name == code or not catalog.is_product(other.name):
                continue
            for name in ('state.json', 'pending.json'):
                snapshot = other / name
                if snapshot.exists() and json.loads(snapshot.read_text()).get('folder', '').casefold() == folder.casefold():
                    raise LibraryError('Another client owns this folder; uninstall refused')
        if game.exists() and (game / '.flavor.info').read_bytes() != flavor_info(code):
            raise LibraryError('The game folder does not belong to this client')
        return game

    def uninstall(self, code: str, *, keep_settings: bool = True, progress=None) -> dict:
        """Remove a client inside locked(); keep addons/settings by default.

        Once the journal is durable, cancellation cannot abandon a partly deleted
        client. Startup or the next locked operation completes the removal.
        """
        self.reject_symlinks()
        for path in (self.state_dir, self.root / 'Data', self.root / '.build.info'):
            self._removal_tree(path)
        state = self.state(code)
        if not state:
            raise LibraryError(f'{catalog.get(code).label} is not installed')
        if self.running(code):
            raise LibraryError('Close the client before uninstalling')
        if self.pending(code) is not None:
            raise LibraryError('Finish the unfinished update before uninstalling')
        if self.has_download_job(code):
            raise LibraryError('Remove the download job before uninstalling')
        game = self._uninstall_folder(code, state['folder'])
        self._removal_tree(game)
        token = uuid.uuid4().hex
        record = {'code': code, 'folder': state['folder'], 'token': token, 'keep_settings': keep_settings}
        for name in (f'.scuffle-trash-{token}', f'Scuffle-backup-{code}-{token}'):
            if self._removal_path(name).exists():
                raise LibraryError('Uninstall destination already exists')
        atomic_json(self.uninstall_record, record)
        return self._finish_uninstall(progress=progress)

    def _finish_uninstall(self, *, progress=None) -> dict:
        self._removal_path(f'{STATE_DIR}/uninstall.json')
        record = json.loads(self.uninstall_record.read_text())
        code, token = record['code'], record['token']
        catalog.get(code)
        if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{32}', token):
            raise LibraryError('Invalid uninstall recovery record')
        self.reject_symlinks()
        for path in (self.state_dir, self.root / 'Data', self.root / '.build.info'):
            self._removal_tree(path)
        game = self._uninstall_folder(code, record['folder'])
        trash = self._removal_path(f'.scuffle-trash-{token}')
        backup = self._removal_path(f'Scuffle-backup-{code}-{token}')
        client = self._removal_path(f'{STATE_DIR}/clients/{code}')
        for path in (game, trash, backup, client):
            self._removal_tree(path)
        emit = progress or (lambda event: None)
        emit({'stage': 'uninstalling'})
        if game.exists():
            if any(self._executable_running(path) for path in game.iterdir()
                   if GAME_EXECUTABLE.fullmatch(path.name)):
                raise LibraryError('Close the client before recovering its uninstall')
            if trash.exists():
                raise LibraryError('Both game and uninstall trash exist; refusing to overwrite either')
            os.rename(game, trash)
            system.sync_directory(self.root)
        if record['keep_settings'] and trash.exists():
            for source in trash.iterdir():
                if source.name.casefold() in KEPT_FOLDERS:
                    backup.mkdir(exist_ok=True)
                    system.sync_directory(self.root)
                    destination = self._removal_path(f'{backup.name}/{source.name}')
                    if destination.exists():
                        raise LibraryError('Settings backup already exists; refusing to overwrite it')
                    os.rename(source, destination)
                    system.sync_directory(backup)
                    system.sync_directory(trash)
        # The client's own state goes into the trash too. A rename cannot half fail, so once
        # the record is gone the client is uninstalled, whatever deleting the trash meets.
        if client.exists():
            trash.mkdir(exist_ok=True)
            os.rename(client, trash / '.scuffle-client')
            system.sync_directory(self.clients_dir)
        # The record already excludes this state from build.info and from readers.
        self.write_build_info()
        self.uninstall_record.unlink()
        system.sync_directory(self.state_dir)
        emit({'stage': 'cleaning_up'})
        removed = remove_tree(trash)
        try:
            cleanup = self.cleanup(threshold=0.0)
        except (LibraryError, StoreError, OSError) as error:
            # Other clients may still be playing or updating. Their content stays.
            cleanup = {'deferred': str(error)}
        return {'uninstalled': code, 'backup': str(backup) if backup.exists() else '', 'cleanup': cleanup,
                'leftover': '' if removed else str(trash)}

    def sweep_trash(self) -> None:
        """Delete what an earlier uninstall could not, such as a file that was open. Never raises."""
        for trash in self.root.glob('.scuffle-trash-*'):
            try:
                self._removal_tree(trash)
            except (LibraryError, OSError):
                continue        # not provably ours, so it stays
            remove_tree(trash)

    def pending(self, code: str) -> dict | None:
        try:
            return json.loads((self.client_dir(code) / 'pending.json').read_text())
        except FileNotFoundError:
            return None

    def download_progress(self, code: str) -> tuple[int, int]:
        """Stored bytes in a completed pending plan, or an unknown size."""
        try:
            pending = self.pending(code)
            if not isinstance(pending, dict) or pending.get('phase') not in ('downloading', 'activating'):
                return 0, 0
            plan = self.client_dir(code) / 'plan.sqlite'
            inventory = self.state_dir / 'store.sqlite'
            with closing(sqlite3.connect(plan.as_uri() + '?mode=ro', uri=True)) as db:
                db.execute('ATTACH DATABASE ? AS owned', (inventory.as_uri() + '?mode=ro',))
                total, stored = db.execute(
                    'SELECT coalesce(sum(p.size),0), '
                    'coalesce(sum(CASE WHEN o.ekey IS NOT NULL THEN p.size ELSE 0 END),0) '
                    'FROM objects p LEFT JOIN owned.objects o ON o.ekey=p.ekey').fetchone()
                return stored, total
        except (OSError, ValueError, sqlite3.Error):
            return 0, 0

    def game_dir(self, code: str) -> Path | None:
        state = self.state(code)
        return self.root / state['folder'] if state else None

    def executable(self, code: str) -> Path | None:
        state = self.state(code)
        if not state:
            return None
        # Test and other builds name it differently, such as WowT.exe, WowB.exe or WowClassicT.exe.
        names = sorted(name for name in state.get('managed_files', []) if GAME_EXECUTABLE.fullmatch(name))
        name = 'Wow.exe' if 'Wow.exe' in names or not names else names[0]
        return self.root / state['folder'] / name

    def running(self, code: str, processes: list | None = None) -> bool:
        """processes is one listing from system.running_executables, shared by several checks."""
        path = self.executable(code)
        if path is None:
            return False
        return self._executable_running(path, processes)

    def running_games(self) -> list[str]:
        """The installed clients whose game runs, from one listing of the processes."""
        installed = self.installed()
        names = {path.name.lower() for path in map(self.executable, installed) if path}
        processes = system.running_executables(names) if names else []
        return [code for code in installed if self.running(code, processes)]

    def game_processes(self, code: str) -> list[int]:
        """The client's game processes, for closing it. Empty when it does not run."""
        path = self.executable(code)
        return system.game_processes(path) if path else []

    def _executable_running(self, path: Path, processes: list | None = None) -> bool:
        folder, name = path.parent, path.name.lower()
        executable = str(path).lower()
        for _, command, cwd in (system.running_executables() if processes is None else processes):
            text = command.lower()
            if name not in text:
                continue
            if executable in text or executable.replace('/', '\\') in text:
                return True
            # Wine can rewrite argv to a prefix drive letter. Its cwd keeps
            # the real folder.
            if cwd and Path(cwd) == folder:
                return True
        return False

    def write_build_info(self, replacing: dict | None = None) -> None:
        states = [self.state(code) for code in self.installed()]
        if replacing:
            states = [s for s in states if s['product'] != replacing['product']] + [replacing]
        atomic_bytes(self.root / '.build.info', build_info(states))

    def storage(self) -> dict:
        """Bytes stored in the shared store and the bytes each client needs."""
        database = self.state_dir / 'store.sqlite'
        stored = 0
        if database.is_file():
            db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
            try:
                stored = db.execute('SELECT coalesce(sum(size),0) FROM objects').fetchone()[0]
            finally:
                db.close()
        clients = {code: (self.state(code) or {}).get('encoded_bytes', 0) for code in self.installed()}
        return {'stored': stored, 'clients': clients}

    # Cleanup

    def cleanup(self, *, threshold: float = 0.25, progress=None, cancelled=None) -> dict:
        """Remove content no client needs. Call inside locked().

        Every plan describes one client's build. While an installed client has
        an unfinished update, its plan already describes the new build while
        the old one is still in use, so cleanup waits until it finished.
        """
        plans = sorted(self.clients_dir.glob('*/plan.sqlite')) if self.clients_dir.is_dir() else []
        # A client being installed may be planning again, with a plan that does not list
        # what it already downloaded yet. Its content stays until it finished.
        for pending in sorted(self.clients_dir.glob('*/pending.json')) if self.clients_dir.is_dir() else []:
            code = pending.parent.name
            name = catalog.get(code).label if catalog.is_product(code) else code
            raise LibraryError(f'Finish the {name} {"update" if self.state(code) else "download"} before cleaning up')
        for code in self.installed():
            if self.running(code):
                raise LibraryError(f'Close {catalog.get(code).label} before cleaning up')
        live = self.state_dir / 'live.sqlite'
        live.unlink(missing_ok=True)
        # URI mode lets the plans attach read-only.
        db = sqlite3.connect(live.as_uri(), uri=True)
        try:
            db.execute('CREATE TABLE keys(ekey BLOB PRIMARY KEY) WITHOUT ROWID')
            for i, plan in enumerate(plans):
                db.execute(f'ATTACH DATABASE ? AS plan{i}', (plan.as_uri() + '?mode=ro',))
                db.execute(f'INSERT OR IGNORE INTO keys SELECT ekey FROM plan{i}.objects')
                db.commit()
                db.execute(f'DETACH DATABASE plan{i}')
        finally:
            db.close()
        store = CascStore(self.root)
        try:
            return store.compact(live, threshold=threshold, progress=progress, cancelled=cancelled)
        finally:
            store.close()
            live.unlink(missing_ok=True)

    # Migration

    @classmethod
    def migrate_wowupdater(cls, root: Path) -> 'Library':
        """Adopt a single-client wowupdater installation in place."""
        root = Path(root).resolve()
        old = root / '.wowupdater'
        if not (old / 'identity.json').is_file():
            raise LibraryError('No wowupdater installation found')
        identity = json.loads((old / 'identity.json').read_text())
        if identity != {'format': 1, 'product': 'wowf'}:
            raise LibraryError('Unsupported wowupdater installation')
        state = json.loads((old / 'state.json').read_text())
        if not state.get('complete'):
            raise LibraryError('Finish or repair the wowupdater installation before adopting it')
        new = root / STATE_DIR
        if new.exists():
            raise LibraryError(f'{new} already exists')
        os.rename(old, new)
        client = new / 'clients' / 'wowf'
        client.mkdir(parents=True)
        for name in ['plan.sqlite', 'plan.sqlite-wal', 'plan.sqlite-shm', 'state.json', 'pending.json', 'staging']:
            if (new / name).exists():
                os.rename(new / name, client / name)
        (new / 'update.lock').unlink(missing_ok=True)
        state.update(folder='_forever_')
        state.pop('executable', None)
        atomic_json(client / 'state.json', state)
        atomic_json(new / 'identity.json', IDENTITY)
        library = cls(root)
        with library.locked():
            library.write_build_info()
        return library


class Plan:
    """The objects and files one client build needs."""

    def __init__(self, path: Path, inventory: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('PRAGMA cache_size=-65536')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS objects(ekey BLOB PRIMARY KEY,size INTEGER NOT NULL,priority INTEGER NOT NULL) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,ckey BLOB NOT NULL,ekey BLOB NOT NULL,size INTEGER NOT NULL) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS metadata(name TEXT PRIMARY KEY,value TEXT NOT NULL);
        ''')
        self.db.execute('ATTACH DATABASE ? AS owned', (str(inventory),))

    def close(self):
        self.db.close()

    def add(self, entries):
        self.db.executemany('''INSERT INTO objects VALUES(?,?,?) ON CONFLICT(ekey) DO UPDATE SET
            priority=min(priority,excluded.priority)''', ((e.ekey, e.encoded_size, e.priority) for e in entries))
        self.db.commit()

    def entries(self, *, missing=False, physical=False) -> Iterator[DownloadEntry]:
        if physical:
            sql = ('SELECT p.ekey,p.size,p.priority FROM objects p LEFT JOIN owned.objects o ON o.ekey=p.ekey '
                   'ORDER BY o.segment,o.offset')
        elif missing:
            sql = ('SELECT p.ekey,p.size,p.priority FROM objects p LEFT JOIN owned.objects o ON o.ekey=p.ekey '
                   'WHERE o.ekey IS NULL ORDER BY p.priority,p.ekey')
        else:
            sql = 'SELECT ekey,size,priority FROM objects ORDER BY priority,ekey'
        for key, size, priority in self.db.execute(sql):
            yield DownloadEntry(key, size, priority)

    def totals(self) -> tuple[int, int, int, int]:
        total = self.db.execute('SELECT count(*),coalesce(sum(size),0) FROM objects').fetchone()
        missing = self.db.execute('''SELECT count(*),coalesce(sum(p.size),0) FROM objects p
            LEFT JOIN owned.objects o ON o.ekey=p.ekey WHERE o.ekey IS NULL''').fetchone()
        return (*total, *missing)


class Updater:
    """Install or update one client in a library. Call inside library.locked()."""

    def __init__(self, library: Library, code: str, client: CDNClient, *, locale='enUS', region='eu',
                 seed_from: Path | None = None, progress: Callable[[dict], None] | None = None,
                 cancelled: Callable[[], bool] | None = None, may_publish: Callable[[], bool] | None = None):
        """cancelled is asked between objects and files. may_publish is asked once, right before
        the published indexes change, and False stops the update as a pause does."""
        self.library, self.root = library, library.root
        self.code, self.entry = code, catalog.get(code)
        self.client, self.locale, self.region = client, locale, region
        self.seed_from = seed_from
        self.progress = progress or (lambda event: None)
        self.cancelled = cancelled or (lambda: False)
        self.may_publish = may_publish or (lambda: True)
        self.client_dir = library.client_dir(code)
        self.client_dir.mkdir(parents=True, exist_ok=True)
        library.metadata.mkdir(parents=True, exist_ok=True)
        self.metadata = library.metadata
        self.store = CascStore(self.root)
        self.source = None
        self.plan = Plan(self.client_dir / 'plan.sqlite', self.store.inventory_path)
        self._last_progress = 0.0
        self.snapshot: dict = {}

    def close(self):
        self.plan.close()
        if self.source:
            self.source.close()
        self.store.close()
        self.client.close()

    def emit(self, stage: str, *, force=False, **fields):
        now = time.monotonic()
        if force or now - self._last_progress >= 0.5:
            self.progress({'client': self.code, 'stage': stage, **fields})
            self._last_progress = now

    def check_cancelled(self):
        if self.cancelled():
            self.store.checkpoint()
            raise Cancelled('Paused')

    def _seed(self):
        if self.seed_from is None:
            return
        source = self.seed_from.resolve()
        if source == self.root or self.root.is_relative_to(source) or source.is_relative_to(self.root):
            raise LibraryError('Seed and installation directories must be separate')
        self.emit('indexing_seed', force=True, source=str(source))
        self.source = self.store.import_existing(source)

    def _encoded(self, key: bytes, size: int | None = None) -> Path:
        path = self.metadata / (key.hex() + '.blte')
        if self.store.has(key):
            if not path.is_file():
                self.store.extract_to(key, path)
            return path
        if self.source:
            try:
                self.source.extract_to(key, path)
                self.store.put(key, path)
                return path
            except (KeyError, StoreError, OSError):
                path.unlink(missing_ok=True)
        path = self.client.get_object(key, size)
        self.store.put(key, path)
        return path

    def _decoded(self, name: str, ck: bytes, ek: bytes, size: int, encoded_size: int) -> Path:
        path = self.metadata / (ek.hex() + '.decoded')
        if path.is_file() and path.stat().st_size == size and md5_file(path) == ck:
            if not self.store.has(ek):
                self._encoded(ek, encoded_size)
            return path
        self.emit('metadata', force=True, name=name, encoded_bytes=encoded_size)
        decode_blte(self._encoded(ek, encoded_size), path, ek, ck, size)
        return path

    def _fill_plan(self, plan: Plan, build: dict, signature: str, region: str, locale: str, *,
                   stage: str | None) -> None:
        """Record the objects and files a build needs. A plan made for the same signature is kept."""
        def manifest(name):
            ck, ek = (bytes.fromhex(x) for x in build[name])
            decoded, encoded = map(int, build[name + '-size'])
            return self._decoded(name, ck, ek, decoded, encoded)

        encoding = manifest('encoding')
        install = manifest('install')
        download = manifest('download')
        selected = {'Windows', 'x86_64', region.upper(), locale, 'speech', 'text'}
        old = plan.db.execute("SELECT value FROM metadata WHERE name='signature'").fetchone()
        with EncodingTable(encoding) as table:
            if not old or old[0] != signature:
                if stage:
                    self.emit(stage, force=True)
                plan.db.execute('DELETE FROM objects')
                plan.db.execute('DELETE FROM files')
                plan.db.commit()
                plan.add(read_download(download, selected))
                for entry in read_install(install, selected):
                    relative = managed_path(entry.path)
                    size, choices = table.lookup(entry.ckey)
                    if size != entry.decoded_size or not choices:
                        raise LibraryError('Install and encoding metadata disagree')
                    encoding_info = choices[0]
                    duplicate = plan.db.execute('SELECT ckey FROM files WHERE lower(path)=lower(?)', (relative,)).fetchone()
                    if duplicate and duplicate[0] != entry.ckey:
                        raise LibraryError('Conflicting selected install file variants')
                    plan.db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?)',
                        (relative, entry.ckey, encoding_info.ekey, size))
                    plan.add([DownloadEntry(encoding_info.ekey, encoding_info.encoded_size, -20)])
                plan.db.execute("INSERT OR REPLACE INTO metadata VALUES('signature',?)", (signature,))
                plan.db.commit()
            # Well-known metadata is required even when absent from the download set.
            entries = []
            for name, values in build.items():
                if name in {'encoding', 'install', 'download', 'size', 'patch-index', 'vfs-root'} or (name.startswith('vfs-') and not name.endswith('-size')):
                    if len(values) == 2 and all(len(x) == 32 for x in values):
                        ek = bytes.fromhex(values[1])
                        entries.append(DownloadEntry(ek, int(build[name + '-size'][1]), -10))
            _, root_choices = table.lookup(bytes.fromhex(build['root'][0]))
            entries.append(DownloadEntry(root_choices[0].ekey, root_choices[0].encoded_size, -10))
            plan.add(entries)

    def prepare(self):
        self._seed()
        version, cdn = self.client.discover()
        self.snapshot = {'product': self.code, 'region': self.region, 'locale': self.locale,
            'version': version['VersionsName'], 'build_config': version['BuildConfig'],
            'cdn_config': version['CDNConfig'], 'version_record': version, 'cdn_record': cdn}
        self.emit('discovery', force=True, version=self.snapshot['version'])
        build_path = self.client.get_config(version['BuildConfig'])
        cdn_path = self.client.get_config(version['CDNConfig'])
        build = read_config(build_path.read_bytes())
        cdn_config = read_config(cdn_path.read_bytes())
        self.client.configure_archives(cdn_config)
        if self.seed_from:
            for key in cdn_config.get('archive-group', []):
                index = self.seed_from / 'Data/indices' / (key + '.index')
                cached = self.client.cache / 'indexes' / (key + '.index')
                if index.is_file() and not cached.is_file():
                    self.emit('seeding_cdn_index', force=True, index=key)
                    self.client.seed_index(key, index, group=True)
        product_path = self.client.get_product_config(version['ProductConfig'])
        product = json.loads(product_path.read_bytes())
        config = product.get('all', {}).get('config', {})
        if config.get('update_method') != 'ngdp' or config.get('data_dir', 'Data/') != 'Data/':
            raise LibraryError(f'{self.entry.label} is not a supported NGDP client')
        folder = config.get('shared_container_default_subfolder', '')
        if not folder or managed_path(folder) != folder or '/' in folder:
            raise LibraryError(f'{self.entry.label} publishes an unusable folder name')
        # Products come from what Blizzard publishes now, so one could name a folder another
        # client already uses. Installing over it would mix two games' files.
        for other in self.library.clients_dir.iterdir():
            if other.name == self.code or not catalog.is_product(other.name):
                continue
            # A paused update can target a new folder while the installed one is still in use.
            used = {(snapshot or {}).get('folder', '').lower()
                    for snapshot in (self.library.state(other.name), self.library.pending(other.name))}
            if folder.lower() in used:
                raise LibraryError(f'{self.entry.label} would install into {folder}, '
                                   f'which {catalog.get(other.name).label} already uses')
        if self.locale not in config.get('supported_locales', []):
            raise LibraryError(f'{self.entry.label} does not support the locale {self.locale}')
        self.snapshot.update(folder=folder)
        for key, path in [(version['BuildConfig'], build_path), (version['CDNConfig'], cdn_path)]:
            atomic_bytes(self.root / 'Data/config' / key[:2] / key[2:4] / key, path.read_bytes())
        key = self.client.runtime_keyring()
        self.snapshot['keyring'] = key or ''
        if key:
            path = self.client.get_config(key)
            atomic_bytes(self.root / 'Data/config' / key[:2] / key[2:4] / key, path.read_bytes())

        self._fill_plan(self.plan, build, json.dumps(self.snapshot, sort_keys=True),
                        self.region, self.locale, stage='planning')
        self.store.checkpoint()
        total, size, missing, missing_size = self.plan.totals()
        free = shutil.disk_usage(self.root).free
        if free < missing_size + (1 << 30):
            raise LibraryError(f'Not enough free space: about {missing_size / 1e9 + 1:.0f} GB are needed')
        atomic_json(self.client_dir / 'pending.json', self.snapshot | {'phase': 'downloading'})
        self.emit('planned', force=True, objects=total, encoded_bytes=size,
                  missing_objects=missing, missing_bytes=missing_size)
        return self.snapshot | {'objects': total, 'encoded_bytes': size,
                                'missing_objects': missing, 'missing_bytes': missing_size}

    def transfer(self):
        total, size, missing, missing_size = self.plan.totals()
        completed, done_bytes = total - missing, size - missing_size
        seeded = fetched = 0
        if self.source and missing:
            with closing(self.source.iter_import_candidates(e.ekey for e in self.plan.entries(missing=True))) as candidates:
                for key, location in candidates:
                    if self.store.has(key):
                        continue
                    try:
                        encoded = self.source.read(key)
                        self.store.put(key, encoded)
                    except (KeyError, StoreError, OSError):
                        continue
                    seeded += 1; completed += 1; done_bytes += len(encoded)
                    if seeded % 2048 == 0:
                        self.store.checkpoint()
                    self.check_cancelled()
                    self.emit('seeding', done=completed, total=total, bytes=done_bytes, total_bytes=size)
            self.store.checkpoint()
        missing = self.plan.totals()[2]
        if missing:
            self.emit('cdn_indexes', force=True, missing_objects=missing)
            self.client.prepare_indexes(lambda done, count: self.emit('cdn_indexes', force=done == count,
                                                                         done=done, total=count))
            base_bytes = received = done_bytes
            def progress(amount):
                nonlocal received
                received = base_bytes + amount
                # Arriving bytes drive the speed only. Progress counts stored, verified objects.
                self.emit('downloading', done=completed, total=total, bytes=done_bytes,
                          received=max(received, done_bytes), total_bytes=size)
            self.emit('downloading', force=True, done=completed, total=total,
                      bytes=done_bytes, total_bytes=size)
            try:
                with closing(self.client.fetch_many(self.plan.entries(missing=True), as_bytes=True,
                             progress=progress, cancelled=self.cancelled)) as downloads:
                    for entry, payload in downloads:
                        self.store.put(entry.ekey, payload)
                        fetched += 1; completed += 1; done_bytes += entry.encoded_size
                        if fetched % 256 == 0:
                            self.store.checkpoint()
                        self.check_cancelled()
                        self.emit('downloading', done=completed, total=total, bytes=done_bytes,
                                  received=max(received, done_bytes), total_bytes=size)
            except TransferCancelled as error:
                self.store.checkpoint()
                raise Cancelled('Paused') from error
            self.emit('downloading', force=True, done=completed, total=total,
                      bytes=done_bytes, total_bytes=size)
        self.store.checkpoint()
        if self.plan.totals()[2]:
            raise LibraryError('Required content is still missing; the client was not activated')
        self.emit('download_complete', force=True, seeded_objects=seeded, downloaded_objects=fetched, objects=total)
        return seeded, fetched

    def activate(self):
        self.library.reject_symlinks()
        self.check_cancelled()
        if self.library.running(self.code):
            raise LibraryError(f'Close {self.entry.label} before its files are replaced')
        staging = self.client_dir / 'staging' / self.snapshot['build_config']
        if staging.is_symlink() or not staging.resolve().is_relative_to(self.root):
            raise LibraryError('The staging directory must stay inside this installation')
        staging.mkdir(parents=True, exist_ok=True)
        files = list(self.plan.db.execute('SELECT path,ckey,ekey,size FROM files ORDER BY path'))
        for i, (relative, ck, ek, size) in enumerate(files):
            destination = contained(staging, relative)
            if not (destination.is_file() and destination.stat().st_size == size and md5_file(destination) == ck):
                destination.parent.mkdir(parents=True, exist_ok=True)
                decode_blte(self.store.read(ek), destination, ek, ck, size)
            if relative.lower().endswith(('.exe', '.dll')):
                destination.chmod(0o755)
            self.emit('materializing', done=i + 1, total=len(files))
            self.check_cancelled()      # each file is kept in staging for the resume
        # The last chance to stop before the published indexes change.
        self.check_cancelled()
        if not self.may_publish():
            self.store.checkpoint()
            raise Cancelled('Paused')
        self.store.publish()
        atomic_json(self.client_dir / 'pending.json', self.snapshot | {'phase': 'activating'})
        game = self.root / self.snapshot['folder']
        game.mkdir(exist_ok=True)
        for relative, ck, ek, size in files:
            source = contained(staging, relative)
            destination = contained(game, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.exists():
                os.replace(source, destination)
                system.sync_directory(destination.parent)
            elif not (destination.is_file() and destination.stat().st_size == size and md5_file(destination) == ck):
                raise LibraryError('Interrupted activation needs to be resumed before launch')
        shutil.rmtree(self.client_dir / 'staging', ignore_errors=True)
        atomic_bytes(game / '.flavor.info', flavor_info(self.code))
        preferences = contained(game, 'WTF/Config.wtf')
        if not preferences.exists():
            atomic_bytes(preferences, default_preferences(self.region, self.locale))
        total, size = self.plan.totals()[:2]
        state = self.snapshot | {'complete': True, 'objects': total, 'encoded_bytes': size,
            'managed_files': [x[0] for x in files], 'updated_at_unix': int(time.time())}
        self.library.write_build_info(replacing=state)
        atomic_json(self.client_dir / 'state.json', state)
        (self.client_dir / 'pending.json').unlink(missing_ok=True)
        for name in ('installed-plan.sqlite', 'installed-plan.sqlite-wal', 'installed-plan.sqlite-shm'):
            (self.client_dir / name).unlink(missing_ok=True)
        system.sync_directory(self.client_dir)
        self.emit('complete', force=True, version=state['version'], objects=total, encoded_bytes=size)
        return state

    def run(self):
        atomic_json(self.client_dir / 'pending.json', {'phase': 'preparing', 'product': self.code,
            'region': self.region, 'locale': self.locale})
        self.prepare()
        seeded, downloaded = self.transfer()
        return self.activate() | {'seeded_objects': seeded, 'downloaded_objects': downloaded}

    def verify(self, *, deep=True):
        state = self.library.state(self.code)
        if not state:
            raise LibraryError(f'{self.entry.label} is not installed')
        plan = self._installed_plan(state)
        try:
            return self._verify(state, plan, deep=deep)
        finally:
            if plan is not self.plan:
                plan.close()

    @staticmethod
    def _describes(plan: Plan, state: dict) -> bool:
        row = plan.db.execute("SELECT value FROM metadata WHERE name='signature'").fetchone()
        try:
            planned = json.loads(row[0]) if row else {}
        except ValueError:
            return False
        return all(planned.get(name) == state.get(name) for name in ('build_config', 'region', 'locale'))

    def _installed_plan(self, state: dict) -> Plan:
        """The plan of the installed build.

        An update plans the new build into plan.sqlite before it downloads, so while it is
        unfinished the installed build gets a plan of its own, made from local content.
        """
        if self._describes(self.plan, state):
            return self.plan
        plan = Plan(self.client_dir / 'installed-plan.sqlite', self.store.inventory_path)
        try:
            if not self._describes(plan, state):
                key = state['build_config']
                path = self.root / 'Data/config' / key[:2] / key[2:4] / key
                if not path.is_file() or md5_file(path).hex() != key:
                    path = self.client.get_config(key)
                signature = json.dumps({name: state.get(name) for name in ('product', 'build_config', 'region', 'locale')},
                                       sort_keys=True)
                self._fill_plan(plan, read_config(path.read_bytes()), signature, state['region'], state['locale'],
                                stage=None)
            return plan
        except BaseException:
            plan.close()
            raise

    def _verify(self, state: dict, plan: Plan, *, deep: bool) -> dict:
        count, bad = 0, []
        for entry in plan.entries(physical=True):
            try:
                if not self.store.has(entry.ekey):
                    raise StoreError('missing object')
                if deep:
                    self.store.verify(entry.ekey)
            except (StoreError, KeyError, OSError) as error:
                bad.append({'ekey': entry.ekey.hex(), 'error': str(error)})
            count += 1
            if count % 4096 == 0:
                self.check_cancelled()
            self.emit('verifying', done=count, errors=len(bad))
        game = self.root / state['folder']
        for relative, ck, ek, size in plan.db.execute('SELECT path,ckey,ekey,size FROM files'):
            path = contained(game, relative)
            if not path.is_file() or path.stat().st_size != size or md5_file(path) != ck:
                bad.append({'path': relative, 'error': 'installed file checksum or size mismatch'})
        try:
            if (game / '.flavor.info').read_bytes() != flavor_info(self.code):
                raise StoreError(f'game flavor selection is not {self.code}')
        except (OSError, StoreError) as error:
            bad.append({'path': f"{state['folder']}/.flavor.info", 'error': str(error)})
        try:
            self.store.verify_indexes()
        except (OSError, StoreError) as error:
            bad.append({'path': 'Data/data/*.idx', 'error': str(error)})
        try:
            expected = build_info([self.library.state(code) for code in self.library.installed()])
            if (self.root / '.build.info').read_bytes() != expected:
                raise StoreError('active build selection differs from the installed clients')
        except (OSError, StoreError) as error:
            bad.append({'path': '.build.info', 'error': str(error)})
        keys = [state['build_config'], state['cdn_config']]
        keyring = state.get('keyring') or state['version_record'].get('KeyRing')
        if keyring:
            keys.append(keyring)
        for key in keys:
            path = self.root / 'Data/config' / key[:2] / key[2:4] / key
            try:
                if md5_file(path).hex() != key:
                    raise StoreError('build configuration checksum mismatch')
            except (OSError, StoreError) as error:
                bad.append({'path': path.relative_to(self.root).as_posix(), 'error': str(error)})
        return {'client': self.code, 'verified_objects': count, 'ok': not bad, 'errors': bad}


def update_available(library: Library, code: str, client: CDNClient) -> bool:
    """Compare the installed build and keyring with the published ones."""
    state = library.state(code)
    if state is None:
        return False
    version, _ = client.discover()
    if version['BuildConfig'] != state['build_config'] or version['CDNConfig'] != state['cdn_config']:
        return True
    return (client.runtime_keyring() or '') != (state.get('keyring') or '')
