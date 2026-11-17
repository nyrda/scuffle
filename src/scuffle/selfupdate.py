"""Update Scuffle itself from its GitHub releases.

The Windows package and the AppImage can replace themselves. The AppImage is a single file. The
Windows package is Scuffle.exe with its version's folder beside it, so an update unpacks the new
folder next to the old one and replaces only the program. Any other installation, such as the Nix
package, is managed by what installed it and only hears about a release. A release is installed
only when the user asks.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable
import urllib.request
import zipfile

from . import diagnostics, system

API = 'https://api.github.com/repos/nyrda/scuffle/releases/latest'
DOWNLOADS = 'https://github.com/nyrda/scuffle/releases/download/'
PAGES = 'https://github.com/nyrda/scuffle/releases/'
SUMS = 'SHA256SUMS'
CHECK_SECONDS = 24 * 3600
TIMEOUT = 30
TRY_OUT_SECONDS = 180       # a virus scanner may look at a new program before it runs
FINISH_SECONDS = 60         # the old Scuffle may still be exiting when the new one starts
LIMIT = 1 << 20
BLOCK = 1 << 20
VERSION = re.compile(r'v?(\d+)\.(\d+)\.(\d+)')
# A Windows package's version folder, and one an update was unpacking.
FOLDER = re.compile(r'scuffle-\d+\.\d+\.\d+(\.partial)?')


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    page: str
    asset: str = ''             # empty when this installation cannot replace itself
    asset_url: str = ''
    asset_size: int = 0
    sums_url: str = ''


def current() -> str:
    return diagnostics.version()


def parsed(version: str) -> tuple[int, int, int] | None:
    match = VERSION.fullmatch(version)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def program() -> Path | None:
    """The program that is this Scuffle, when it can replace itself."""
    if getattr(sys, 'frozen', False) and system.WINDOWS:
        return Path(sys.executable)
    appimage = os.environ.get('APPIMAGE')
    return Path(appimage) if appimage and Path(appimage).is_file() else None


def can_install() -> bool:
    return program() is not None


def asset_name(version: str) -> str:
    return f'Scuffle-{version}-windows-x64.zip' if system.WINDOWS else f'Scuffle-{version}-x86_64.AppImage'


def version_folder(version: str) -> str:
    """The folder beside Scuffle.exe with the rest of that version. scuffle.spec names it."""
    return f'scuffle-{version}'


def _sibling(path: Path, tag: str) -> Path:
    # Keeps the extension, since Windows runs what it recognises as a program.
    return path.with_name(f'{path.stem}.{tag}{path.suffix}')


def _open(url: str, accept: str = '*/*'):
    request = urllib.request.Request(url, headers={'User-Agent': f'scuffle/{current()}', 'Accept': accept})
    return urllib.request.urlopen(request, timeout=TIMEOUT)     # noqa: S310, the addresses are this module's


def _read(url: str, accept: str = '*/*') -> bytes:
    with _open(url, accept) as response:
        data = response.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise UpdateError('GitHub sent more than a release description')
    return data


def parse_release(data: bytes, installed: str) -> Release | None:
    """The published release when it is newer than installed. Anything unexpected counts as none."""
    release = json.loads(data)
    if not isinstance(release, dict) or release.get('draft') or release.get('prerelease'):
        return None
    tag, page = release.get('tag_name'), release.get('html_url')
    number, have = parsed(tag) if isinstance(tag, str) else None, parsed(installed)
    if number is None or have is None or number <= have or not str(page).startswith(PAGES):
        return None
    version = '.'.join(map(str, number))
    if not can_install():
        return Release(version, page)
    assets = {a.get('name'): a for a in release.get('assets', []) if isinstance(a, dict)}
    asset, sums = assets.get(asset_name(version)), assets.get(SUMS)
    urls = [item.get('browser_download_url') for item in (asset, sums) if item]
    if len(urls) != 2 or not all(isinstance(u, str) and u.startswith(DOWNLOADS) for u in urls):
        return Release(version, page)       # it can still be announced, with its page to get it from
    return Release(version, page, asset['name'], urls[0], int(asset.get('size') or 0), urls[1])


def latest() -> Release | None:
    """The newer release, or None. Raises OSError or ValueError when GitHub cannot say."""
    return parse_release(_read(API, 'application/vnd.github+json'), current())


def _expected_sum(release: Release) -> str:
    for line in _read(release.sums_url).decode(errors='replace').splitlines():
        digest, _, name = line.strip().partition('  ')
        if name == release.asset and re.fullmatch(r'[0-9a-f]{64}', digest):
            return digest
    raise UpdateError(f'The release does not list a checksum for {release.asset}')


def _download(release: Release, target: Path, expected: str, progress, cancelled) -> None:
    digest, received = hashlib.sha256(), 0
    with _open(release.asset_url) as response, open(target, 'wb') as output:
        while block := response.read(BLOCK):
            if cancelled and cancelled():
                raise UpdateError('The update was cancelled')
            received += len(block)
            if received > release.asset_size:
                raise UpdateError('The download is larger than the release says')
            digest.update(block)
            output.write(block)
            if progress:
                progress(received, release.asset_size)
        output.flush()
        os.fsync(output.fileno())
    if received != release.asset_size or digest.hexdigest() != expected:
        raise UpdateError('The download does not match the checksum of the release')
    if not system.WINDOWS:
        target.chmod(0o755)


def _unpack(archive: Path, staged: Path, version: str) -> Path:
    """Unpack the release's version folder beside the running one, and its program as staged.

    The folder is complete before it gets its name, so a folder of that name is never half there.
    """
    name = version_folder(version)
    final, partial = staged.with_name(name), staged.with_name(f'{name}.partial')
    for leftover in (final, partial):       # from an update that was interrupted
        shutil.rmtree(leftover, ignore_errors=True)
    try:
        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.infolist():
                if entry.is_dir():
                    continue
                parts = PurePosixPath(entry.filename).parts
                if any(part in ('', '.', '..') or '\\' in part or ':' in part for part in parts):
                    raise UpdateError(f'The release holds a file outside its folder: {entry.filename}')
                if parts == ('Scuffle', 'Scuffle.exe'):
                    target = staged
                elif len(parts) > 2 and parts[:2] == ('Scuffle', name):
                    target = partial.joinpath(*parts[2:])
                else:
                    raise UpdateError(f'The release holds an unexpected file: {entry.filename}')
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as source, open(target, 'wb') as output:
                    shutil.copyfileobj(source, output, BLOCK)
                    output.flush()
                    os.fsync(output.fileno())
        if not staged.is_file() or not partial.is_dir():
            raise UpdateError('The release lacks Scuffle.exe or its version folder')
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    os.replace(partial, final)
    return final


def clean_environment() -> dict[str, str]:
    """What another copy of Scuffle should start with, free of this running one's own package."""
    environment = dict(os.environ)
    # PyInstaller's variables of this running Scuffle must not reach the new one.
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    appdir = environment.get('APPDIR')
    for name in ['APPDIR', 'APPIMAGE', 'ARGV0', 'OWD', 'SCUFFLE_BRIDGE']:
        environment.pop(name, None)
    if appdir:
        environment['PATH'] = os.pathsep.join(part for part in environment.get('PATH', '').split(os.pathsep)
                                              if not part.startswith(appdir))
    return environment


