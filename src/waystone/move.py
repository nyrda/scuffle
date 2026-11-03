"""Move the installation folder to another place without losing it to a crash.

Everything moves: every client's game folder, the shared CASC store and
Waystone's state in .waystone. Nothing inside the folder names its own
location, so no file changes, only the saved setting.

A journal in Waystone's data folder names the old and the new place. It lives
outside the installation, since a rename takes the installation's folder with
it. The places themselves carry a marker, .waystone/move.json, with the
journal's token. Library.locked() refuses a folder with a marker, so no job
runs in either place while it moves, and recovery deletes only what carries
its own token.

On one file system the folder moves with one rename. Across file systems it is
copied, the copy is checked, the journal and then the setting switch to it, and
only then is the old folder deleted. Startup finishes a switched move and undoes
any other from the journal, so one complete folder always exists and the
setting ends up pointing at it.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid
from typing import Callable

from . import catalog, settings as settings_module, system
from .library import Cancelled, Library, LibraryError, atomic_json, mount_points, remove_tree
from .ngdp.store import STATE_DIR, CascStore, StoreError

MARKER = f'{STATE_DIR}/move.json'
# Recreated where they are needed. Locks and SQLite's shared memory index are never copied.
TRANSIENT = {f'{STATE_DIR}/library.lock', f'{STATE_DIR}/store.lock', MARKER}
SPARE = 1 << 30           # room a copy leaves on the new drive, as an install does
CHUNK = 8 << 20
HEAD = 1 << 20            # the part of a large file compared at each end
WHOLE = 4 << 20           # files up to this size are compared whole


def journal_path() -> Path:
    return settings_module.data_dir() / 'move.json'


def size_words(value: int) -> str:
    return f'{value / 1e9:.1f} GB' if value >= 1e9 else f'{value / 1e6:.0f} MB'


def same_place(a, b) -> bool:
    """Whether two paths name the same folder, spelled as the file system compares names."""
    if not a or not b:
        return False
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def inside(path: Path, folder: Path) -> bool:
    """Whether path is folder or lies within it, also through another name for an existing folder."""
    if Path(os.path.normcase(path)).is_relative_to(os.path.normcase(folder)):
        return True
    # A different spelling on a case-insensitive drive, or a bind mount of the folder.
    for parent in [path, *path.parents]:
        try:
            if parent.exists() and os.path.samefile(parent, folder):
                return True
        except OSError:
            continue
    return False


def same_file_system(a: Path, b: Path) -> bool:
    """Whether a rename can carry a to b. Linux bind mounts share a device but not renames."""
    existing = next(p for p in [b, *b.parents] if p.exists())
    if os.stat(a).st_dev != os.stat(existing).st_dev:
        return False
    try:
        points = mount_points()
    except LibraryError:
        return True         # the rename itself will tell
    def mount(path: Path) -> Path:
        real = Path(os.path.realpath(path))
        return max((p for p in points if real.is_relative_to(p)), key=lambda p: len(p.parts), default=Path('/'))
    return mount(a) == mount(existing)


# Checks


def check_destination(library: Library, destination) -> Path:
    """The new place, or a refusal in plain words."""
    raw = Path(destination).expanduser()
    if not raw.is_absolute():
        raise LibraryError('Choose a full path for the new folder, such as /games/Waystone.')
    target = Path(os.path.abspath(raw))
    if target.is_symlink():
        raise LibraryError('The new folder is a symbolic link. Choose the folder it points to instead.')
    target = target.resolve()
    source = library.root
    if inside(target, source):
        raise LibraryError('The new folder is inside the installation folder. Choose a folder outside it.')
    if inside(source, target):
        raise LibraryError('The installation folder is inside the new folder. Choose an empty folder elsewhere.')
    try:
        if target.exists():
            if not target.is_dir():
                raise LibraryError('The new place is a file, not a folder.')
            if any(target.iterdir()):
                raise LibraryError('The new folder is not empty. Choose an empty folder, or a new one.')
        elif not target.parent.is_dir():
            raise LibraryError(f'{target.parent} does not exist. Choose a folder in a place that exists.')
    except OSError as error:
        raise LibraryError(f'Waystone cannot read {target}: {error.strerror}') from None
    writable = target if target.exists() else target.parent
    if not os.access(writable, os.W_OK):
        raise LibraryError(f'Waystone may not write to {writable}.')
    return target


def refuse_busy(library: Library, has_jobs: Callable[[], bool]) -> None:
    if library.uninstalling():
        raise LibraryError('Wait until the uninstall has finished, then move the folder.')
    if has_jobs():
        raise LibraryError('Finish or remove every download before moving the installation folder.')
    processes = system.running_executables()
    for code in library.installed():
        if library.running(code, processes):
            raise LibraryError(f'Close {catalog.get(code).label} before moving the installation folder.')


def preview(library: Library, destination) -> dict:
    """What moving to destination takes, for the confirmation, or why it cannot happen."""
    try:
        target = check_destination(library, destination)
        same = same_file_system(library.root, target)
        _, files = survey(library.root)
        free = shutil.disk_usage(target if target.exists() else target.parent).free
    except (LibraryError, OSError) as error:
        return {'destination': str(destination), 'problem': str(error)}
    total = sum(files.values())
    problem = '' if same or free >= total + SPARE else (
        f'The new drive has {size_words(free)} free, but the installation needs {size_words(total + SPARE)} there.')
    return {'destination': str(target), 'same_drive': same, 'size': total, 'free': free, 'problem': problem}


def survey(root: Path) -> tuple[list[str], dict[str, int]]:
    """Every folder and file to move, relative to root, with each file's size.

    Links, junctions, other file systems and special files are refused, as uninstall
    refuses them. A move would copy what they point to, or delete it afterwards.
    """
    root = Path(root)
    device = root.lstat().st_dev
    try:
        mounted = [p for p in mount_points() if p != root and p.is_relative_to(root)]
    except LibraryError:
        raise LibraryError('Linux mount information is unreadable, so Waystone cannot check the folder '
                           'and does not move it.') from None
    if mounted:
        raise LibraryError(f'Another file system is mounted at {mounted[0]}. Unmount it, then move the folder.')
    folders, files = [], {}

    def unreadable(error):
        raise LibraryError(f'Waystone cannot read {error.filename}, so it does not move the folder.') from error
    for directory, names, filenames in os.walk(root, followlinks=False, onerror=unreadable):
        for name in sorted(names) + sorted(filenames):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or path.is_junction():
                raise LibraryError(f'The installation folder holds a link at {relative}. '
                                   'Waystone does not move links. Remove it, then try again.')
            if info.st_dev != device or os.path.ismount(path):
                raise LibraryError(f'Another drive is mounted at {relative}. Unmount it, then move the folder.')
            if stat.S_ISDIR(info.st_mode):
                folders.append(relative)
            elif not stat.S_ISREG(info.st_mode):
                raise LibraryError(f'{relative} is not a regular file, so Waystone does not move it.')
            elif relative not in TRANSIENT and not (relative.startswith(f'{STATE_DIR}/') and relative.endswith('-shm')):
                files[relative] = info.st_size
    return sorted(folders), files       # sorted, parents first, the same for any listing order


# Journal and markers


def read_journal() -> dict | None:
    path = journal_path()
    try:
        if path.is_symlink():
            raise ValueError
        record = json.loads(path.read_text())
        valid = (isinstance(record, dict) and isinstance(record.get('token'), str)
                 and re.fullmatch('[0-9a-f]{32}', record['token'])
                 and all(isinstance(record.get(k), str) and Path(record[k]).is_absolute()
                         for k in ('source', 'destination'))
                 and record.get('mode') in ('rename', 'copy')
                 and record.get('phase') in ('moving', 'copying', 'switched', 'deleting')
                 and isinstance(record.get('created'), bool))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        valid = False
    if not valid:
        raise LibraryError(f'The record of an unfinished move is damaged: {path}. Nothing was changed.')
    return record


def write_journal(record: dict) -> None:
    atomic_json(journal_path(), record)


def drop_journal() -> None:
    path = journal_path()
    path.unlink(missing_ok=True)
    _sweep_temporary(path)
    system.sync_directory(path.parent)


def _sweep_temporary(path: Path) -> None:
    """Remove what an atomic write of path left when a crash cut it short."""
    for stray in path.parent.glob(f'.{path.name}?*'):
        if stray.is_file() and not stray.is_symlink():
            stray.unlink(missing_ok=True)


def marker_token(root: Path) -> str | None:
    """The token of the move a folder belongs to, if it carries one."""
    path = Path(root) / MARKER
    try:
        if (Path(root) / STATE_DIR).is_symlink() or path.is_symlink() or not path.is_file():
            return None
        token = json.loads(path.read_text()).get('token')
    except (OSError, ValueError, AttributeError):
        return None
    return token if isinstance(token, str) else None


def put_marker(root: Path, record: dict) -> None:
    atomic_json(Path(root) / MARKER, {key: record[key] for key in ('token', 'source', 'destination')})


def clear_marker(root: Path) -> None:
    state = Path(root) / STATE_DIR
    if state.is_dir() and not state.is_symlink():
        (Path(root) / MARKER).unlink(missing_ok=True)
        _sweep_temporary(Path(root) / MARKER)
        system.sync_directory(state)


def save_root(settings, root: Path) -> None:
    """Point the settings at root. When saving fails, they keep naming the folder the file names."""
    previous, settings.root = settings.root, str(root)
    try:
        settings.save()
    except BaseException:
        settings.root = previous
        raise


# Moving


def move(library: Library, destination, settings, *, progress=None, cancelled=None,
         has_jobs: Callable[[], bool] = lambda: False) -> dict:
    """Move the whole installation to destination and point settings at it.

    Runs on the download worker, with no job in the queue. Cancelling stops a copy
    before the switch and removes what it made. The old folder stays untouched then.
    """
    emit = progress or (lambda event: None)
    cancelled = cancelled or (lambda: False)
    with exclusive():
        record = read_journal()
        if record is not None:
            # An earlier move is settled first. One that cannot be blocks a new one.
            _resolve(record, settings, emit=emit)
            if journal_path().exists():
                raise LibraryError('An earlier move of the installation folder is unfinished. '
                                   'Restart Waystone to finish it.')
        emit({'stage': 'checking'})
        with library.locked():
            source = library.root
            target = check_destination(library, destination)
            refuse_busy(library, has_jobs)
            folders, files = survey(source)
            if cancelled():
                raise Cancelled('Cancelled')
            # A mount point cannot be renamed, or be renamed onto. Its content is copied.
            copy = os.path.ismount(source) or (target.exists() and os.path.ismount(target))
            record = {'token': uuid.uuid4().hex, 'source': str(source), 'destination': str(target),
                      'mode': 'copy' if copy else 'rename', 'phase': 'copying' if copy else 'moving',
                      'created': not target.exists()}
            write_journal(record)
            put_marker(source, record)
        # From here the marker keeps every other operation out. The lock is released, since
        # Windows renames or deletes no folder while a file inside it is open.
        try:
            if record['mode'] == 'rename':
                if _rename(source, target):
                    return _finish_rename(record, settings, emit)
                record.update(mode='copy', phase='copying')
                write_journal(record)
            return _copy(record, folders, files, settings, emit, cancelled)
        except BaseException:
            # Before the switch the move is undone, after it finished. The journal on disk
            # decides, not this copy of it, which a failed write can leave ahead. The next
            # start does the same if this fails too.
            try:
                persisted = read_journal()
                if persisted is not None:
                    _resolve(persisted, settings, emit=emit)
            except (LibraryError, OSError, ValueError):
                pass
            raise


@contextmanager
def exclusive():
    """One move or recovery at a time, also across several Waystone processes."""
    path = journal_path().with_suffix('.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        try:
            system.lock(handle, blocking=False)
        except system.LockBusy:
            raise LibraryError('Another Waystone is moving the installation folder.') from None
        try:
            yield
        finally:
            system.unlock(handle)


def _cross_device(error: OSError) -> bool:
    # Windows reports another drive as ERROR_NOT_SAME_DEVICE.
    return error.errno == errno.EXDEV or getattr(error, 'winerror', None) == 17


def _rename(source: Path, target: Path) -> bool:
    """Rename the folder in one step. False when the new place is on another file system."""
    existed = target.exists()
    for attempt in range(5):
        try:
            if existed and system.WINDOWS:
                target.rmdir()      # Windows renames only onto a free name. The folder is empty.
            os.rename(source, target)
        except OSError as error:
            if existed and system.WINDOWS and not target.exists():
                target.mkdir()
            if _cross_device(error):
                return False
            if isinstance(error, PermissionError) and system.WINDOWS and attempt < 4:
                time.sleep(0.25)    # a file Waystone itself just read can still be open
                continue
            if isinstance(error, PermissionError):
                raise LibraryError('A file in the installation folder is open in another program. '
                                   'Close it, then try again.') from None
            raise
        system.sync_directory(target.parent)
        system.sync_directory(source.parent)
        return True
    return False    # not reached: the last attempt returns or raises


def _finish_rename(record: dict, settings, emit) -> dict:
    source, target = Path(record['source']), Path(record['destination'])
    emit({'stage': 'switching'})
    try:
        save_root(settings, target)
    except OSError as error:
        # The setting still names the old place, so the folder goes back there.
        os.rename(target, source)
        system.sync_directory(source.parent)
        system.sync_directory(target.parent)
        raise LibraryError(f'The settings could not be saved, so the folder stays where it was. '
                           f'{error.strerror or error}') from None
    clear_marker(target)
    drop_journal()
    return {'moved': True, 'copied': False, 'source': str(source), 'destination': str(target), 'leftover': ''}


def _copy(record: dict, folders: list[str], files: dict[str, int], settings, emit, cancelled) -> dict:
    source, target = Path(record['source']), Path(record['destination'])
    total = sum(files.values())
    free = shutil.disk_usage(target if target.exists() else target.parent).free
    if free < total + SPARE:
        raise LibraryError(f'The new drive has {size_words(free)} free, but the installation needs '
                           f'{size_words(total + SPARE)} there.')
    if record['created']:
        target.mkdir()
        system.sync_directory(target.parent)
    put_marker(target, record)          # first, so recovery knows the copy as its own
    done, last = 0, 0.0

    def advance(amount: int) -> None:
        nonlocal done, last
        done += amount
        now = time.monotonic()
        if now - last >= 0.25 or done == total:
            last = now
            emit({'stage': 'copying', 'bytes': done, 'total_bytes': total})
        if cancelled():
            raise Cancelled('Cancelled')

    emit({'stage': 'copying', 'bytes': 0, 'total_bytes': total})
    for folder in folders:
        (target / folder).mkdir(exist_ok=True)
    # The identity last, so an unfinished copy is never taken for an installation.
    identity = f'{STATE_DIR}/identity.json'
    for relative in sorted(files, key=lambda name: name == identity):
        _copy_file(source / relative, target / relative, advance)
        if cancelled():
            raise Cancelled('Cancelled')
    for folder in sorted(folders, key=lambda name: -name.count('/')):
        system.sync_directory(target / folder)
    system.sync_directory(target)
    emit({'stage': 'verifying'})
    _verify(source, target, folders, files, cancelled)
    refuse_busy(Library(source), lambda: False)
    if cancelled():
        raise Cancelled('Cancelled')
    # The switch: the journal first, then the setting. From here the copy is the installation.
    emit({'stage': 'switching'})
    record['phase'] = 'switched'
    write_journal(record)
    try:
        save_root(settings, target)
    except OSError as error:
        record['phase'] = 'copying'
        write_journal(record)
        raise LibraryError(f'The settings could not be saved, so the installation stays where it was. '
                           f'{error.strerror or error}') from None
    emit({'stage': 'removing_old'})
    record['phase'] = 'deleting'         # from here the old folder may be partly gone
    write_journal(record)
    leftover = _delete_marked(source, record['token'], keep_folder=False)
    # The new folder stays closed to other processes until the old one is gone. What cannot
    # be deleted waits for the next start, and does not keep the new folder closed.
    clear_marker(target)
    if not leftover:
        drop_journal()
    return {'moved': True, 'copied': True, 'source': str(source), 'destination': str(target), 'leftover': leftover}


def _copy_file(source: Path, target: Path, advance) -> None:
    with source.open('rb') as reader, target.open('xb') as writer:      # never over an existing file
        while chunk := reader.read(CHUNK):
            writer.write(chunk)
            advance(len(chunk))
        writer.flush()
        os.fsync(writer.fileno())
    shutil.copystat(source, target, follow_symlinks=False)      # times and permissions, as before


def _verify(source: Path, target: Path, folders: list[str], files: dict[str, int], cancelled) -> None:
    """Sizes everywhere, the store's records whole, and both ends of every large file."""
    if survey(source) != (folders, files):
        raise LibraryError('The installation folder changed while it was copied. Try again.')
    if survey(target) != (folders, files):
        raise LibraryError('The copy is incomplete. Check the new drive, then try again.')
    for relative, size in files.items():
        if cancelled():
            raise Cancelled('Cancelled')
        whole = size <= WHOLE or relative.startswith(f'{STATE_DIR}/') or relative.endswith('.idx')
        if not _same_bytes(source / relative, target / relative, size, whole):
            raise LibraryError(f'The copy of {relative} differs from the original. Check the new drive, then try again.')
    if (target / STATE_DIR / 'store.sqlite').is_file():
        # The store's own check: it opens, recovers its tails, and the newest object of every
        # segment reads back intact. A copy cut short shows there first.
        try:
            with CascStore(target) as store:
                newest = {}
                for ekey, location in store.iter_entries():
                    if location.offset >= newest.get(location.archive, (b'', -1))[1]:
                        newest[location.archive] = (ekey, location.offset)
                for ekey, _ in newest.values():
                    store.verify(ekey)
        except (StoreError, OSError, KeyError) as error:
            raise LibraryError(f'The copied content failed its check: {error}') from None


