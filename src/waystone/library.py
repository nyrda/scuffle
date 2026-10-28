"""One installation folder that holds every installed client.

All clients share the CASC store under Data/. Each client has its own plan,
state and game folder under .waystone/clients/<code>/ and <root>/<folder>/.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import time
from typing import Callable, Iterator

from . import catalog, system
from .ngdp.cdn import CDNClient, TransferCancelled
from .ngdp.formats import EncodingTable, decode_blte, read_config, read_download, read_install
from .ngdp.models import DownloadEntry
from .ngdp.store import STATE_DIR, CascSource, CascStore, StoreError

IDENTITY = {'format': 1, 'kind': 'waystone'}
BUILD_INFO_COLUMNS = [
    'Branch!STRING:0', 'Active!DEC:1', 'Build Key!HEX:16', 'CDN Key!HEX:16', 'Install Key!HEX:16',
    'IM Size!DEC:4', 'CDN Path!STRING:0', 'CDN Hosts!STRING:0', 'CDN Servers!STRING:0', 'Tags!STRING:0',
    'Armadillo!STRING:0', 'Last Activated!STRING:0', 'Version!STRING:0', 'KeyRing!HEX:16', 'Product!STRING:0']


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


def md5_file(path: Path) -> bytes:
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'md5').digest()


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
        names = [STATE_DIR, f'{STATE_DIR}/metadata', f'{STATE_DIR}/clients', 'Data', 'Data/data', 'Data/config']
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
            raise LibraryError(f'{self.root} is not a Waystone installation')
        try:
            with system.locked_file(self.state_dir / 'library.lock', blocking=False):
                yield self
        except system.LockBusy:
            raise LibraryError('Another Waystone operation is using this installation') from None

    # Clients

    def client_dir(self, code: str) -> Path:
        catalog.get(code)
        return self.clients_dir / code

    def state(self, code: str) -> dict | None:
        try:
            return json.loads((self.client_dir(code) / 'state.json').read_text())
        except FileNotFoundError:
            return None

    def installed(self) -> list[str]:
        if not self.clients_dir.is_dir():
            return []
        return sorted(p.name for p in self.clients_dir.iterdir()
                      if p.name in catalog.BY_CODE and (p / 'state.json').is_file())

    def pending(self, code: str) -> dict | None:
        try:
            return json.loads((self.client_dir(code) / 'pending.json').read_text())
        except FileNotFoundError:
            return None

    def game_dir(self, code: str) -> Path | None:
        state = self.state(code)
        return self.root / state['folder'] if state else None

    def executable(self, code: str) -> Path | None:
        folder = self.game_dir(code)
        return folder / 'Wow.exe' if folder else None

    def running(self, code: str) -> bool:
        folder = self.game_dir(code)
        if folder is None:
            return False
        executable = str(folder / 'Wow.exe').lower()
        for _, command, cwd in system.running_executables():
            text = command.lower()
            if 'wow.exe' not in text:
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
        for code in self.installed():
            if self.pending(code):
                raise LibraryError(f'Finish the {catalog.get(code).label} update before cleaning up')
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
                 cancelled: Callable[[], bool] | None = None):
        self.library, self.root = library, library.root
        self.code, self.entry = code, catalog.get(code)
        self.client, self.locale, self.region = client, locale, region
        self.seed_from = seed_from
        self.progress = progress or (lambda event: None)
        self.cancelled = cancelled or (lambda: False)
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
        product = json.loads(product_path.read_text())
        config = product.get('all', {}).get('config', {})
        if config.get('update_method') != 'ngdp' or config.get('data_dir', 'Data/') != 'Data/':
            raise LibraryError(f'{self.entry.label} is not a supported NGDP client')
        folder = config.get('shared_container_default_subfolder', '')
        if not folder or managed_path(folder) != folder or '/' in folder:
            raise LibraryError(f'{self.entry.label} publishes an unusable folder name')
        if self.locale not in config.get('supported_locales', []):
            raise LibraryError(f'{self.entry.label} does not support the locale {self.locale}')
        self.snapshot.update(folder=folder, launch_arguments=list(config.get('launch_arguments') or []))
        for key, path in [(version['BuildConfig'], build_path), (version['CDNConfig'], cdn_path)]:
            atomic_bytes(self.root / 'Data/config' / key[:2] / key[2:4] / key, path.read_bytes())
        key = self.client.runtime_keyring()
        self.snapshot['keyring'] = key or ''
        if key:
            path = self.client.get_config(key)
            atomic_bytes(self.root / 'Data/config' / key[:2] / key[2:4] / key, path.read_bytes())

        def manifest(name):
            ck, ek = (bytes.fromhex(x) for x in build[name])
            decoded, encoded = map(int, build[name + '-size'])
            return self._decoded(name, ck, ek, decoded, encoded)

        encoding = manifest('encoding')
        install = manifest('install')
        download = manifest('download')
        selected = {'Windows', 'x86_64', self.region.upper(), self.locale, 'speech', 'text'}
        signature = json.dumps(self.snapshot, sort_keys=True)
        old = self.plan.db.execute("SELECT value FROM metadata WHERE name='signature'").fetchone()
        with EncodingTable(encoding) as table:
            if not old or old[0] != signature:
                self.emit('planning', force=True)
                self.plan.db.execute('DELETE FROM objects')
                self.plan.db.execute('DELETE FROM files')
                self.plan.db.commit()
                self.plan.add(read_download(download, selected))
                for entry in read_install(install, selected):
                    relative = managed_path(entry.path)
                    size, choices = table.lookup(entry.ckey)
                    if size != entry.decoded_size or not choices:
                        raise LibraryError('Install and encoding metadata disagree')
                    encoding_info = choices[0]
                    duplicate = self.plan.db.execute('SELECT ckey FROM files WHERE lower(path)=lower(?)', (relative,)).fetchone()
                    if duplicate and duplicate[0] != entry.ckey:
                        raise LibraryError('Conflicting selected install file variants')
                    self.plan.db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?)',
                        (relative, entry.ckey, encoding_info.ekey, size))
                    self.plan.add([DownloadEntry(encoding_info.ekey, encoding_info.encoded_size, -20)])
                self.plan.db.execute("INSERT OR REPLACE INTO metadata VALUES('signature',?)", (signature,))
                self.plan.db.commit()
            # Well-known metadata is required even when absent from the download set.
            entries = []
            for name, values in build.items():
                if name in {'encoding', 'install', 'download', 'size', 'patch-index', 'vfs-root'} or (name.startswith('vfs-') and not name.endswith('-size')):
                    if len(values) == 2 and all(len(x) == 32 for x in values):
                        ek = bytes.fromhex(values[1])
                        entries.append(DownloadEntry(ek, int(build[name + '-size'][1]), -10))
            _, root_choices = table.lookup(bytes.fromhex(build['root'][0]))
            entries.append(DownloadEntry(root_choices[0].ekey, root_choices[0].encoded_size, -10))
            self.plan.add(entries)
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
            self.client.prepare_indexes(lambda done, count: self.emit('cdn_indexes', done=done, total=count))
            base_bytes = received = done_bytes
            def progress(amount):
                nonlocal received
                received = base_bytes + amount
                self.emit('downloading', done=completed, total=total,
                          bytes=max(received, done_bytes), total_bytes=size)
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
                        self.emit('downloading', done=completed, total=total,
                                  bytes=max(received, done_bytes), total_bytes=size)
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
            atomic_bytes(preferences, (
                f'SET portal "{self.region.upper()}"\n'
                f'SET textLocale "{self.locale}"\n'
                f'SET audioLocale "{self.locale}"\n'
            ).encode())
        total, size = self.plan.totals()[:2]
        state = self.snapshot | {'complete': True, 'objects': total, 'encoded_bytes': size,
            'managed_files': [x[0] for x in files], 'updated_at_unix': int(time.time())}
        self.library.write_build_info(replacing=state)
        atomic_json(self.client_dir / 'state.json', state)
        (self.client_dir / 'pending.json').unlink(missing_ok=True)
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
        count, bad = 0, []
        for entry in self.plan.entries(physical=True):
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
        for relative, ck, ek, size in self.plan.db.execute('SELECT path,ckey,ekey,size FROM files'):
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
                bad.append({'path': str(path.relative_to(self.root)), 'error': str(error)})
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