def _try_out(target: Path, version: str) -> None:
    """Whether the new file runs on this system and is the version it should be, before anything changes."""
    try:
        result = subprocess.run([str(target), '--version'], env=clean_environment(), capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=TRY_OUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f'The new version does not start on this system: {error}') from error
    if result.returncode or result.stdout.strip() != f'scuffle {version}':
        raise UpdateError(f'The new version does not start on this system: exit code {result.returncode}')


def _swap(running: Path, staged: Path) -> None:
    backup = _sibling(running, 'old')
    backup.unlink(missing_ok=True)
    if system.WINDOWS:
        # A running program can be renamed but not overwritten, so for a moment no file has its name.
        os.replace(running, backup)
        try:
            os.replace(staged, running)
        except OSError:
            os.replace(backup, running)
            raise
    else:
        os.link(running, backup)
        os.replace(staged, running)


def install(release: Release, progress: Callable[[int, int], None] | None = None,
            cancelled: Callable[[], bool] | None = None) -> None:
    """Replace this Scuffle with the release. It runs on after this, and the next start is the new one."""
    running = program()
    if running is None or not release.asset:
        raise UpdateError('This installation cannot update itself')
    staged = _sibling(running, 'new')
    archive = running.with_name(f'{running.stem}.new.zip')
    folder, swapped = None, False
    try:
        if system.WINDOWS:
            _download(release, archive, _expected_sum(release), progress, cancelled)
            folder = _unpack(archive, staged, release.version)
        else:
            _download(release, staged, _expected_sum(release), progress, cancelled)
        _try_out(staged, release.version)
        _swap(running, staged)
        swapped = True
    except (OSError, http.client.HTTPException, zipfile.BadZipFile) as error:
        raise UpdateError(f'The update could not be installed: {error}') from error
    finally:
        for path in (staged, archive):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        if folder is not None and not swapped:
            shutil.rmtree(folder, ignore_errors=True)


def restart() -> None:
    """Start the Scuffle on disk. Call once this one no longer holds the window's lock."""
    running = program()
    if running is None:
        return
    options = ({'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
               if system.WINDOWS else {'start_new_session': True})
    subprocess.Popen([str(running)], env=clean_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, **options)


def leftovers(running: Path) -> list[Path]:
    """What an update leaves beside the program: the old one, and an interrupted download.

    In the Windows package also the other versions' folders. Only names an update gives are listed.
    """
    found = [_sibling(running, 'old'), _sibling(running, 'new'), running.with_name(f'{running.stem}.new.zip')]
    if system.WINDOWS:
        own = Path(getattr(sys, '_MEIPASS', '')).name
        found += [path for path in running.parent.iterdir()
                  if FOLDER.fullmatch(path.name) and path.name != own and path.is_dir()]
    return found


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def finish() -> None:
    """The new version has started, so the old one and an interrupted download can go."""
    running = program()
    if running is None:
        return
    try:
        remaining = leftovers(running)
    except OSError:
        return

    def work():
        deadline = time.monotonic() + FINISH_SECONDS
        while True:
            for path in list(remaining):
                try:
                    _remove(path)
                    remaining.remove(path)
                except OSError:
                    pass
            if not remaining or time.monotonic() >= deadline:
                return
            time.sleep(1)

    threading.Thread(target=work, name='scuffle-update-leftovers', daemon=True).start()