def _same_bytes(a: Path, b: Path, size: int, whole: bool) -> bool:
    with a.open('rb') as left, b.open('rb') as right:
        if whole:
            while True:
                chunk = left.read(CHUNK)
                if chunk != right.read(CHUNK):
                    return False
                if not chunk:
                    return True
        for offset in (0, max(0, size - HEAD)):
            left.seek(offset)
            right.seek(offset)
            if left.read(HEAD) != right.read(HEAD):
                return False
        return True


# Recovery


def recover(settings, *, delete: bool = True, progress=None) -> dict | None:
    """Finish or undo an interrupted move from its journal, before the library opens.

    With delete False, only the setting and the markers are settled, which is quick, so the
    window can open the right folder at once. The worker deletes the rest afterwards.
    """
    with exclusive():
        record = read_journal()
        if record is None:
            return None
        return _resolve(record, settings, delete=delete, emit=progress or (lambda event: None))


def _resolve(record: dict, settings, *, delete: bool = True, emit=lambda event: None) -> dict:
    source, target, token = Path(record['source']), Path(record['destination']), record['token']
    if record['mode'] == 'rename':
        # One rename, so the marker shows on which side the folder is.
        if marker_token(target) == token:
            switched = True
        elif marker_token(source) == token:
            switched = False
        else:
            drop_journal()          # the marker was never written, or already removed
            return {'recovered': 'nothing', 'source': str(source), 'destination': str(target), 'leftover': ''}
    else:
        switched = record['phase'] in ('switched', 'deleting')
        if switched and not Library(target).exists():
            # The checked copy is gone or was replaced since. Before deleting began the old
            # folder is whole, so the move is undone. After it, nothing more is deleted.
            if record['phase'] == 'deleting':
                raise LibraryError(f'The new installation folder {target} is gone, and the old one '
                                   f'at {source} is partly removed. Waystone deletes nothing more.')
            switched = False
    result = {'recovered': 'finished' if switched else 'undone', 'source': str(source),
              'destination': str(target), 'leftover': ''}
    if switched:
        # A setting the user pointed elsewhere since is theirs. Only the old place is replaced.
        if not same_place(settings.root, target) and (not settings.root or same_place(settings.root, source)):
            save_root(settings, target)
        if record['mode'] == 'rename':
            clear_marker(target)
            drop_journal()
            return result
        if not delete:
            return result | {'pending': True}
        emit({'stage': 'removing_old'})
        if record['phase'] != 'deleting':
            write_journal(record | {'phase': 'deleting'})
        result['leftover'] = _delete_marked(source, token, keep_folder=False)
        clear_marker(target)
    else:
        if same_place(settings.root, target):
            save_root(settings, source)
        clear_marker(source)
        partial = record['mode'] == 'copy' and marker_token(target) == token
        if partial and not delete:
            return result | {'pending': True}
        if partial:
            emit({'stage': 'removing_copy'})
            result['leftover'] = _delete_marked(target, token, keep_folder=not record['created'])
        elif record['mode'] == 'copy' and _marker_debris(target):
            # Interrupted while its marker was written: only that folder and its temporary file.
            for path in _marker_debris(target):
                path.unlink() if path.is_file() else path.rmdir()
            if record['created']:
                _remove_folder(target)
        elif record['created'] and target.is_dir() and not target.is_symlink() and not any(target.iterdir()):
            target.rmdir()      # made, then interrupted before its marker
        elif not record['created'] and not target.exists() and target.parent.is_dir():
            target.mkdir()      # the user's empty folder, moved out of a Windows rename's way
    if not result['leftover']:
        drop_journal()
    return result


def _marker_debris(target: Path) -> list[Path]:
    """What writing the marker into a new folder leaves when it is cut short, deepest first, or []."""
    state = target / STATE_DIR
    try:
        if target.is_symlink() or state.is_symlink() or [p.name for p in target.iterdir()] != [STATE_DIR]:
            return []
        inside = list(state.iterdir())
    except OSError:
        return []
    # atomic_bytes writes '.move.json' plus a random suffix next to the marker, then renames it.
    if not all(p.is_file() and not p.is_symlink() and p.name.startswith('.move.json') for p in inside):
        return []
    return inside + [state]


def _delete_marked(root: Path, token: str, *, keep_folder: bool) -> str:
    """Delete a folder that carries this move's marker, the marker last. What stays, or ''."""
    if marker_token(root) != token:
        # Already deleted up to the marker, or never this move's. Only empty folders go then.
        state = root / STATE_DIR
        if root.is_dir() and not root.is_symlink() and [p.name for p in root.iterdir()] == [STATE_DIR] \
                and state.is_dir() and not state.is_symlink() and not any(state.iterdir()):
            _remove_folder(state)
        if not keep_folder and root.is_dir() and not root.is_symlink() and not any(root.iterdir()):
            _remove_folder(root)
        return ''
    try:
        survey(root)            # no links or mounts crept in since
        library = Library(root)
        if any(library.running(code) for code in library.installed()):
            return str(root)    # a game started from it after all. The next start tries again
    except (LibraryError, OSError, ValueError):
        return str(root)
    state = root / STATE_DIR
    entries = [p for p in root.iterdir() if p.name != STATE_DIR] + \
              [p for p in state.iterdir() if p.name != 'move.json']
    if not all(_remove(path) for path in entries):
        return str(root)
    clear_marker(root)
    _remove_folder(state)
    if not keep_folder:
        _remove_folder(root)
    return ''


def _remove(path: Path) -> bool:
    if path.is_dir() and not path.is_symlink():
        return remove_tree(path)
    try:
        path.unlink()
    except PermissionError:
        try:
            path.chmod(stat.S_IWRITE | stat.S_IREAD)      # a read-only file on Windows
            path.unlink()
        except OSError:
            return False
    except FileNotFoundError:
        pass
    except OSError:
        return False
    system.sync_directory(path.parent)
    return True


def _remove_folder(path: Path) -> None:
    try:
        path.rmdir()
        system.sync_directory(path.parent)
    except OSError:
        pass        # a mount point, or something the user put there since
